"""Central application state — the Python equivalent of MockBackendService
+ AuthService on the Flutter side. Ports their exact behavior (timings,
lockout logic, arm-mode sensor bypass) so the two sides agree.

Durable across restarts: the settings that a user configured by hand
(password, approved faces, emergency contact, the ESP32 device key) are
written to a JSON file on every change and reloaded at startup — see
[AppState._persist]. Live state (alarm state, sensor readings, board
heartbeats) is deliberately *not* persisted; see that method for why.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import WebSocket

from . import notifier
from .models import (
    AlarmState,
    ApprovedFace,
    ArmMode,
    BoardStatus,
    EventCategory,
    FullState,
    SecurityEvent,
    SensorReading,
    SensorType,
)
from .security import TokenStore, hash_password, resolve_device_key, verify_password

logger = logging.getLogger("state")

MAX_LOGIN_ATTEMPTS = 3
LOCKOUT_DURATION = timedelta(seconds=30)
ARMING_EXIT_DELAY = timedelta(seconds=3)

# A board is considered offline once this long has passed with no heartbeat.
# The sketches ping every 10 seconds, so this tolerates two consecutive
# missed pings before reporting a problem — long enough not to flicker on one
# dropped packet, short enough that a genuinely dead board is noticed fast.
BOARD_TIMEOUT = timedelta(seconds=30)
# How often the background monitor re-evaluates board liveness. Board status
# is time-derived, so without something actively re-checking it, a board that
# stopped reporting would stay "online" in the UI until the next unrelated
# state change happened to trigger a broadcast.
BOARD_MONITOR_INTERVAL = 5.0

# How often the night-mode schedule is evaluated. Once a minute is enough
# for hour-granularity scheduling and costs essentially nothing.
NIGHT_MODE_CHECK_INTERVAL = 60.0

# Names the three sketches identify themselves with on /esp32/heartbeat.
# Kept as plain strings (not a SensorType) because the door controller is not
# a sensor — it's the arm/disarm authority that also happens to own the door
# reed switch.
BOARD_NAMES = ("ultrasonic", "vibration", "door_controller")

# Keeping every event forever would grow without bound in a process meant to
# run for weeks. The app's log screen only ever shows a scrollback anyway.
MAX_EVENTS = 200

DEFAULT_PASSWORD = "homeguard"
STATE_FILE = Path(
    os.environ.get("HOMEGUARD_STATE_FILE", Path(__file__).resolve().parent.parent / "state.json")
)


class AppState:
    """Single in-process instance (see `state` at the bottom of this file)
    holding every piece of mutable system state, plus the logic that
    mutates it. FastAPI route handlers in main.py are thin wrappers around
    these methods; almost every mutator ends by calling `_broadcast()` so
    all connected app instances stay in sync in real time.
    """

    def __init__(self) -> None:
        """Sets up initial state — disarmed, all sensors ok, no boards seen
        yet — then overlays whatever was persisted from a previous run (see
        [_load]). The defaults here are what a brand-new install gets: the
        password "homeguard" and two pre-approved faces.
        """
        self._password_hash = hash_password(DEFAULT_PASSWORD)
        self.alarm_state = AlarmState.disarmed
        self.sensors: list[SensorReading] = [
            SensorReading(type=SensorType.ultrasonic, label="Ultrasonic sensor", ok=True),
            SensorReading(type=SensorType.door, label="Door sensor", ok=True),
            SensorReading(type=SensorType.vibration, label="Vibration sensor", ok=True),
        ]
        self.events: list[SecurityEvent] = []
        self.faces: list[ApprovedFace] = [
            ApprovedFace(id="1", name="Guy Bar"),
            ApprovedFace(id="2", name="Yair Bonfil"),
        ]
        self._next_face_id = 3  # ids "1"/"2" are taken by the seed faces above
        self.camera_online = False
        self.face_id_enabled = True
        # Off by default and deliberately so — see log_face_recognized for
        # why letting a face turn the alarm off is a security trade-off.
        self.face_disarm_enabled = False
        self.night_mode = False
        # Whole hours (0-23): when night mode arms and disarms. Defaults to
        # arming at 23:00 and disarming at 07:00.
        self.night_mode_arm_hour = 23
        self.night_mode_disarm_hour = 7
        self.emergency_number = ""
        self.police_call_delay_seconds = 60
        # Push-notification target (see backend/notifier.py). Empty topic =
        # disabled, which is the default: pushing requires the user to pick a
        # topic name, since that name is the only thing protecting it.
        self.ntfy_server = "https://ntfy.sh"
        self.ntfy_topic = ""

        # Last time each board was heard from. Absent = never seen this run.
        # Board liveness is derived from these timestamps rather than stored
        # as a flag, so it can't get stuck "online" the way a plain boolean
        # did before — nothing has to remember to clear it.
        self._board_last_seen: dict[str, datetime] = {}

        self._failed_attempts = 0
        self._locked_until: datetime | None = None
        self._arming_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._night_mode_task: asyncio.Task | None = None
        # Pending "alarm still not cleared" follow-up; cancelled by disarm.
        self._escalation_task: asyncio.Task | None = None
        # Which mode the system was last armed in. Needed so a state report
        # from the door controller can reapply Home mode's sensor bypass —
        # the controller only reports armed/disarmed, never the mode.
        self._arm_mode: ArmMode = ArmMode.away
        # When the door controller's disarm countdown runs out. Set by
        # begin_entry_delay, read by entry_delay_seconds_remaining.
        self._entry_delay_ends_at: datetime | None = None

        self.tokens = TokenStore()
        self.device_key = ""

        self._sockets: set[WebSocket] = set()

        self._load()

    # ---- Persistence ---------------------------------------------------------

    def _load(self) -> None:
        """Overlays the persisted state file (if any) onto the defaults set
        in `__init__`, then makes sure a device key exists.

        Every field is read defensively with `.get(...)` and a fallback,
        because this file is plain JSON on disk that a user may well have
        opened and edited. A malformed or partial file degrades to defaults
        rather than preventing the backend from starting — an alarm system
        that won't boot because its config file has a typo is worse than one
        that boots with a default password and says so.
        """
        data: dict = {}
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Could not read state file %s (%s) — using defaults.", STATE_FILE, exc
                )

        self._password_hash = data.get("password_hash") or self._password_hash
        self.face_id_enabled = data.get("face_id_enabled", self.face_id_enabled)
        self.face_disarm_enabled = data.get("face_disarm_enabled", self.face_disarm_enabled)
        self.night_mode = data.get("night_mode", self.night_mode)
        self.night_mode_arm_hour = data.get("night_mode_arm_hour", self.night_mode_arm_hour)
        self.night_mode_disarm_hour = data.get(
            "night_mode_disarm_hour", self.night_mode_disarm_hour
        )
        self.emergency_number = data.get("emergency_number", self.emergency_number)
        self.police_call_delay_seconds = data.get(
            "police_call_delay_seconds", self.police_call_delay_seconds
        )
        self.ntfy_server = data.get("ntfy_server") or self.ntfy_server
        self.ntfy_topic = data.get("ntfy_topic", self.ntfy_topic)

        faces = data.get("faces")
        if isinstance(faces, list):
            try:
                self.faces = [ApprovedFace(**f) for f in faces]
            except (TypeError, ValueError) as exc:
                logger.warning("Persisted faces are malformed (%s) — keeping seed faces.", exc)

        events = data.get("events")
        if isinstance(events, list):
            try:
                self.events = [SecurityEvent(**e) for e in events][:MAX_EVENTS]
            except (TypeError, ValueError) as exc:
                logger.warning("Persisted events are malformed (%s) — starting an empty log.", exc)

        # Face ids must never be reused: a recycled id would silently attach
        # a new person to the previous holder's training photos on disk.
        # Derived from the loaded faces rather than persisted separately so
        # the two can't disagree.
        numeric_ids = [int(f.id) for f in self.faces if f.id.isdigit()]
        self._next_face_id = max(numeric_ids, default=0) + 1

        self.device_key = resolve_device_key(data.get("device_key"))
        # Written back unconditionally: on a first run this is what saves the
        # freshly generated key, and on later runs it's a cheap no-op rewrite.
        self._persist()

    def _persist(self) -> None:
        """Writes the durable subset of state to [STATE_FILE].

        What is saved: things a human configured and would be annoyed to lose
        — the password hash, the device key, approved faces, settings, and
        the event log.

        What is deliberately *not* saved: `alarm_state`, sensor readings, and
        board heartbeats. Those describe the physical world right now, and a
        restarted backend genuinely does not know any of them. Coming back up
        `disarmed` and letting the door controller's next `/esp32/state-change`
        report establish the truth is both honest and self-correcting; writing
        a stale `armed` back would make the app confidently display a state
        nothing has verified.

        Written via a temp file + `os.replace` so a crash mid-write can't
        leave a truncated JSON file behind — `replace` is atomic on both POSIX
        and Windows, so the file on disk is always either the old complete
        version or the new complete version.
        """
        payload = {
            "password_hash": self._password_hash,
            "device_key": self.device_key,
            "faces": [f.model_dump() for f in self.faces],
            "events": [json.loads(e.model_dump_json()) for e in self.events[:MAX_EVENTS]],
            "face_id_enabled": self.face_id_enabled,
            "face_disarm_enabled": self.face_disarm_enabled,
            "night_mode": self.night_mode,
            "night_mode_arm_hour": self.night_mode_arm_hour,
            "night_mode_disarm_hour": self.night_mode_disarm_hour,
            "emergency_number": self.emergency_number,
            "police_call_delay_seconds": self.police_call_delay_seconds,
            "ntfy_server": self.ntfy_server,
            "ntfy_topic": self.ntfy_topic,
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = STATE_FILE.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temp, STATE_FILE)
        except OSError as exc:
            # A read-only or full disk shouldn't take the alarm down; the
            # system keeps working, it just won't survive the next restart.
            logger.warning("Could not write state file %s: %s", STATE_FILE, exc)

    # ---- WebSocket pub/sub -------------------------------------------------

    async def register(self, ws: WebSocket) -> None:
        """Adds a newly-accepted WebSocket to the broadcast set. Called by
        the /ws handler in main.py right after `websocket.accept()`."""
        self._sockets.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        """Removes a WebSocket from the broadcast set, e.g. once the /ws
        handler detects the client disconnected. `discard` (not `remove`)
        so this is safe to call even if the socket was never registered
        or was already removed by `_broadcast`."""
        self._sockets.discard(ws)

    async def _broadcast(self) -> None:
        """Pushes a fresh state snapshot to every connected app instance.
        Called at the end of nearly every mutating method so all clients
        stay live-synced instead of having to poll /state.
        """
        if not self._sockets:
            return
        payload = self.snapshot().model_dump_json()
        dead = []
        for ws in self._sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                # A send failure means that socket is gone (client closed,
                # network drop, etc). Broad except because the exact
                # exception type varies by transport/ASGI server; any
                # failure here just means "drop this socket", not a bug
                # to propagate. Collected instead of discarded in-loop to
                # avoid mutating self._sockets while iterating over it.
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)

    # ---- Board liveness ------------------------------------------------------

    def board_statuses(self) -> list[BoardStatus]:
        """Current online/offline status of each of the three boards,
        computed fresh from their last-heartbeat timestamps every time this
        is called. Boards never heard from this run report `last_seen=None`
        and `online=False`.
        """
        now = datetime.now()
        return [
            BoardStatus(
                name=name,
                online=(
                    name in self._board_last_seen
                    and now - self._board_last_seen[name] < BOARD_TIMEOUT
                ),
                last_seen=self._board_last_seen.get(name),
            )
            for name in BOARD_NAMES
        ]

    @property
    def esp32_online(self) -> bool:
        """Aggregate "is any board reachable" flag, kept because the app's
        connectivity indicator is a single badge. Per-board detail is in
        [board_statuses] — the app shows both.
        """
        return any(b.online for b in self.board_statuses())

    async def note_board_seen(self, board: str) -> None:
        """Records a heartbeat (or any other contact) from `board`.

        Broadcasts only when this actually flips the board from offline to
        online, not on every heartbeat — three boards pinging every 10
        seconds would otherwise mean a full-state push to every connected app
        six times a minute for no reason.
        """
        was_online = self.esp32_online
        known = board in self._board_last_seen
        previously = (
            known and datetime.now() - self._board_last_seen[board] < BOARD_TIMEOUT
        )
        self._board_last_seen[board] = datetime.now()
        if not previously or was_online != self.esp32_online:
            await self._broadcast()

    async def _monitor_boards(self) -> None:
        """Background loop that re-broadcasts when a board's derived
        online/offline status changes.

        Necessary because board status is a function of elapsed time, not of
        any event: when a board dies it stops sending, and "stopped sending"
        is precisely not something that can trigger a push on its own. Without
        this loop the app would keep showing a dead board as online until some
        unrelated state change happened to cause a broadcast.
        """
        previous = [(b.name, b.online) for b in self.board_statuses()]
        while True:
            await asyncio.sleep(BOARD_MONITOR_INTERVAL)
            current = [(b.name, b.online) for b in self.board_statuses()]
            if current != previous:
                # strict=True: both lists come from board_statuses(), so a
                # length mismatch would mean the board set changed under us —
                # worth raising rather than silently truncating the log.
                for (name, was), (_, now_online) in zip(previous, current, strict=True):
                    if was and not now_online:
                        self._log(f"Board offline: {name}", EventCategory.info)
                    elif not was and now_online:
                        self._log(f"Board online: {name}", EventCategory.info)
                previous = current
                await self._broadcast()

    def start_monitor(self) -> None:
        """Starts the background loops — board liveness and the night-mode
        schedule. Called from the FastAPI lifespan handler rather than from
        `__init__`, because creating a task requires a running event loop and
        this object is constructed at import time.
        """
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_boards())
        if self._night_mode_task is None:
            self._night_mode_task = asyncio.create_task(self._night_mode_loop())

    async def stop_monitor(self) -> None:
        """Cancels the background loops on shutdown and waits for them to
        actually finish, so neither can outlive the app and log into a
        closing event loop."""
        for name in ("_monitor_task", "_night_mode_task"):
            task = getattr(self, name)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, name, None)

    def snapshot(self) -> FullState:
        """Builds the current FullState payload sent both over /state and
        over the WebSocket."""
        return FullState(
            alarm_state=self.alarm_state,
            entry_delay_seconds_remaining=self.entry_delay_seconds_remaining,
            sensors=self.sensors,
            events=self.events,
            faces=self.faces,
            esp32_online=self.esp32_online,
            boards=self.board_statuses(),
            camera_online=self.camera_online,
            face_id_enabled=self.face_id_enabled,
            face_disarm_enabled=self.face_disarm_enabled,
            night_mode=self.night_mode,
            night_mode_arm_hour=self.night_mode_arm_hour,
            night_mode_disarm_hour=self.night_mode_disarm_hour,
            emergency_number=self.emergency_number,
            police_call_delay_seconds=self.police_call_delay_seconds,
            ntfy_server=self.ntfy_server,
            ntfy_topic=self.ntfy_topic,
        )

    def _log(self, message: str, category: EventCategory = EventCategory.info) -> None:
        """Prepends a new SecurityEvent to the activity log. Inserted at
        index 0 (rather than appended) so `events` stays newest-first,
        matching how the app's event log screen displays it, and trimmed to
        [MAX_EVENTS] so a long-running process doesn't grow without bound.
        Does not broadcast itself — callers are expected to call
        `_broadcast()` afterwards (usually as part of the same state change).
        """
        self.events.insert(
            0, SecurityEvent(timestamp=datetime.now(), message=message, category=category)
        )
        del self.events[MAX_EVENTS:]

    # ---- Auth ---------------------------------------------------------------

    @property
    def is_locked_out(self) -> bool:
        """True while a login lockout (from MAX_LOGIN_ATTEMPTS failures)
        is still in effect. `_locked_until` is only cleared implicitly by
        time passing, not reset on read, so this must be re-checked
        (rather than cached) every time it matters."""
        return self._locked_until is not None and datetime.now() < self._locked_until

    @property
    def lockout_remaining_seconds(self) -> int:
        """Seconds left in the current lockout, for the app's countdown
        display. 0 whenever not locked out."""
        if not self.is_locked_out:
            return 0
        return max(0, int((self._locked_until - datetime.now()).total_seconds()))

    @property
    def attempts_remaining(self) -> int:
        """How many more wrong passwords are allowed before lockout
        triggers. Clamped at 0 (never negative) for display purposes."""
        return max(0, MAX_LOGIN_ATTEMPTS - self._failed_attempts)

    async def _register_failed_attempt(self, reason: str) -> None:
        """Shared failure path for every password check in the system.

        Both `/login` and `/change-password` route through here, which is the
        point: previously only `/login` counted attempts, so `/change-password`
        was an unthrottled password oracle — an attacker could guess the
        password there indefinitely without ever tripping the 3-attempt
        lockout that `/login` enforced.
        """
        self._failed_attempts += 1
        if self._failed_attempts >= MAX_LOGIN_ATTEMPTS:
            self._locked_until = datetime.now() + LOCKOUT_DURATION
            # Mirrors the physical keypad's documented lockout behavior
            # from the original spec: 3 wrong attempts while armed
            # triggers the alarm, treating it as a possible break-in.
            await self.trigger_alert(source=None, reason=reason)

    async def login(self, password: str) -> str | None:
        """Attempts a login. Returns a fresh session token on success, or
        None on failure — the token is what every subsequent request
        authenticates with, so a caller that gets None has no way to do
        anything else.

        On success, resets the failed-attempt counter. On failure, defers to
        [_register_failed_attempt] for the counter/lockout/alert logic.
        """
        if self.is_locked_out:
            return None

        if not verify_password(password, self._password_hash):
            await self._register_failed_attempt(f"{MAX_LOGIN_ATTEMPTS} failed login attempts")
            return None

        self._failed_attempts = 0
        self._log("Logged in", EventCategory.auth)
        await self._broadcast()
        return self.tokens.issue()

    async def change_password(
        self, old_password: str, new_password: str, current_token: str | None = None
    ) -> bool:
        """Changes the login password after verifying `old_password`.

        Rejects a new password that's empty/whitespace-only (`.strip()` check)
        so the account can't accidentally be left with a blank password, and
        rejects a wrong `old_password` through the same counted/lockout path
        as a failed login (see [_register_failed_attempt]).

        On success, every *other* session token is revoked — `current_token`
        (the device that made this change) keeps working, and every other
        signed-in device is forced to re-authenticate with the new password.
        Leaving old sessions alive would defeat the main reason to change an
        alarm system's password.
        """
        if self.is_locked_out:
            return False
        if not verify_password(old_password, self._password_hash):
            await self._register_failed_attempt(
                f"{MAX_LOGIN_ATTEMPTS} failed password-change attempts"
            )
            return False
        if not new_password.strip():
            return False

        self._failed_attempts = 0
        self._password_hash = hash_password(new_password)
        self.tokens.revoke_all(except_token=current_token)
        self._log("Password changed", EventCategory.auth)
        self._persist()
        await self._broadcast()
        return True

    # ---- Arm / disarm ---------------------------------------------------------

    async def arm(self, mode: ArmMode) -> None:
        """Starts arming in the given mode. Only takes effect from
        `disarmed` — arming is a no-op if already transitional/armed/
        triggered, so repeated /arm calls (e.g. a double-tap in the app)
        can't restart the exit-delay countdown or interrupt an alert.
        Immediately flips to `transitional` and broadcasts so the app can
        show the exit-delay countdown right away, then schedules
        `_finish_arming` to run after the delay.
        """
        if self.alarm_state != AlarmState.disarmed:
            return
        self._arm_mode = mode
        self._log(
            f"Arming ({'Away' if mode == ArmMode.away else 'Home'}) — exit now",
            EventCategory.armDisarm,
        )
        self.alarm_state = AlarmState.transitional
        await self._broadcast()

        # Defensive: the `disarmed`-only guard above means there should
        # never be a prior task still running here, but cancel it anyway
        # before replacing the reference so a stray leftover task can
        # never keep running (and later flip the state) unnoticed.
        if self._arming_task:
            self._arming_task.cancel()
        self._arming_task = asyncio.create_task(self._finish_arming(mode))

    def _apply_bypass(self, mode: ArmMode) -> None:
        """Resets every sensor to ok and applies `mode`'s bypass rules.

        Home mode bypasses the vibration sensor (people moving around inside
        shouldn't trip it); Away mode bypasses nothing. Factored out of
        `_finish_arming` so `report_state_from_esp32` can apply the exact
        same rules when the door controller is what puts the system into
        `armed` — previously that path had no way to do this and Home mode's
        bypass was silently lost whenever the controller won the race.
        """
        self.sensors = [
            SensorReading(
                type=s.type,
                label=s.label,
                ok=True,
                bypassed=(mode == ArmMode.home and s.type == SensorType.vibration),
            )
            for s in self.sensors
        ]

    async def _finish_arming(self, mode: ArmMode) -> None:
        """Background task scheduled by `arm()`. Waits out the exit
        delay, then transitions from `transitional` to `armed` and
        applies the mode's sensor bypass. Runs as a cancellable
        asyncio.Task specifically so `disarm()` or
        `report_state_from_esp32()` can abort it if the system leaves
        `transitional` before the delay elapses.
        """
        await asyncio.sleep(ARMING_EXIT_DELAY.total_seconds())
        self.alarm_state = AlarmState.armed
        self._apply_bypass(mode)
        self._log(
            f"System armed ({'Away' if mode == ArmMode.away else 'Home'})",
            EventCategory.armDisarm,
        )
        await self._broadcast()

    async def disarm(self) -> None:
        """Disarms the system. Cancels any in-progress `_finish_arming`
        task first, so a disarm during the exit-delay window reliably
        stops that task from later flipping the state back to `armed`
        out from under this call. Always resets every sensor to
        ok=True/bypassed=False (SensorReading defaults), even if the
        system was already disarmed, so sensors never get stuck bypassed
        or faulted after a disarm. If already disarmed, still broadcasts
        (to push that sensor reset to clients) but skips re-logging
        "System disarmed" to avoid duplicate log entries.
        """
        if self._arming_task:
            self._arming_task.cancel()
            self._arming_task = None
        # Whatever countdown was running is over — clear it so a stale
        # deadline can't make the app show a countdown after disarming.
        self._entry_delay_ends_at = None
        # Acknowledging the alarm in time is exactly what should stop the
        # escalation notification from going out.
        if self._escalation_task:
            self._escalation_task.cancel()
            self._escalation_task = None
        self.sensors = [
            SensorReading(type=s.type, label=s.label, ok=True) for s in self.sensors
        ]
        if self.alarm_state == AlarmState.disarmed:
            await self._broadcast()
            return
        self.alarm_state = AlarmState.disarmed
        self._log("System disarmed", EventCategory.armDisarm)
        await self._broadcast()

    async def report_state_from_esp32(self, armed: bool) -> None:
        """The door/keypad ESP32 is the authority on armed/disarmed — it's
        the board with the keypad, so it decides. It calls this whenever
        its state changes, whether that change came from the physical
        keypad or from us relaying an app-initiated /arm /disarm. That way
        the backend/app stay correct even if someone arms or disarms at
        the keypad without touching the app at all.
        """
        # The controller's report is authoritative and can arrive at any
        # time, including mid-exit-delay (e.g. it finished its own timer,
        # or someone armed/disarmed directly at the keypad). Cancel our
        # local _finish_arming task so it can't fire later and stomp on
        # the state this report is about to set.
        if self._arming_task:
            self._arming_task.cancel()
            self._arming_task = None

        new_state = AlarmState.armed if armed else AlarmState.disarmed
        if new_state == self.alarm_state:
            # Already agree with the controller (e.g. this is just a
            # follow-up report, or we got here via our own /arm /disarm
            # relay already having applied it) — skip the log/broadcast
            # so we don't spam duplicate "armed/disarmed" events.
            return

        self.alarm_state = new_state
        # The controller reports only armed/disarmed, never which mode was
        # requested — so the mode comes from `_arm_mode`, which `arm()`
        # recorded when the request came in. That's what lets Home mode's
        # vibration bypass survive the controller winning the race to set
        # `armed`; arming from the physical keypad (which the app never told
        # us a mode for) falls back to the last mode used, defaulting to Away
        # — the stricter of the two, so an unknown mode never silently
        # disables a sensor.
        if armed:
            self._apply_bypass(self._arm_mode)
        else:
            self.sensors = [
                SensorReading(type=s.type, label=s.label, ok=True) for s in self.sensors
            ]
        self._log(
            "System armed (reported by controller)"
            if armed
            else "System disarmed (reported by controller)",
            EventCategory.armDisarm,
        )
        await self._broadcast()

    async def begin_entry_delay(self, seconds: int) -> None:
        """The door controller reports that someone opened the door on an
        armed system and the disarm countdown has started.

        Only meaningful from `armed`. Previously the controller ran this
        window silently and reported only the outcome, so the app jumped
        straight from ARMED to TRIGGERED — the person walking in had no idea
        how long they had, and neither did anyone watching remotely.

        `entry_delay_ends_at` is stored so `entry_delay_seconds_remaining`
        can be derived on every snapshot; the controller owns the real timer,
        this is a display mirror of it (same pattern as the exit-delay
        countdown).
        """
        if self.alarm_state != AlarmState.armed:
            return
        self.alarm_state = AlarmState.entryDelay
        self._entry_delay_ends_at = datetime.now() + timedelta(seconds=seconds)
        self._log("Door opened — enter code to disarm", EventCategory.armDisarm)
        await self._broadcast()

    @property
    def entry_delay_seconds_remaining(self) -> int:
        """Seconds left to disarm before the alarm sounds, for the app's
        countdown. 0 whenever not in an entry delay."""
        if self.alarm_state != AlarmState.entryDelay or self._entry_delay_ends_at is None:
            return 0
        return max(0, int((self._entry_delay_ends_at - datetime.now()).total_seconds()))

    async def trigger_alert(self, source: SensorType | None, reason: str) -> None:
        """Raises an alert and flips the system to `triggered`, if (and
        only if) the system is currently `armed`. `source` identifies
        which sensor reported it (SensorReading is marked ok=False for
        that sensor below) or is None for non-sensor alerts (failed
        logins, the debug/demo trigger). Called from three places:
        AppState._register_failed_attempt (lockout), /trigger-alert (demo
        button), and /esp32/sensor-event (real sensor reports).
        """
        if self.alarm_state not in (AlarmState.armed, AlarmState.entryDelay):
            # Ignore triggers while disarmed, still arming (transitional
            # — sensors legitimately fire during the exit walk), or
            # already triggered (avoid re-logging/re-broadcasting an
            # alert that's already in progress).
            #
            # `entryDelay` *is* escalatable: it's the state the system is in
            # when the door controller's disarm countdown expires, and that
            # expiry arrives here as the controller's door alert.
            return

        # A bypassed sensor (e.g. vibration in Home mode) is expected to
        # keep firing physically — that board doesn't know about arm
        # modes, it just reports. It's this check, not the board, that
        # makes Home mode's "ignore vibration" promise actually hold.
        if source is not None:
            reporting_sensor = next((s for s in self.sensors if s.type == source), None)
            if reporting_sensor is not None and reporting_sensor.bypassed:
                return

            self.sensors = [
                SensorReading(type=s.type, label=s.label, ok=False, bypassed=s.bypassed)
                if s.type == source
                else s
                for s in self.sensors
            ]
        self.alarm_state = AlarmState.triggered
        self._log(f"ALERT — {reason}", EventCategory.alert)
        self._persist()  # an alert is exactly the event worth surviving a restart
        # Pushed before the broadcast, but non-blocking (see notifier.notify):
        # this only schedules the request, so the state push below still goes
        # out immediately even if the ntfy server is slow or unreachable.
        notifier.notify(
            self.ntfy_server,
            self.ntfy_topic,
            title="HomeGuard alarm",
            message=reason,
        )
        self._start_escalation(reason)
        await self._broadcast()

    def _start_escalation(self, reason: str) -> None:
        """Schedules the follow-up notification for an alarm nobody has
        cleared.

        This is what `emergency_number` and `police_call_delay_seconds` now
        actually drive. They previously drove nothing at all: the settings
        were stored, the UI said the number would be "auto-called", and no
        code anywhere dialled anything.

        Genuinely placing a phone call needs a telephony provider (an account,
        credentials, per-message cost) — out of scope here, and adding a
        half-working version would be worse than none. What this does instead
        is honest and useful: after the configured delay, if the alarm is
        *still* unacknowledged, it pushes a second, louder notification naming
        the emergency contact to call. The UI wording matches.
        """
        if self._escalation_task:
            self._escalation_task.cancel()
        self._escalation_task = asyncio.create_task(self._escalate_after_delay(reason))

    async def _escalate_after_delay(self, reason: str) -> None:
        """Waits out the response window, then pushes the escalation if the
        alarm is still triggered. Cancelled by `disarm`, so acknowledging the
        alarm in time stops it."""
        try:
            await asyncio.sleep(self.police_call_delay_seconds)
        except asyncio.CancelledError:
            return
        if self.alarm_state != AlarmState.triggered:
            return

        contact = self.emergency_number.strip()
        message = (
            f"Alarm still active after {self.police_call_delay_seconds}s — {reason}."
            + (f" Call {contact}." if contact else "")
        )
        self._log("Alarm not cleared — escalation notification sent", EventCategory.alert)
        notifier.notify(
            self.ntfy_server,
            self.ntfy_topic,
            title="HomeGuard — ALARM NOT CLEARED",
            message=message,
            priority="urgent",
            tags="rotating_light,sos",
        )
        await self._broadcast()

    # ---- Faces ----------------------------------------------------------------

    async def add_face(self, name: str) -> ApprovedFace:
        """Registers a new approved-face entry (id + name only — no photo
        yet; the caller uploads training photos separately via the
        FaceRecognitionService). ids are simple ascending strings, not
        UUIDs, and are never reused — see the `_next_face_id` derivation in
        [_load] for why that matters.
        """
        face = ApprovedFace(id=str(self._next_face_id), name=name)
        self._next_face_id += 1
        self.faces.append(face)
        self._log(f"Approved face added: {name}", EventCategory.faceId)
        self._persist()
        await self._broadcast()
        return face

    async def remove_face(self, face_id: str) -> None:
        """Removes an approved-face entry by id. No-ops quietly (no log
        entry, but still broadcasts) if the id doesn't exist, e.g. a
        double-delete race. Only updates this record — deleting the
        underlying trained photos is the caller's job (see
        FaceRecognitionService.remove_face, called separately in main.py).
        """
        face = next((f for f in self.faces if f.id == face_id), None)
        self.faces = [f for f in self.faces if f.id != face_id]
        if face:
            self._log(f"Approved face removed: {face.name}", EventCategory.faceId)
        self._persist()
        await self._broadcast()

    async def log_face_recognized(self, name: str) -> bool:
        """Records a face-recognition match and, if face-disarm is enabled,
        disarms the system. Returns whether it disarmed.

        Face-disarm is **off by default**, deliberately. SFace compares face
        embeddings; it has no liveness detection, so a printed photo or a
        picture on a phone screen may well pass. That makes this a
        convenience feature, not a security one — it lowers the bar to
        disarming from "knows the PIN or the password" to "has a photo of
        someone who lives here", which is a real downgrade for an alarm.
        Enabling it is an informed choice the user makes in Settings.

        Only disarms from `armed` or `triggered` — the states where disarming
        is a meaningful action. Recognizing a face while already disarmed is
        just an audit-log entry.
        """
        self._log(f"Face recognized: {name}", EventCategory.faceId)

        should_disarm = self.face_disarm_enabled and self.alarm_state in (
            AlarmState.armed,
            AlarmState.triggered,
            AlarmState.entryDelay,
        )
        if not should_disarm:
            await self._broadcast()
            return False

        # disarm() broadcasts and resets sensors; log the reason first so the
        # event log reads in cause-then-effect order.
        self._log(f"Disarmed by Face ID: {name}", EventCategory.armDisarm)
        await self.disarm()
        return True

    async def set_face_disarm_enabled(self, enabled: bool) -> None:
        """Turns face-disarm on or off. Logged either way — this changes what
        it takes to disarm the alarm, which is exactly the kind of change the
        activity log exists to record.
        """
        self.face_disarm_enabled = enabled
        self._log(
            "Face ID disarm enabled" if enabled else "Face ID disarm disabled",
            EventCategory.auth,
        )
        self._persist()
        await self._broadcast()

    async def set_face_id_enabled(self, enabled: bool) -> None:
        """Toggles whether /camera/recognize actually runs recognition
        (see that route's early-return in main.py). Not logged, since
        this is a routine settings toggle, not a security event.
        """
        self.face_id_enabled = enabled
        self._persist()
        await self._broadcast()

    # ---- Settings ---------------------------------------------------------------

    async def set_night_mode(self, enabled: bool) -> None:
        """Toggles night mode: automatic arming overnight.

        This setting used to be stored and nothing more, while both the UI and
        the log line claimed it "auto-arms overnight" — an alarm system
        telling the user it's covering them when it isn't. `_night_mode_tick`
        is what now makes the claim true.
        """
        self.night_mode = enabled
        self._log(
            f"Night mode enabled — arms at {self.night_mode_arm_hour:02d}:00, "
            f"disarms at {self.night_mode_disarm_hour:02d}:00"
            if enabled
            else "Night mode disabled",
            EventCategory.info,
        )
        self._persist()
        await self._broadcast()

    async def set_night_mode_hours(self, arm_hour: int, disarm_hour: int) -> None:
        """Sets when night mode arms and disarms, as whole hours (0-23).

        Whole hours rather than arbitrary times because the scheduler checks
        once a minute against the local clock — minute-level precision would
        imply an accuracy the design doesn't have.
        """
        self.night_mode_arm_hour = max(0, min(23, arm_hour))
        self.night_mode_disarm_hour = max(0, min(23, disarm_hour))
        self._persist()
        await self._broadcast()

    async def _night_mode_tick(self, now: datetime | None = None) -> None:
        """One evaluation of the night-mode schedule. Split out from the loop
        so tests can drive it with an explicit clock instead of waiting for
        real hours to pass.

        Arms only from `disarmed`, and disarms only from `armed` — never from
        `triggered` or `entryDelay`. That restraint is the important part: an
        alarm going off at 3am must not be silenced by the schedule reaching
        its morning disarm time. A human decides when an alert is over.

        Acts on the hour boundary only (`minute == 0`), so re-arming is not
        attempted every minute for an hour after someone deliberately
        disarmed at 02:30.
        """
        now = now or datetime.now()
        if not self.night_mode or now.minute != 0:
            return

        if now.hour == self.night_mode_arm_hour and self.alarm_state == AlarmState.disarmed:
            self._log("Night mode — arming automatically", EventCategory.armDisarm)
            await self.arm(ArmMode.home)
        elif now.hour == self.night_mode_disarm_hour and self.alarm_state == AlarmState.armed:
            self._log("Night mode — disarming automatically", EventCategory.armDisarm)
            await self.disarm()

    async def _night_mode_loop(self) -> None:
        """Checks the night-mode schedule once a minute."""
        while True:
            await asyncio.sleep(NIGHT_MODE_CHECK_INTERVAL)
            try:
                await self._night_mode_tick()
            except Exception:
                # A scheduling error must never kill the loop and silently
                # stop all future auto-arming.
                logger.exception("Night-mode check failed")

    async def set_emergency_number(self, number: str) -> None:
        """Stores the number to call when police auto-dial (if/once
        implemented) would trigger. Just a plain setting — no validation and
        no log entry, but it is persisted and broadcast so a second signed-in
        device doesn't keep showing the old value.
        """
        self.emergency_number = number
        self._persist()
        await self._broadcast()

    async def set_ntfy(self, server: str, topic: str) -> None:
        """Sets where alerts are pushed. An empty `topic` turns pushing off.

        The topic is not logged — on a public ntfy server it's the only thing
        keeping strangers from reading this house's alarm notifications, so it
        doesn't belong in an event log the app displays.
        """
        self.ntfy_server = server.strip() or "https://ntfy.sh"
        self.ntfy_topic = topic.strip()
        self._log(
            "Push notifications enabled" if self.ntfy_topic else "Push notifications disabled",
            EventCategory.info,
        )
        self._persist()
        await self._broadcast()

    async def send_test_notification(self) -> bool:
        """Pushes a harmless test notification, so the user can confirm the
        topic works *before* relying on it for a real break-in. Returns
        whether a topic was configured at all — not whether it was delivered,
        which is deliberately not awaited (see notifier.notify).
        """
        if not self.ntfy_topic:
            return False
        notifier.notify(
            self.ntfy_server,
            self.ntfy_topic,
            title="HomeGuard test",
            message="Test notification — your alerts will arrive here.",
            priority="default",
            tags="white_check_mark",
        )
        return True

    async def set_police_call_delay(self, seconds: int) -> None:
        """Stores how long to wait after a triggered alarm before
        auto-calling `emergency_number`. Like set_emergency_number, this
        is currently just stored — see that method's note.
        """
        self.police_call_delay_seconds = seconds
        self._persist()
        await self._broadcast()

    # ---- Camera connectivity ---------------------------------------------------

    async def set_camera_online(self, online: bool) -> None:
        """Updates the "camera online" flag shown in the app, set True by
        /camera/recognize whenever a frame comes in. No-ops when the value
        isn't actually changing, to avoid a broadcast per frame.
        """
        if online == self.camera_online:
            return
        self.camera_online = online
        await self._broadcast()


state = AppState()
