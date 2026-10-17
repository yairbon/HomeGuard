"""Pydantic models. These mirror the Flutter app's wire types in
app/lib/services/http_backend_service.dart (not the abstract
backend_service.dart, which contains no wire keys) field-for-field and
name-for-name so the two sides stay in lockstep — if you rename something
here, rename it there too.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class AlarmState(StrEnum):
    """Overall system state shown in the app and driving alert logic.

    `transitional` is the exit-delay window between an arm request and the
    system actually becoming `armed` (see AppState._finish_arming) — it
    exists so the app can show a countdown instead of jumping straight to
    "armed" while the user is still walking out.

    `entryDelay` is its mirror image: someone opened the door on an armed
    system and has a limited time to disarm before the alarm sounds. The door
    controller has always had this window (its ENTRY_DELAY state), but it only
    ever reported the *outcome*, so the app jumped straight from ARMED to
    TRIGGERED and never showed the countdown the person walking in is actually
    racing.
    """

    disarmed = "disarmed"
    transitional = "transitional"
    armed = "armed"
    entryDelay = "entryDelay"
    triggered = "triggered"


class ArmMode(StrEnum):
    """Which arm profile was requested. `home` bypasses the vibration
    sensor (people moving around inside shouldn't trip it); `away`
    bypasses nothing. The actual bypass assignment happens in
    AppState._finish_arming, not here — this enum is just the selector.
    """

    away = "away"
    home = "home"


class EventCategory(StrEnum):
    """Tag on a SecurityEvent used by the app to pick an icon/filter for
    the activity log."""

    auth = "auth"
    armDisarm = "armDisarm"
    alert = "alert"
    faceId = "faceId"
    info = "info"


class SensorType(StrEnum):
    """Identifies which of the three sensor boards a sensor reading or
    event refers to."""

    ultrasonic = "ultrasonic"
    door = "door"
    vibration = "vibration"


class SecurityEvent(BaseModel):
    """One entry in the activity/event log shown in the app."""

    timestamp: datetime
    message: str
    category: EventCategory = EventCategory.info


class SensorReading(BaseModel):
    """Current status of one sensor, as shown on the app's dashboard.

    `bypassed` means the sensor is intentionally being ignored by
    trigger_alert (e.g. vibration while armed Home) — it's still reported
    to the app as `ok`, just marked so the UI can explain why it won't
    raise an alert.
    """

    type: SensorType
    label: str
    ok: bool
    bypassed: bool = False


class ApprovedFace(BaseModel):
    """A person whose face unlocks/disarms via face recognition."""

    id: str
    name: str


class BoardStatus(BaseModel):
    """Liveness of one of the three heartbeating ESP32 boards, derived from
    when it last called `/esp32/heartbeat`. (The fourth board, the ESP32-CAM,
    doesn't heartbeat — it reports through `camera_online` instead.)

    Exists because a single aggregate `esp32_online` flag couldn't express
    "the vibration board is dead but the other two are fine" — and, worse,
    the old flag latched on: once any board had ever checked in, nothing
    ever set it back to False, so a board could lose power and the app would
    keep showing it as online indefinitely.

    `last_seen` is None for a board that hasn't reported at all since the
    backend started, which reads differently in the UI from a board that was
    online and went quiet.
    """

    name: str
    online: bool
    last_seen: datetime | None = None


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    """Result of a login attempt. Lockout/attempts fields are populated
    on both success and failure so the app can always show the current
    lockout countdown or remaining-attempts count, not just on failure.

    `token` is the session token the app must send as
    `Authorization: Bearer <token>` on every subsequent request — it is the
    entire point of logging in. None on failure, so a caller that didn't
    authenticate has nothing to send and every protected route will reject it.
    """

    ok: bool
    token: str | None = None
    locked_out: bool = False
    lockout_remaining_seconds: int = 0
    attempts_remaining: int = 0


class ChangePasswordRequest(BaseModel):
    """Body for /change-password — changes the single shared secret that is
    both this app's login password and the physical keypad's disarm PIN.

    These were originally two independent credentials, each with its own
    endpoint (this one, and a since-removed /settings/keypad-pin). Unified
    at the project owner's explicit request; see AppState.change_password
    for the full account, including the security trade-off that merge
    accepts — the app password used to be free-form, and now it is bound by
    the keypad's stricter shape.

    `new_password` must be 4-8 digits, the one shape a 4x4 physical keypad
    can type, which is now the binding constraint on *both* credentials, not
    just the keypad's. Enforced in AppState.change_password
    (`is_valid_keypad_pin`) rather than as a Pydantic field constraint here,
    so a violation gets a 422 with a specific reason instead of FastAPI's
    generic body-validation error. `old_password` carries no such
    constraint — it is whatever the previous value was, checked the same way
    `/login` checks it.
    """

    old_password: str
    new_password: str


class ArmRequest(BaseModel):
    mode: ArmMode


class TriggerAlertRequest(BaseModel):
    """Body for /trigger-alert (a manual alarm trigger).
    `source` is optional because not every alert is tied to a specific
    sensor — e.g. the 3-failed-logins alert (see AppState.login) passes
    source=None since it's a keypad/auth event, not a sensor report.
    """

    source: SensorType | None = None
    reason: str


class AddFaceRequest(BaseModel):
    name: str


class SetFaceIdEnabledRequest(BaseModel):
    enabled: bool


class SetFaceDisarmEnabledRequest(BaseModel):
    """Body for /face-disarm-enabled. Separate from SetFaceIdEnabledRequest
    because the two toggles mean very different things: face *ID* is whether
    recognition runs at all, face *disarm* is whether a match is allowed to
    turn the alarm off."""

    enabled: bool


class SetNightModeRequest(BaseModel):
    enabled: bool


class SetAlertEmailsRequest(BaseModel):
    """Body for /settings/alert-emails: who is emailed when an alarm triggers
    (`alert_to`), and who is added if it is still not cleared after the delay
    (`escalation_to`, the emergency contacts). Each may be empty."""

    alert_to: list[str] = []
    escalation_to: list[str] = []


class SetAlertsEnabledRequest(BaseModel):
    """Body for /settings/alerts-enabled: the master switch for messages that
    leave the house (email, WhatsApp, ntfy)."""

    enabled: bool


class SetNightModeHoursRequest(BaseModel):
    """When night mode arms and disarms, as whole hours (0-23). Clamped
    server-side rather than validated here, so an out-of-range value from an
    older client degrades instead of 422-ing."""

    arm_hour: int
    disarm_hour: int


class SetEmergencyNumberRequest(BaseModel):
    number: str


class SetPoliceCallDelayRequest(BaseModel):
    seconds: int


class SetNtfyRequest(BaseModel):
    """Body for /settings/ntfy — where to push alerts so they arrive even
    when the app isn't running. See backend/notifier.py.

    `server` defaults to the public ntfy.sh so the feature works with nothing
    but a topic name; point it at a self-hosted instance to keep alerts off a
    third-party server. An empty `topic` disables pushing entirely.
    """

    server: str = "https://ntfy.sh"
    topic: str = ""


class SetEntryDelayRequest(BaseModel):
    """Body the door controller POSTs to /esp32/entry-delay the moment it
    detects the door opening on an armed system.

    `seconds` is how long its own countdown will run before it sounds the
    alarm, so the app can mirror that number rather than hardcoding a guess
    that would drift if the sketch's ENTRY_TIME_LIMIT ever changed.
    """

    seconds: int


class Esp32StateChangeRequest(BaseModel):
    """Body the door/keypad controller POSTs to /esp32/state-change
    whenever its own armed/disarmed state changes (from the physical
    keypad or from relaying our /arm /disarm). It is the sole source of
    truth for `armed`; see AppState.report_state_from_esp32.
    """

    armed: bool


class FullState(BaseModel):
    """Snapshot pushed over the WebSocket on connect and after any change."""

    alarm_state: AlarmState
    # Seconds left to disarm before the alarm sounds. Only non-zero while
    # `alarm_state` is `entryDelay`; the door controller owns the real timer,
    # this mirrors it so the app can show a countdown.
    entry_delay_seconds_remaining: int = 0
    sensors: list[SensorReading]
    events: list[SecurityEvent]
    faces: list[ApprovedFace]
    # Aggregate "is any board reachable", kept for the app's single
    # connectivity badge; `boards` carries the per-board detail behind it.
    esp32_online: bool
    boards: list[BoardStatus]
    camera_online: bool
    face_id_enabled: bool
    # Whether a recognized face disarms the system. Off by default — even
    # with the landmark-motion + anti-spoofing liveness checks, this stays
    # convenience, not security.
    face_disarm_enabled: bool
    night_mode: bool
    # Whole hours (0-23) at which night mode arms and disarms.
    night_mode_arm_hour: int
    night_mode_disarm_hour: int
    # Master switch for alerts that leave the house (email, WhatsApp, ntfy).
    # Off means an alarm still sounds and shows in the app, it just sends no
    # message — for testing without flooding an inbox.
    alerts_enabled: bool
    # Who the alert emails go to; edited from the app. The sending account and
    # its password stay in the local alerts file, never here.
    alert_emails: list[str]
    escalation_emails: list[str]
    emergency_number: str
    police_call_delay_seconds: int
    # Where alerts are pushed so they arrive with the app closed. The topic
    # name doubles as the only access control on a public ntfy server, so
    # treat it as a secret — see backend/notifier.py.
    ntfy_server: str
    ntfy_topic: str
