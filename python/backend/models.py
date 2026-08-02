"""Pydantic models. These mirror the Flutter app's BackendService types
(app/lib/services/backend_service.dart) field-for-field and name-for-name
so the two sides stay in lockstep — if you rename something here, rename it
there too.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class AlarmState(str, Enum):
    """Overall system state shown in the app and driving alert logic.

    `transitional` is the exit-delay window between an arm request and the
    system actually becoming `armed` (see AppState._finish_arming) — it
    exists so the app can show a countdown instead of jumping straight to
    "armed" while the user is still walking out.
    """

    disarmed = "disarmed"
    transitional = "transitional"
    armed = "armed"
    triggered = "triggered"


class ArmMode(str, Enum):
    """Which arm profile was requested. `home` bypasses the vibration
    sensor (people moving around inside shouldn't trip it); `away`
    bypasses nothing. The actual bypass assignment happens in
    AppState._finish_arming, not here — this enum is just the selector.
    """

    away = "away"
    home = "home"


class EventCategory(str, Enum):
    """Tag on a SecurityEvent used by the app to pick an icon/filter for
    the activity log."""

    auth = "auth"
    armDisarm = "armDisarm"
    alert = "alert"
    faceId = "faceId"
    info = "info"


class SensorType(str, Enum):
    """Identifies which of the three ESP32 boards a sensor reading or
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


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    """Result of a login attempt. Lockout/attempts fields are populated
    on both success and failure so the app can always show the current
    lockout countdown or remaining-attempts count, not just on failure.
    """

    ok: bool
    locked_out: bool = False
    lockout_remaining_seconds: int = 0
    attempts_remaining: int = 0


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ArmRequest(BaseModel):
    mode: ArmMode


class TriggerAlertRequest(BaseModel):
    """Body for /trigger-alert (the demo "simulate intrusion" button).
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


class SetNightModeRequest(BaseModel):
    enabled: bool


class SetEmergencyNumberRequest(BaseModel):
    number: str


class SetPoliceCallDelayRequest(BaseModel):
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
    sensors: list[SensorReading]
    events: list[SecurityEvent]
    faces: list[ApprovedFace]
    esp32_online: bool
    camera_online: bool
    face_id_enabled: bool
    night_mode: bool
    emergency_number: str
    police_call_delay_seconds: int
