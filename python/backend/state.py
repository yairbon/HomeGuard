"""Central application state — the Python equivalent of MockBackendService
+ AuthService on the Flutter side. Ports their exact behavior (timings,
lockout logic, arm-mode sensor bypass) so the two sides agree.

In-memory only for now — fine for a course project; swap for a real store
if this needs to survive restarts.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta

from fastapi import WebSocket

from .models import (
    AlarmState,
    ApprovedFace,
    ArmMode,
    EventCategory,
    FullState,
    SecurityEvent,
    SensorReading,
    SensorType,
)

MAX_LOGIN_ATTEMPTS = 3
LOCKOUT_DURATION = timedelta(seconds=30)
ARMING_EXIT_DELAY = timedelta(seconds=3)


class AppState:
    """Single in-process instance (see `state` at the bottom of this file)
    holding every piece of mutable system state, plus the logic that
    mutates it. FastAPI route handlers in main.py are thin wrappers around
    these methods; almost every mutator ends by calling `_broadcast()` so
    all connected app instances stay in sync in real time.
    """

    def __init__(self) -> None:
        """Sets up the initial demo state: disarmed, all sensors ok, two
        pre-approved faces, default password "homeguard", and no ESP32/
        camera connected yet (those flip to True once the respective
        client actually reports in).
        """
        self._password = "homeguard"
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
        self._face_id_counter = itertools.count(3)  # starts at 3: ids "1"/"2" are already taken by the seed faces above
        self.esp32_online = False  # true once the ESP32 actually connects
        self.camera_online = False
        self.face_id_enabled = True
        self.night_mode = False
        self.emergency_number = ""
        self.police_call_delay_seconds = 60

        self._failed_attempts = 0
        self._locked_until: datetime | None = None
        self._arming_task: asyncio.Task | None = None
        # Which mode the system was last asked to arm in. Needed because
        # the door controller reports only armed/disarmed, never the mode
        # (see report_state_from_esp32) — without remembering it here,
        # Home mode's vibration bypass would silently not be applied
        # whenever the controller's report, rather than our own
        # _finish_arming, is what puts the system into `armed`. Reset to
        # `away` on disarm so a subsequent arm straight from the physical
        # keypad (which we never see a mode for) bypasses nothing, which
        # is the fail-safe direction.
        self._arm_mode = ArmMode.away

        self._sockets: set[WebSocket] = set()

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

    def snapshot(self) -> FullState:
        """Builds the current FullState payload sent both over /state and
        over the WebSocket."""
        return FullState(
            alarm_state=self.alarm_state,
            sensors=self.sensors,
            events=self.events,
            faces=self.faces,
            esp32_online=self.esp32_online,
            camera_online=self.camera_online,
            face_id_enabled=self.face_id_enabled,
            night_mode=self.night_mode,
            emergency_number=self.emergency_number,
            police_call_delay_seconds=self.police_call_delay_seconds,
        )

    def _log(self, message: str, category: EventCategory = EventCategory.info) -> None:
        """Prepends a new SecurityEvent to the activity log. Inserted at
        index 0 (rather than appended) so `events` stays newest-first,
        matching how the app's event log screen displays it. Does not
        broadcast itself — callers are expected to call `_broadcast()`
        afterwards (usually as part of the same state change).
        """
        self.events.insert(
            0, SecurityEvent(timestamp=datetime.now(), message=message, category=category)
        )

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

    def _clear_expired_lockout(self) -> None:
        """Drops a lockout whose window has already elapsed, along with
        the failed-attempt counter that caused it.

        Without this the counter stays pinned at MAX_LOGIN_ATTEMPTS after
        a lockout expires, so the very next wrong password would re-lock
        immediately instead of starting a fresh 3-attempt budget, and
        `attempts_remaining` would keep reporting 0 to the app while the
        user isn't actually locked out. Only `login` calls this: it's the
        single place that reads either field, and main.py builds its
        LoginResponse from them right after awaiting it.
        """
        if self._locked_until is not None and datetime.now() >= self._locked_until:
            self._locked_until = None
            self._failed_attempts = 0

    async def login(self, password: str) -> bool:
        """Attempts a login. Returns whether it succeeded. On success,
        resets the failed-attempt counter and logs it. On failure,
        increments the counter and, once it hits MAX_LOGIN_ATTEMPTS,
        starts a lockout AND raises an alert (see comment below) — this
        does not check `self.alarm_state`, so failed logins only actually
        set off the alarm via trigger_alert's own armed-only guard.
        """
        if self.is_locked_out:
            return False
        # Not locked out — but a *previous* lockout may have just run out,
        # in which case its counter needs clearing before we judge this
        # attempt against it.
        self._clear_expired_lockout()

        ok = password == self._password
        if ok:
            self._failed_attempts = 0
            self._log("Logged in", EventCategory.auth)
            await self._broadcast()
        else:
            self._failed_attempts += 1
            if self._failed_attempts >= MAX_LOGIN_ATTEMPTS:
                self._locked_until = datetime.now() + LOCKOUT_DURATION
                # Mirrors the physical keypad's documented lockout behavior
                # from the original spec: 3 wrong attempts while armed
                # triggers the alarm, treating it as a possible break-in.
                await self.trigger_alert(
                    source=None, reason=f"{MAX_LOGIN_ATTEMPTS} failed login attempts"
                )
        return ok

    async def change_password(self, old_password: str, new_password: str) -> bool:
        """Changes the login password after verifying `old_password`.
        Rejects a new password that's empty/whitespace-only (`.strip()`
        check) so the account can't accidentally be left with a blank
        password. Does not affect the login lockout counters.
        """
        if old_password != self._password or not new_password.strip():
            return False
        self._password = new_password
        self._log("Password changed", EventCategory.auth)
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
        self._log(
            f"Arming ({'Away' if mode == ArmMode.away else 'Home'}) — exit now",
            EventCategory.armDisarm,
        )
        # Remembered so report_state_from_esp32 can apply the right sensor
        # bypass if the controller's report beats _finish_arming to it.
        self._arm_mode = mode
        self.alarm_state = AlarmState.transitional
        await self._broadcast()

        # Defensive: the `disarmed`-only guard above means there should
        # never be a prior task still running here, but cancel it anyway
        # before replacing the reference so a stray leftover task can
        # never keep running (and later flip the state) unnoticed.
        if self._arming_task:
            self._arming_task.cancel()
        self._arming_task = asyncio.create_task(self._finish_arming(mode))

    async def _finish_arming(self, mode: ArmMode) -> None:
        """Background task scheduled by `arm()`. Waits out the exit
        delay, then transitions from `transitional` to `armed` and
        applies the mode's sensor bypass (vibration bypassed in Home
        mode, nothing bypassed in Away mode — see ArmMode). Runs as a
        cancellable asyncio.Task specifically so `disarm()` or
        `report_state_from_esp32()` can abort it if the system leaves
        `transitional` before the delay elapses.
        """
        await asyncio.sleep(ARMING_EXIT_DELAY.total_seconds())
        self.alarm_state = AlarmState.armed
        self.sensors = [
            SensorReading(
                type=s.type,
                label=s.label,
                ok=True,
                bypassed=(mode == ArmMode.home and s.type == SensorType.vibration),
            )
            for s in self.sensors
        ]
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
        # Back to the fail-safe default: if the next arm comes from the
        # physical keypad we never learn its mode, and "bypass nothing" is
        # the direction that can only cause a false alarm, never a missed
        # one. An app-initiated arm overwrites this in `arm()` anyway.
        self._arm_mode = ArmMode.away
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
        # The controller reports armed/disarmed but never *which mode*, so
        # the bypass set is recomputed from `_arm_mode` — the mode the last
        # /arm asked for — using the same rule as `_finish_arming`. This is
        # what makes Home mode hold when the controller's report, rather
        # than our own exit-delay task, is what completes the arm (it wins
        # that race whenever its countdown finishes first, and it's the
        # only path at all when someone arms at the physical keypad).
        # `disarm()` resets `_arm_mode` to `away`, so a keypad-initiated
        # arm we were never told the mode for bypasses nothing.
        self.sensors = [
            SensorReading(
                type=s.type,
                label=s.label,
                ok=True,
                bypassed=(
                    armed
                    and self._arm_mode == ArmMode.home
                    and s.type == SensorType.vibration
                ),
            )
            for s in self.sensors
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
        AppState.login (lockout), /trigger-alert (demo button), and
        /esp32/sensor-event (real sensor reports).
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
        await self._broadcast()

    # ---- Faces ----------------------------------------------------------------

    async def add_face(self, name: str) -> ApprovedFace:
        """Registers a new approved-face entry (id + name only — no photo
        yet; the caller uploads training photos separately via the
        FaceRecognitionService). id comes from `_face_id_counter`, so ids
        are simple ascending strings, not UUIDs.
        """
        face = ApprovedFace(id=str(next(self._face_id_counter)), name=name)
        self.faces.append(face)
        self._log(f"Approved face added: {name}", EventCategory.faceId)
        await self._broadcast()
        return face

    async def remove_face(self, face_id: str) -> None:
        """Removes an approved-face entry by id. No-ops quietly (no log
        entry, but still broadcasts) if the id doesn't exist, e.g. a
        double-delete race. Only updates this in-memory record — deleting
        the underlying trained photos is the caller's job (see
        FaceRecognitionService.remove_face, called separately in main.py).
        """
        face = next((f for f in self.faces if f.id == face_id), None)
        self.faces = [f for f in self.faces if f.id != face_id]
        if face:
            self._log(f"Approved face removed: {face.name}", EventCategory.faceId)
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
        await self._broadcast()

    # ---- Settings ---------------------------------------------------------------

    async def set_night_mode(self, enabled: bool) -> None:
        """Toggles night mode and logs it.

        The log message deliberately says only that the flag was turned
        on, not that anything auto-arms: no scheduling/auto-arm logic
        exists anywhere in this backend. The flag is stored and surfaced
        to the app, nothing more. (It used to claim "auto-arms overnight",
        which described behavior that was never implemented — a log line
        in a security system's audit trail shouldn't assert a protection
        the system doesn't actually provide.)
        """
        self.night_mode = enabled
        self._log(
            "Night mode enabled" if enabled else "Night mode disabled",
            EventCategory.info,
        )
        await self._broadcast()

    async def set_emergency_number(self, number: str) -> None:
        """Stores the number to call when police auto-dial (if/once
        implemented) would trigger. A plain setting — no validation and no
        log entry, but it does broadcast: `emergency_number` is part of
        FullState, so any *other* connected app instance would otherwise
        keep showing the stale number until it reconnected.
        """
        self.emergency_number = number
        await self._broadcast()

    async def set_police_call_delay(self, seconds: int) -> None:
        """Stores how long to wait after a triggered alarm before
        auto-calling `emergency_number`. Like set_emergency_number, this
        is currently just stored — and broadcast, for the same
        keep-other-clients-in-sync reason.
        """
        self.police_call_delay_seconds = seconds
        await self._broadcast()

    # ---- ESP32 / camera connectivity (set by esp32_client / camera worker) ----

    async def set_esp32_online(self, online: bool) -> None:
        """Updates the single shared "ESP32 online" flag shown in the
        app. Guarded to no-op when the value isn't actually changing, to
        avoid a broadcast storm from e.g. every single heartbeat call.
        NOTE: there are three separate ESP32 boards but only one online
        flag here — it doesn't distinguish which board(s) are actually
        reachable, so e.g. one board being down while others are up isn't
        visible to the app.
        """
        if online == self.esp32_online:
            return
        self.esp32_online = online
        await self._broadcast()

    async def set_camera_online(self, online: bool) -> None:
        """Updates the "camera online" flag shown in the app, set True by
        /camera/recognize whenever a frame comes in. Same no-op guard as
        set_esp32_online, and for the same reason.
        """
        if online == self.camera_online:
            return
        self.camera_online = online
        await self._broadcast()


state = AppState()
