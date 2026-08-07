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
        self.night_mode = False
        self.emergency_number = ""
        self.police_call_delay_seconds = 60

        # Last time each board was heard from. Absent = never seen this run.
        # Board liveness is derived from these timestamps rather than stored
        # as a flag, so it can't get stuck "online" the way a plain boolean
        # did before — nothing has to remember to clear it.
        self._board_last_seen: dict[str, datetime] = {}

        self._failed_attempts = 0
        self._locked_until: datetime | None = None
        self._arming_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        # Which mode the system was last armed in. Needed so a state report
        # from the door controller can reapply Home mode's sensor bypass —
        # the controller only reports armed/disarmed, never the mode.
        self._arm_mode: ArmMode = ArmMode.away

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
        self.night_mode = data.get("night_mode", self.night_mode)
        self.emergency_number = data.get("emergency_number", self.emergency_number)
        self.police_call_delay_seconds = data.get(
            "police_call_delay_seconds", self.police_call_delay_seconds
        )

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
            "night_mode": self.night_mode,
            "emergency_number": self.emergency_number,
            "police_call_delay_seconds": self.police_call_delay_seconds,
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
        """Starts [_monitor_boards]. Called from the FastAPI lifespan handler
        rather than from `__init__`, because creating a task requires a
        running event loop and this object is constructed at import time.
        """
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_boards())

    async def stop_monitor(self) -> None:
        """Cancels the board monitor on shutdown and waits for it to actually
        finish, so the loop can't outlive the app and log into a closing
        event loop."""
        if self._monitor_task is None:
            return
        self._monitor_task.cancel()
        try:
            await self._monitor_task
        except asyncio.CancelledError:
            pass
        self._monitor_task = None

    def snapshot(self) -> FullState:
        """Builds the current FullState payload sent both over /state and
        over the WebSocket."""
        return FullState(
            alarm_state=self.alarm_state,
            sensors=self.sensors,
            events=self.events,
            faces=self.faces,
            esp32_online=self.esp32_online,
            boards=self.board_statuses(),
            camera_online=self.camera_online,
            face_id_enabled=self.face_id_enabled,
            night_mode=self.night_mode,
            emergency_number=self.emergency_number,
            police_call_delay_seconds=self.police_call_delay_seconds,
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

    async def trigger_alert(self, source: SensorType | None, reason: str) -> None:
        """Raises an alert and flips the system to `triggered`, if (and
        only if) the system is currently `armed`. `source` identifies
        which sensor reported it (SensorReading is marked ok=False for
        that sensor below) or is None for non-sensor alerts (failed
        logins, the debug/demo trigger). Called from three places:
        AppState._register_failed_attempt (lockout), /trigger-alert (demo
        button), and /esp32/sensor-event (real sensor reports).
        """
        if self.alarm_state != AlarmState.armed:
            # Ignore triggers while disarmed, still arming (transitional
            # — sensors legitimately fire during the exit walk), or
            # already triggered (avoid re-logging/re-broadcasting an
            # alert that's already in progress).
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

    async def log_face_recognized(self, name: str) -> None:
        """Records a face-recognition match in the activity log. Purely
        an audit/notification action — recognizing a face does not by
        itself disarm the system or change alarm_state.
        """
        self._log(f"Face recognized: {name}", EventCategory.faceId)
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
        """Toggles night mode and logs it.

        The log message says only that the setting changed. It used to claim
        night mode "auto-arms overnight", which no code anywhere implements —
        a log line describing behavior the system doesn't have is worse than
        no log line, because it's the record someone would consult after an
        incident.
        """
        self.night_mode = enabled
        self._log(
            "Night mode enabled" if enabled else "Night mode disabled",
            EventCategory.info,
        )
        self._persist()
        await self._broadcast()

    async def set_emergency_number(self, number: str) -> None:
        """Stores the number to call when police auto-dial (if/once
        implemented) would trigger. Just a plain setting — no validation and
        no log entry, but it is persisted and broadcast so a second signed-in
        device doesn't keep showing the old value.
        """
        self.emergency_number = number
        self._persist()
        await self._broadcast()

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
