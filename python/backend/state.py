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
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

from fastapi import WebSocket

from . import alerts, clip, notifier, secrets_file
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

# How long past the door controller's own entry-delay deadline the backend
# waits before raising the alarm itself. See `_entry_delay_watchdog`: the
# controller is meant to report the outcome, and this is the fallback for when
# it can't. Small, but not zero — the board's report has to cross a WiFi hop,
# and beating it to the punch would mean logging an alarm for an entry that
# was in fact disarmed a few hundred milliseconds earlier.
ENTRY_DELAY_GRACE = timedelta(seconds=5)

# Bounds on the "alarm not cleared" escalation delay. The lower bound is the
# real one: `_escalate_after_delay` sleeps this many seconds, and
# `asyncio.sleep()` with a negative argument returns *immediately*, so a
# negative setting fired the "nobody has dealt with this alarm" escalation in
# the same breath as the alarm itself. The upper bound is just a sanity cap —
# an escalation an hour later is already well past useful.
MIN_POLICE_CALL_DELAY = 0
MAX_POLICE_CALL_DELAY = 3600

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

# Names the three heartbeating sketches identify themselves with on
# /esp32/heartbeat. (There are four boards; the fourth, the ESP32-CAM, only
# pushes frames to /camera/recognize and never heartbeats.) Kept as plain
# strings (not a SensorType) because the door controller is not a sensor —
# it's the arm/disarm authority that also happens to own the door reed switch.
ALL_BOARD_NAMES = ("ultrasonic", "vibration", "door_controller")


def expected_boards(raw: str) -> tuple[str, ...]:
    """Parses `HOMEGUARD_BOARDS` into the boards this install expects to hear from.

    The ultrasonic sketch also carries the vibration (force) sensor, so an
    install with both sensors wired to one ESP32 has no board that will ever
    heartbeat as `vibration`. Left in the list it would show as permanently
    offline, and the app raises a "SENSOR OFFLINE" banner for it. That setup
    is `HOMEGUARD_BOARDS=ultrasonic,door_controller`.

    Unset or empty means every board, and that is the deliberate default: a
    board that is expected but never comes up is exactly the failure the
    offline banner exists to show, so dropping one is something an install
    opts into rather than something a typo can do. Unknown names are ignored
    with a warning, and a value with no valid name falls back to every board
    for the same reason. Order is always [ALL_BOARD_NAMES]'s, however the
    variable lists them.
    """
    wanted = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = sorted(wanted - set(ALL_BOARD_NAMES))
    if unknown:
        logger.warning("HOMEGUARD_BOARDS: ignoring unknown board name(s): %s", ", ".join(unknown))
    return tuple(name for name in ALL_BOARD_NAMES if name in wanted) or ALL_BOARD_NAMES


# The boards this install expects heartbeats from — what `/state` reports, and
# the allow-list for `note_board_seen`: `board` arrives as a query string
# from the network, so without this check `_board_last_seen` was a dict an
# authenticated-but-buggy (or hostile) caller could grow without bound, one
# key per name it invented.
BOARD_NAMES = expected_boards(os.environ.get("HOMEGUARD_BOARDS", ""))

# Which board owns each sensor, for reports that don't name one. Only the door
# is a surprise: the door reed switch hangs off the *controller*, so a door
# event means "door_controller was heard from", not a board called "door".
# `/esp32/sensor-event` used to fall back to the sensor's own name, which for
# the door meant recording liveness under a key nothing ever reads — the
# fallback silently marked nothing online at all.
BOARD_FOR_SENSOR = {
    SensorType.ultrasonic: "ultrasonic",
    SensorType.vibration: "vibration",
    SensorType.door: "door_controller",
}

# How long to wait before logging the same recognised face again. Chosen to be
# long enough that a person lingering in front of the camera produces one entry
# rather than one every 500 ms, and short enough that genuinely separate
# arrivals are still recorded separately.
FACE_LOG_COOLDOWN = timedelta(seconds=60)

# Keeping every event forever would grow without bound in a process meant to
# run for weeks. The app's log screen only ever shows a scrollback anyway.
MAX_EVENTS = 200

# Bounds on the physical keypad's disarm PIN. Digits only, because the 3x4
# matrix's other two keys are '*' (arm) and '#' (submit) — a PIN containing
# either could never be typed. The same rule is enforced in the door
# controller sketch's isValidPin(); both sides check, because the board's
# /keypad-pin route is reachable by anything on the network holding the
# device key and so cannot assume the backend validated first.
#
# Also, since AppState.change_password merged the login password and the
# keypad PIN into one shared secret, these bounds now gate *both*: the app
# password used to accept any length or character set, and now can't be set
# to anything the keypad couldn't also type. See that method's docstring for
# the trade-off that accepts.
KEYPAD_PIN_MIN_LENGTH = 4
KEYPAD_PIN_MAX_LENGTH = 8


def is_valid_keypad_pin(pin: object) -> bool:
    """True for a value the keypad can actually accept as a PIN — which,
    since AppState.change_password unified the two, is now also the only
    shape a new login password may take.

    Deliberately strict rather than permissive. Beyond being a usability
    rule, this is the sanitizer standing between an app-supplied string and
    two places that string reaches: an HTTP body sent to the door controller,
    and — when secrets-file syncing is switched on — a `#define` written into
    a C++ header that gets compiled into firmware. A PIN restricted to digits
    cannot carry a quote, a newline, or a preprocessor directive into either.
    """
    return (
        isinstance(pin, str)
        and KEYPAD_PIN_MIN_LENGTH <= len(pin) <= KEYPAD_PIN_MAX_LENGTH
        and pin.isdigit()
        # `str.isdigit()` is True for superscripts and other Unicode digit
        # forms ('²', '٣'); the keypad emits ASCII, and so must this.
        and pin.isascii()
    )


_T = TypeVar("_T")


def _scalar(data: dict, key: str, default: _T) -> _T:
    """`data.get(key, default)` that also rejects a value of the wrong type.

    `state.json` is a plain file a user may well have opened and edited, and
    plain `.get(key, default)` only defends against a *missing* key: a present
    one holding `null` came back as `None` and was assigned straight onto the
    attribute. That loaded fine and then failed somewhere else entirely — a
    single `"emergency_number": null` made `snapshot()` raise a Pydantic
    ValidationError on the first `/state` call and on every WebSocket connect,
    so the app could display nothing at all, with a traceback pointing at the
    model rather than at the file that caused it.
    """
    value = data.get(key, default)
    if isinstance(default, bool):
        # Checked before the int branch below, because `bool` *is* an `int` in
        # Python and `isinstance(True, int)` is True — without this order,
        # `"night_mode": 1` would be accepted for a bool field and
        # `"police_call_delay_seconds": true` would be accepted as 1 second.
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    return value if isinstance(value, type(default)) else default


def _clamp_police_call_delay(seconds: int) -> int:
    """Keeps the escalation delay inside [MIN_POLICE_CALL_DELAY,
    MAX_POLICE_CALL_DELAY]. Applied both when the app sets it and when it is
    read back off disk, since a hand-edited file reaches
    `_escalate_after_delay` by exactly the same route the setter does.
    """
    return max(MIN_POLICE_CALL_DELAY, min(MAX_POLICE_CALL_DELAY, seconds))


# The factory-default login password on a fresh install. Predates the
# digit-only constraint change_password now enforces on any *new* value (see
# that method), and is grandfathered in as a valid *old* one — /login never
# validated password shape and still doesn't. It can be verified against
# forever, it just can never be set again once changed away from: the first
# change_password call moves the account permanently into the digits-only
# regime, same as a fresh keypad PIN always was.
DEFAULT_PASSWORD = "homeguard"
STATE_FILE = Path(
    os.environ.get("HOMEGUARD_STATE_FILE", Path(__file__).resolve().parent.parent / "state.json")
)



def _clamp_clip_seconds(value) -> int:
    """The video length setting as a whole number of seconds between 5 and 60."""
    try:
        return max(5, min(int(clip.CLIP_SECONDS), int(value)))
    except (TypeError, ValueError):
        return int(clip.CLIP_SECONDS)


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
        password (the door keypad's PIN when `HOMEGUARD_SECRETS_H` points at
        its secrets.h, else "homeguard") and no approved faces.
        """
        # Left empty on purpose — the default password is hashed in [_load],
        # and only if nothing was persisted. scrypt is deliberately expensive
        # (~50ms, see security.py), and every install that has ever been
        # started overwrites this value a few lines later, so hashing it here
        # burned that 50ms on every single process start to produce a string
        # nobody would ever read. `verify_password` treats "" as a malformed
        # hash and refuses every login, so a code path that somehow skipped
        # `_load` fails closed rather than open.
        self._password_hash = ""
        self.alarm_state = AlarmState.disarmed
        self.sensors: list[SensorReading] = [
            SensorReading(type=SensorType.ultrasonic, label="Ultrasonic sensor", ok=True),
            SensorReading(type=SensorType.door, label="Door sensor", ok=True),
            SensorReading(type=SensorType.vibration, label="Vibration sensor", ok=True),
        ]
        self.events: list[SecurityEvent] = []
        # A fresh install has nobody approved: the people who may disarm by
        # face are enrolled from the app. (It used to ship two hardcoded
        # developer names with no photos, which showed up as approved people
        # who could never match anything.)
        self.faces: list[ApprovedFace] = []
        self._next_face_id = 1
        # When the last camera frame arrived. `camera_online` is *derived*
        # from this rather than being a stored flag, for exactly the reason
        # the per-board `esp32_online` flag was replaced by
        # `_board_last_seen`: a boolean set to True by an arriving frame has
        # nothing to set it back. The ESP32-CAM sends no heartbeat (by
        # design — see esp32/README.md), so frames are the only liveness
        # signal there is, and a camera that lost power kept the app showing
        # a live badge over a frozen last frame indefinitely.
        self._camera_last_frame_at: datetime | None = None
        # Last time each recognised face was written to the event log, for the
        # per-person rate limit in `log_face_recognized`. Not persisted: it is
        # a debouncing detail, and a restart logging one extra sighting is
        # harmless.
        self._face_last_logged: dict[str, datetime] = {}
        self.face_id_enabled = True
        # Off by default and deliberately so — see log_face_recognized for
        # why letting a face turn the alarm off is a security trade-off.
        self.face_disarm_enabled = False
        self.night_mode = False
        # Master switch for messages that leave the house; see FullState.
        self.alerts_enabled = True
        # The video clip in the breach email: on/off and how many seconds (5-60).
        self.clip_enabled = True
        self.clip_seconds = 60
        # Who the alert emails go to, set from the app. On the first start after
        # this existed they are carried over from the local alerts file (see
        # `_load`), so nobody has to type them twice.
        self.alert_emails: list[str] = []
        self.escalation_emails: list[str] = []
        self._pin_resync_task: asyncio.Task | None = None
        # Returns the newest camera frame (JPEG bytes) or None. Set by main.py,
        # which owns the frame cache; used to attach a picture to alarm emails.
        self.camera_frame_source: Callable[[], bytes | None] | None = None
        # The last minute of camera frames as (unix time, JPEG), for the video
        # clip in the alarm email. In memory only: a restart starts it empty.
        self.clip_frames: deque[tuple[float, bytes]] = deque(maxlen=clip.MAX_FRAMES)
        self._stream_frame_at = 0.0  # when the live stream last delivered a frame
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
        # The physical keypad's disarm PIN, when it has been set from the app.
        # Empty means "never set here" — the board is then running whatever
        # KEYPAD_PIN was compiled into its secrets.h, which the backend has no
        # way to know. Stored as the literal PIN rather than a hash — unlike
        # `_password_hash`, the backend does not *verify* this value: it
        # relays it to the door controller and, optionally, secrets.h,
        # neither of which can compare against a scrypt digest. That also
        # means it has to survive a restart, so a re-send after the board
        # comes back online sends the right PIN. It sits in state.json next
        # to the device key, which is likewise a shared secret held in the
        # clear (see security.py).
        #
        # Since AppState.change_password unified the login password and the
        # keypad PIN into one shared secret, this is always kept equal to
        # the plaintext of whatever `_password_hash` currently verifies —
        # two representations of the same value, not two secrets. It is
        # still its own field rather than derived from the hash because a
        # hash cannot be reversed back into the plaintext this needs to send
        # over the wire.
        self.keypad_pin = ""

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
        # Fallback timer for an entry delay the door controller never reports
        # the outcome of; see [_entry_delay_watchdog].
        self._entry_delay_task: asyncio.Task | None = None
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

        Every field is read defensively through [_scalar], because this file
        is plain JSON on disk that a user may well have opened and edited. A
        malformed or partial file degrades to defaults rather than preventing
        the backend from starting — an alarm system that won't boot because
        its config file has a typo is worse than one that boots with a default
        password and says so.

        That promise used to hold only for files that failed to *parse*. Two
        kinds of valid JSON still broke it: a non-object at the top level
        (`[]`, `null`, `"hello"`) went straight into `data.get(...)` and raised
        AttributeError out of `__init__`, so the backend did not start at all;
        and a present-but-null scalar was assigned as `None`, which loaded
        quietly and then made `snapshot()` raise on the first `/state` request.
        Both are handled below.
        """
        data: object = {}
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Could not read state file %s (%s) — using defaults.", STATE_FILE, exc
                )
        if not isinstance(data, dict):
            logger.warning(
                "State file %s is valid JSON but not an object (%s) — using defaults.",
                STATE_FILE,
                type(data).__name__,
            )
            data = {}

        # The one place the first password is hashed, and only on an install
        # that has never been started; see the note in `__init__`. It starts
        # as the door keypad's own PIN when the backend can read it from the
        # board's secrets.h, so app password and PIN are one secret from the
        # first minute rather than after the first password change; without
        # that file it falls back to the well-known factory default.
        stored_hash = _scalar(data, "password_hash", "")
        first_run_pin = ""
        if stored_hash:
            self._password_hash = stored_hash
        else:
            candidate = secrets_file.read_keypad_pin()
            if is_valid_keypad_pin(candidate):
                first_run_pin = candidate
                self._password_hash = hash_password(candidate)
            else:
                self._password_hash = hash_password(DEFAULT_PASSWORD)
        self.face_id_enabled = _scalar(data, "face_id_enabled", self.face_id_enabled)
        self.face_disarm_enabled = _scalar(data, "face_disarm_enabled", self.face_disarm_enabled)
        self.night_mode = _scalar(data, "night_mode", self.night_mode)
        self.alerts_enabled = _scalar(data, "alerts_enabled", self.alerts_enabled)
        self.clip_enabled = _scalar(data, "clip_enabled", self.clip_enabled)
        self.clip_seconds = _clamp_clip_seconds(_scalar(data, "clip_seconds", self.clip_seconds))
        stored_alert, stored_escalation = data.get("alert_emails"), data.get("escalation_emails")
        if isinstance(stored_alert, list) or isinstance(stored_escalation, list):
            self.alert_emails = list(alerts.parse_addresses(stored_alert)[0])
            self.escalation_emails = list(alerts.parse_addresses(stored_escalation)[0])
        else:
            # First start with recipients living here: carry over what the local
            # alerts file already says, once; from then on the app owns them.
            seed = alerts.load_config().email
            if seed is not None:
                self.alert_emails = list(seed.alert_to)
                self.escalation_emails = list(seed.escalation_to)
        # Clamped on the way in as well as in the setters: a hand-edited hour
        # of 99 gives a night-mode schedule that can never fire, and a negative
        # escalation delay fires it instantly (see MIN_POLICE_CALL_DELAY).
        self.night_mode_arm_hour = max(
            0, min(23, _scalar(data, "night_mode_arm_hour", self.night_mode_arm_hour))
        )
        self.night_mode_disarm_hour = max(
            0, min(23, _scalar(data, "night_mode_disarm_hour", self.night_mode_disarm_hour))
        )
        self.emergency_number = _scalar(data, "emergency_number", self.emergency_number)
        self.police_call_delay_seconds = _clamp_police_call_delay(
            _scalar(data, "police_call_delay_seconds", self.police_call_delay_seconds)
        )
        self.ntfy_server = _scalar(data, "ntfy_server", "") or self.ntfy_server
        self.ntfy_topic = _scalar(data, "ntfy_topic", self.ntfy_topic)
        # Re-validated on load rather than trusted: this is the one persisted
        # field that gets relayed to hardware, and state.json is a plain file
        # a user may well have opened and edited.
        stored_pin = data.get("keypad_pin", "")
        self.keypad_pin = stored_pin if is_valid_keypad_pin(stored_pin) else first_run_pin

        faces = data.get("faces")
        if isinstance(faces, list):
            try:
                self.faces = [ApprovedFace(**f) for f in faces]
            except (TypeError, ValueError) as exc:
                logger.warning("Persisted faces are malformed (%s) — starting with none.", exc)

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

        # `_scalar` rather than `.get`, because a non-string here would be
        # handed to `hmac.compare_digest` on every /esp32/* request, which
        # raises TypeError — every board locked out, with a 500 apiece.
        self.device_key = resolve_device_key(_scalar(data, "device_key", "") or None)
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
            # `model_dump(mode="json")`, not `json.loads(model_dump_json())`:
            # the latter serialized every event to a JSON string only to parse
            # it straight back into the dict needed here, and then serialized
            # the lot again below. `_persist` runs on the alarm path, so that
            # doubled work was being paid at the worst possible moment.
            "events": [e.model_dump(mode="json") for e in self.events[:MAX_EVENTS]],
            "face_id_enabled": self.face_id_enabled,
            "face_disarm_enabled": self.face_disarm_enabled,
            "night_mode": self.night_mode,
            "alerts_enabled": self.alerts_enabled,
            "clip_enabled": self.clip_enabled,
            "clip_seconds": self.clip_seconds,
            "alert_emails": self.alert_emails,
            "escalation_emails": self.escalation_emails,
            "night_mode_arm_hour": self.night_mode_arm_hour,
            "night_mode_disarm_hour": self.night_mode_disarm_hour,
            "emergency_number": self.emergency_number,
            "police_call_delay_seconds": self.police_call_delay_seconds,
            "ntfy_server": self.ntfy_server,
            "ntfy_topic": self.ntfy_topic,
            "keypad_pin": self.keypad_pin,
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = STATE_FILE.with_suffix(".json.tmp")
            # Not pretty-printed. Indenting a 200-entry event log costs both
            # time and a quarter more bytes on a file that is rewritten on
            # every settings change and every alert; `_load` parses either
            # form identically, and anyone reading it by hand can pipe it
            # through a formatter.
            temp.write_text(json.dumps(payload), encoding="utf-8")
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
        # Iterate a snapshot of the set, not the set itself. `send_text`
        # suspends whenever the peer's write buffer is full — routine for a
        # backgrounded phone or a Tailscale hop — and while it is suspended
        # another task can register() a newly-connected app or unregister()
        # one that just dropped. Iterating the live set through that window
        # raises `RuntimeError: Set changed size during iteration`, which is
        # not caught by the `except` below (it wraps only the send) and so
        # escapes into whatever called us: the alarm frame is lost for every
        # socket after the mutation point, `/esp32/sensor-event` 500s back at
        # the board, and the same escape from `_monitor_boards` kills board
        # liveness for the rest of the process.
        for ws in list(self._sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                # A send failure means that socket is gone (client closed,
                # network drop, etc). Broad except because the exact
                # exception type varies by transport/ASGI server; any
                # failure here just means "drop this socket", not a bug
                # to propagate.
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)

    # ---- Board liveness ------------------------------------------------------

    @property
    def camera_online(self) -> bool:
        """Whether a camera has sent a frame recently enough to count as
        live. Uses the same window as the boards' heartbeats, so "gone
        quiet" means the same thing everywhere in the UI."""
        if self._camera_last_frame_at is None:
            return False
        return datetime.now() - self._camera_last_frame_at < BOARD_TIMEOUT

    def board_statuses(self) -> list[BoardStatus]:
        """Current online/offline status of each board in [BOARD_NAMES],
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

        Unknown names are ignored rather than recorded. `board` is a query
        string off the network, and `_board_last_seen` is a dict that nothing
        ever prunes, so accepting arbitrary keys meant an authenticated caller
        (a mistyped `BOARD_NAME` in a sketch is enough) could grow it without
        bound in a process meant to run for weeks — and none of those keys
        could ever be read back, since `board_statuses` only ever looks up
        [BOARD_NAMES].
        """
        if board not in BOARD_NAMES:
            logger.warning("Ignoring contact from unknown board %r", board)
            return

        was_online = self.esp32_online
        known = board in self._board_last_seen
        previously = (
            known and datetime.now() - self._board_last_seen[board] < BOARD_TIMEOUT
        )
        self._board_last_seen[board] = datetime.now()
        if board == "door_controller" and not previously and self.keypad_pin:
            # The keypad board has just (re)appeared, or this backend just
            # started. A PIN changed while it was unplugged never reached it, so
            # the keypad would still be on the old code while the app says the
            # new one: say it again. Harmless when it already matches.
            self._pin_resync_task = asyncio.create_task(self._resync_keypad_pin())
        if not previously or was_online != self.esp32_online:
            await self._broadcast()

    async def _resync_keypad_pin(self) -> None:
        """Pushes the current PIN to the door controller (see `note_board_seen`)."""
        from . import esp32_client  # deferred: esp32_client imports `state`

        if await esp32_client.send_keypad_pin(self.keypad_pin):
            logger.info("Keypad PIN re-sent to the door controller.")

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
            try:
                current = [(b.name, b.online) for b in self.board_statuses()]
                if current != previous:
                    # strict=True: both lists come from board_statuses(), so a
                    # length mismatch would mean the board set changed under
                    # us — worth raising rather than silently truncating the
                    # log.
                    for (name, was), (_, now_online) in zip(previous, current, strict=True):
                        if was and not now_online:
                            self._log(f"Board offline: {name}", EventCategory.info)
                        elif not was and now_online:
                            self._log(f"Board online: {name}", EventCategory.info)
                    previous = current
                    await self._broadcast()
            except Exception:
                # Same guard, and for the same reason, as `_night_mode_loop`.
                # A single raise here used to end the task permanently, and
                # `start_monitor` only creates one when `_monitor_task is
                # None` — a *dead* task is not None, so nothing would ever
                # restart it. Board liveness would then freeze for the rest of
                # the process: every board stuck at whatever it last was, a
                # dead one still showing ONLINE in the app, and not even a log
                # line at the moment it happened.
                logger.exception("Board liveness check failed")

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
        """Cancels every background task on shutdown and waits for each to
        actually finish, so none can outlive the app and log into a closing
        event loop.

        The list is not just the two loops. `_arming_task`,
        `_escalation_task` and `_entry_delay_task` are one-shot timers, but a
        shutdown lands mid-countdown often enough (restarting the backend
        while the house is arming is the normal way to deploy a change), and a
        pending one would wake up on a loop that is already closing —
        "Task was destroyed but it is pending!" at best, and at worst a
        `trigger_alert` broadcasting into torn-down WebSockets.
        """
        for name in (
            "_monitor_task",
            "_night_mode_task",
            "_arming_task",
            "_escalation_task",
            "_entry_delay_task",
        ):
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
            alerts_enabled=self.alerts_enabled,
            clip_enabled=self.clip_enabled,
            clip_seconds=self.clip_seconds,
            alert_emails=self.alert_emails,
            escalation_emails=self.escalation_emails,
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
        # Also on the console: the backend's window then tells the story as it
        # happens (armed, door opened, alarm, email sent) instead of a wall of
        # routine HTTP lines. Event messages never carry a secret by design (see
        # set_ntfy and set_alert_emails).
        logger.info("[%s] %s", category.value, message)

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
        """Resets the failed-attempt counter once a lockout has run out.

        Called at the top of every password check. `is_locked_out` is derived
        purely from `_locked_until` elapsing, which says nothing about the
        counter — so without this the user serves the 30 seconds and then
        starts their next attempt already at the limit. Deliberately a no-op
        while a lockout is still in effect, so it can't be used to reset the
        counter early.
        """
        if self._locked_until is not None and not self.is_locked_out:
            self._locked_until = None
            self._failed_attempts = 0

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

        # A lockout that has elapsed must also clear the counter that caused
        # it. Previously only a *successful* login reset `_failed_attempts`,
        # so after the 30 seconds expired the counter was still latched at
        # MAX_LOGIN_ATTEMPTS: the next single typo hit `>= MAX` immediately,
        # re-locked for another 30 seconds, and — because
        # `_register_failed_attempt` escalates — fired `trigger_alert` on an
        # armed house. One mistyped character, full siren. It also made
        # `attempts_remaining` report 0 alongside `locked_out: false`, an
        # incoherent pair the app rendered as "0 attempts left" on a system
        # that was in fact accepting logins.
        self._clear_expired_lockout()

        # Off the event loop: scrypt is tuned to take ~50ms (see security.py),
        # and this runs on the one loop that also serves every connected
        # WebSocket. Blocking it means the alarm's real-time state push stops
        # for the duration — for one login that is barely visible, but the
        # lockout only starts after three attempts, so this is by design a
        # path someone can make the backend walk repeatedly.
        if not await asyncio.to_thread(verify_password, password, self._password_hash):
            await self._register_failed_attempt(f"{MAX_LOGIN_ATTEMPTS} failed login attempts")
            return None

        self._failed_attempts = 0
        self._log("Logged in", EventCategory.auth)
        await self._broadcast()
        return self.tokens.issue()

    async def change_password(
        self, old_password: str, new_password: str, current_token: str | None = None
    ) -> tuple[bool, bool, bool]:
        """Changes the single shared secret that is both this app's login
        password and the physical keypad's disarm PIN.

        These were originally two independent credentials — a free-form,
        scrypt-hashed app password here, and a separately-set 4-8 digit
        keypad PIN behind the now-removed `set_keypad_pin`/
        `/settings/keypad-pin` — unified into one at the project owner's
        explicit request. **This is a real security trade-off, not just a
        UX simplification, and it is worth stating plainly rather than
        leaving it implicit in the diff**: the app password used to be able
        to be any length or character set; the keypad PIN was always
        constrained to [KEYPAD_PIN_MIN_LENGTH, KEYPAD_PIN_MAX_LENGTH] digits,
        because that is all a 4x4 physical keypad can type. Unifying the two
        means the *weaker* shape wins for both — the one value that now also
        unlocks remote app access (arm/disarm, the live camera feed, every
        setting), not just the front door, lives in a space of at most
        10**KEYPAD_PIN_MAX_LENGTH digit combinations, rather than an
        arbitrary-strength password. Accepted knowingly for this household's
        own system; worth re-weighing if this backend is ever reachable from
        somewhere less trusted than a private Tailnet.

        Checks `old_password` first — unconstrained in shape, since it's
        just whatever the previous value already was (digit PIN, or the
        original free-form default; see DEFAULT_PASSWORD) — through the same
        counted/lockout path as a failed login (see
        [_register_failed_attempt]). Only once that succeeds is
        `new_password` checked against [is_valid_keypad_pin] and rejected
        with `ValueError` (the route turns that into a 422) if it isn't
        4-8 digits. Checking the old value's *correctness* before the new
        value's *shape* means three wrong old-value guesses still lock the
        account out no matter what `new_password` looks like — a caller
        can't dodge the lockout by pairing each guess with a deliberately
        malformed new value.

        On success:
        - `_password_hash` is replaced (what `/login` compares against), and
          `keypad_pin` is set to the same plaintext value — see the comment
          on that field for why it has to be stored separately from the hash.
        - every *other* session token is revoked — `current_token` (the
          device that made this change) keeps working, and every other
          signed-in device is forced to re-authenticate with the new value.
          Unchanged from this method's pre-merge behavior, and just as
          necessary now: the whole point of changing the credential that
          guards an alarm system is that a lost/stolen/ex-housemate's
          session stops working.
        - persisted to disk **before** the door controller is contacted —
          same reasoning `set_keypad_pin` used to state explicitly: a crash
          or an unreachable board during that round trip leaves the backend
          holding the new value rather than losing it, so a later re-send
          is possible instead of the change silently vanishing.
        - pushed to the door controller and, if `HOMEGUARD_SECRETS_H` is
          configured, synced into secrets.h. Both are best-effort and
          reported back rather than required for `ok=True`: the credential
          itself is already changed the moment it's verified and stored
          here, independent of whether either hardware round trip lands.

        Returns `(ok, hardware_ack, secrets_synced)`. `ok=False` means
        nothing changed anywhere — wrong `old_password`, or locked out —
        and `hardware_ack`/`secrets_synced` are always `False` alongside it,
        since nothing was there to push. When `ok=True`, `hardware_ack` says
        whether the door controller acknowledged the new PIN and
        `secrets_synced` whether secrets.h was rewritten to match; see
        `esp32_client.send_keypad_pin` and `secrets_file.update_keypad_pin`.
        Logged either way, but the value itself never enters the event log —
        the same rule the ntfy topic follows — only the fact of the change
        and the hardware outcome are.
        """
        if self.is_locked_out:
            return False, False, False
        self._clear_expired_lockout()
        # Both scrypt calls run in a worker thread; see `login` for why. This
        # one is the more expensive of the two — a verify plus a fresh hash is
        # ~150ms of solid CPU, which is a long time to hold an event loop that
        # is also responsible for pushing an alarm.
        if not await asyncio.to_thread(verify_password, old_password, self._password_hash):
            await self._register_failed_attempt(
                f"{MAX_LOGIN_ATTEMPTS} failed password-change attempts"
            )
            return False, False, False

        if not is_valid_keypad_pin(new_password):
            raise ValueError(
                f"New password must be {KEYPAD_PIN_MIN_LENGTH}-{KEYPAD_PIN_MAX_LENGTH} "
                "digits — it doubles as the physical keypad's PIN"
            )

        self._failed_attempts = 0
        self._password_hash = await asyncio.to_thread(hash_password, new_password)
        self.keypad_pin = new_password
        self.tokens.revoke_all(except_token=current_token)
        # Durable before the hardware round trip below, on purpose — see the
        # docstring above.
        self._persist()

        from . import esp32_client, secrets_file  # deferred: esp32_client imports `state`

        hardware_ack = await esp32_client.send_keypad_pin(new_password)
        secrets_synced = secrets_file.update_keypad_pin(new_password)

        self._log(
            "Password changed"
            if hardware_ack
            else "Password changed — the door controller could not be reached",
            EventCategory.auth,
        )
        # Persisted again so the log line above (and the hardware/secrets
        # outcome, though those aren't themselves stored fields) survive a
        # crash right after the change — the earlier _persist() only
        # promised the credential itself wouldn't be lost.
        self._persist()
        await self._broadcast()
        return True, hardware_ack, secrets_synced

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

        Home mode bypasses both perimeter sensors, the ultrasonic one and the
        vibration one (people moving around inside shouldn't trip them); the
        door sensor stays live. Away mode bypasses nothing. Factored out of
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
                bypassed=(
                    mode == ArmMode.home
                    and s.type in (SensorType.vibration, SensorType.ultrasonic)
                ),
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

        # Re-check before committing. `arm()` publishes `transitional` and
        # awaits a broadcast *before* it stores our task handle, so during
        # that window `disarm()` and `report_state_from_esp32()` both find
        # `self._arming_task is None` and their `.cancel()` is a no-op — the
        # disarm applies, and three seconds later this task would arm a house
        # whose door controller was just told to disarm. Backend says ARMED,
        # hardware says DISARMED, siren dead, and an opened door reports
        # nothing. Checking the state here closes that regardless of whether
        # the broadcast happened to suspend.
        if self.alarm_state != AlarmState.transitional:
            return

        self.alarm_state = AlarmState.armed
        self._apply_bypass(mode)
        self._log(
            f"System armed ({'Away' if mode == ArmMode.away else 'Home'})",
            EventCategory.armDisarm,
        )
        await self._broadcast()

    def _clear_alarm_timers(self) -> None:
        """Drops everything that only means something while an alarm cycle is
        still running: the entry-delay deadline, the watchdog behind it, and
        the pending "still not cleared" escalation.

        Shared by `disarm()` and `report_state_from_esp32()` because those are
        the same event seen from two sides — someone made the system safe.
        Only `disarm()` used to do any of this, and it is the *rarer* of the
        two paths: the door controller owns the keypad, so a real-world disarm
        arrives here as a controller report. That left a disarmed system still
        holding a live escalation task, which fired the urgent "alarm not
        cleared, call your emergency contact" push a minute after the alarm
        had already been dealt with, and a stale `_entry_delay_ends_at`.
        """
        self._entry_delay_ends_at = None
        if self._entry_delay_task:
            self._entry_delay_task.cancel()
            self._entry_delay_task = None
        # Acknowledging the alarm in time is exactly what should stop the
        # escalation notification from going out.
        if self._escalation_task:
            self._escalation_task.cancel()
            self._escalation_task = None

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
        self._clear_alarm_timers()
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
        # One exception to "the controller is authoritative": it may not
        # arm out of a live alarm. `arm()` already refuses that ("repeated
        # /arm calls can't interrupt an alert") but this path used to accept
        # `triggered -> armed`, and `_apply_bypass` below then rebuilt every
        # SensorReading with ok=True — erasing the ok=False marker on the
        # sensor that caught the intrusion. The red banner vanished from the
        # app with no `disarmed` event to explain it, and the pending
        # `_escalate_after_delay` found a non-triggered state and swallowed
        # the "alarm not cleared" push. Reachable whenever the controller
        # doesn't know we're alarming, which is the default: an unset
        # HOMEGUARD_DOOR_CONTROLLER_URL makes every outbound command a no-op.
        # Only a human disarm — keypad, app, or face — leaves `triggered`.
        if armed and self.alarm_state == AlarmState.triggered:
            self._log(
                "Controller reported armed during an active alarm — ignored",
                EventCategory.alert,
            )
            await self._broadcast()
            return

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

        # Same teardown `disarm()` does, and this is the path that actually
        # runs in the field — see [_clear_alarm_timers].
        self._clear_alarm_timers()
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
        if self._entry_delay_task:
            self._entry_delay_task.cancel()
        self._entry_delay_task = asyncio.create_task(self._entry_delay_watchdog(seconds))
        self._log("Door opened — enter code to disarm", EventCategory.armDisarm)
        await self._broadcast()

    async def _entry_delay_watchdog(self, seconds: int) -> None:
        """Raises the alarm ourselves if the entry delay simply ran out.

        The countdown belongs to the door controller, and the design is that
        it reports the outcome: PIN accepted (`/esp32/state-change`) or time
        up (`/esp32/sensor-event`). Nothing here ever checked that a report
        arrived, so escalation depended entirely on that one board surviving
        the next 30 seconds — and the board is inside the house the intruder
        just walked into. Pull its power, or its WiFi, between opening the
        door and the countdown expiring and the system stayed in `entryDelay`
        for good: no `triggered`, no siren command to any other board, no
        push, and an app showing a countdown that never reaches zero. A silent
        and total bypass of the alarm, from the one state it is most exposed
        in.

        So: wait out the same window plus [ENTRY_DELAY_GRACE], and if the
        system is *still* in `entryDelay`, treat it as nobody having disarmed.
        Cancelled by every legitimate way out of the state (see
        [_clear_alarm_timers]), so it only ever fires when the controller went
        quiet.
        """
        try:
            await asyncio.sleep(seconds + ENTRY_DELAY_GRACE.total_seconds())
        except asyncio.CancelledError:
            return
        if self.alarm_state != AlarmState.entryDelay:
            return
        logger.warning("Entry delay expired with no report from the door controller — alarming.")
        await self.trigger_alert(SensorType.door, "entry delay expired with no disarm")

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
        logins, manual triggers). Called from three places:
        AppState._register_failed_attempt (lockout), /trigger-alert (manual
        trigger, used by simulate.py), and /esp32/sensor-event (real sensor
        reports).
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
        if self.alerts_enabled:
            notifier.notify(
                self.ntfy_server,
                self.ntfy_topic,
                title="HomeGuard alarm",
                message=reason,
                alert_to=self.alert_emails,
                escalation_to=self.escalation_emails,
                photo=self._alert_photo(),
                clip=self._alert_clip(),
            )
        self._start_escalation(reason)

        # Push to the apps *before* touching the hardware. This used to run
        # after, which meant every connected phone waited out the door
        # controller's round trip — and that board answers from inside its
        # own `loop()`, where a blocking HTTPClient call can hold it for
        # seconds, so the wait routinely ran to `esp32_client`'s full 3s
        # timeout. Three seconds of an alarm nobody has been told about is
        # the exact cost `notifier.notify()` is non-blocking to avoid; there
        # is no reason to reintroduce it two lines later.
        await self._broadcast()

        # The door controller's buzzers only react to its own local door
        # sensor + keypad — they don't know a different board just tripped.
        # Skipped when the door itself is the source: its entryDelay->ALARM
        # transition already sounds the siren locally before this runs (see
        # reportDoorAlert() in the sketch), so this would just be a redundant
        # round trip to a board that's already alarming.
        if source != SensorType.door:
            from . import esp32_client  # deferred: esp32_client imports `state`

            await esp32_client.send_trigger_alarm()

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

        message = (
            f"Alarm still active after {self.police_call_delay_seconds}s — {reason}. "
            "Nobody has cleared it."
        )
        if not self.alerts_enabled:
            self._log(
                "Alarm not cleared — escalation NOT sent: alerts are switched off",
                EventCategory.alert,
            )
        elif self.ntfy_topic or self._alert_config().any_channel(escalation=True):
            self._log("Alarm not cleared — escalation notification sent", EventCategory.alert)
        else:
            # With no channel `notifier.notify` is a no-op, so claiming a
            # notification went out would be a lie in the security log.
            self._log(
                "Alarm not cleared — escalation NOT sent: no notification channel is set up",
                EventCategory.alert,
            )
        if self.alerts_enabled:
            notifier.notify(
                self.ntfy_server,
                self.ntfy_topic,
                title="HomeGuard — ALARM NOT CLEARED",
                message=message,
                priority="urgent",
                tags="rotating_light,sos",
                escalation=True,
                alert_to=self.alert_emails,
                escalation_to=self.escalation_emails,
                photo=self._alert_photo(),
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

    async def log_face_recognized(self, name: str, *, live: bool = True) -> bool:
        """Records a face-recognition match and, if face-disarm is enabled,
        disarms the system. Returns whether it disarmed.

        Face-disarm is **off by default**, deliberately. Even accounting for
        `live` below, this stays a convenience feature, not a security one —
        it lowers the bar to disarming from "knows the PIN or the password"
        to "has a photo of someone who lives here, held steady enough to
        fool a motion check". Enabling it is an informed choice the user
        makes in Settings.

        `live` is what the caller (main.py's recognize_frame) determined
        from a short window of facial-landmark movement — see
        `is_live_landmark_sequence` in face_recognition_service.py. SFace
        itself only compares face embeddings and has no notion of liveness:
        a printed photo or a picture on a phone screen matches just as well
        as the real person, unless something else is watching for the
        natural micro-movement a static image can't reproduce. `live`
        defaults to True so every call site that predates this check —
        including the direct state-layer tests of the arm/disarm machinery
        below, which have nothing to do with the camera pipeline — keeps
        disarming exactly as it did before liveness existed.

        Only disarms from `armed` or `triggered` — the states where
        disarming is a meaningful action. Recognizing a face while already
        disarmed is just an audit-log entry — and so, distinctly, is a match
        that would have disarmed but failed the liveness check (see below).

        That audit entry is rate-limited per person (see
        FACE_LOG_COOLDOWN). The ESP32-CAM pushes two frames a second, so
        someone simply standing in front of it used to produce two log lines
        and two full-state broadcasts every second: the 200-entry log filled
        with "Face recognized: X" in under two minutes, burying everything
        that mattered, and every connected phone was sent ~21 KB per frame —
        about 2.6 MB a minute — to be told the same thing over and over.
        Recognition itself is unthrottled; only the *reporting* of it is.
        The same cooldown covers the "liveness check failed" line below, for
        the same reason: a spoofing attempt held in front of the camera
        should not spam the log any more than a genuine sighting would.
        """
        now = datetime.now()
        last_seen = self._face_last_logged.get(name)
        recently_logged = last_seen is not None and now - last_seen < FACE_LOG_COOLDOWN
        self._face_last_logged[name] = now

        # Whether this match would disarm if it were live — kept separate
        # from `should_disarm` so a match defeated *only* by a failed
        # liveness check can be told apart from one that was never going to
        # disarm anything (face-disarm off, or not in a disarmable state).
        disarm_eligible = self.face_disarm_enabled and self.alarm_state in (
            AlarmState.armed,
            AlarmState.triggered,
            AlarmState.entryDelay,
        )
        should_disarm = disarm_eligible and live

        if not should_disarm:
            if disarm_eligible and not live:
                # Would have disarmed; the liveness check said no. That's a
                # security-relevant signal in its own right — it's what a
                # spoofing attempt looks like — so it gets its own line
                # instead of being folded into a plain sighting, but the
                # same per-person cooldown still applies to it.
                if recently_logged:
                    return False
                self._log(
                    f"Face recognized but liveness check failed: {name}",
                    EventCategory.armDisarm,
                )
                await self._broadcast()
                return False
            if recently_logged:
                return False
            self._log(f"Face recognized: {name}", EventCategory.faceId)
            await self._broadcast()
            return False

        # A disarm is always logged, however recently this face was seen —
        # rate-limiting an "the alarm was just turned off" entry would be
        # exactly the wrong thing to economise on.
        self._log(f"Face recognized: {name}", EventCategory.faceId)

        # disarm() broadcasts and resets sensors; log the reason first so the
        # event log reads in cause-then-effect order.
        self._log(f"Disarmed by Face ID: {name}", EventCategory.armDisarm)
        await self.disarm()

        # Same local-state-then-hardware-command pattern as the /disarm
        # route (main.py) — without this, a face disarm only ever flipped
        # the software state: the app showed disarmed over the WebSocket
        # broadcast above, but the door controller was never told, so its
        # buzzer/lock kept running. Deferred import: esp32_client imports
        # `state` at module level, so importing it up top would be circular.
        from . import esp32_client

        await esp32_client.send_disarm()
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

    async def set_alerts_enabled(self, enabled: bool) -> None:
        """Switches messages that leave the house on or off (email, WhatsApp,
        ntfy). The alarm itself — siren, app state, event log — is unaffected;
        only the outgoing messages stop, so a bench test does not flood an inbox.
        Logged, because "why did no email arrive" must be answerable later."""
        self.alerts_enabled = enabled
        self._log(
            "Alerts switched on" if enabled else "Alerts switched OFF — no messages will be sent",
            EventCategory.info,
        )
        self._persist()
        await self._broadcast()

    async def set_alert_clip(self, enabled: bool, seconds: int) -> None:
        """Whether the breach email carries a video of the last few seconds, and
        how many (5-60, clamped). The camera frames are kept either way."""
        self.clip_enabled = enabled
        self.clip_seconds = _clamp_clip_seconds(seconds)
        self._log(
            f"Alert video on, last {self.clip_seconds} s"
            if enabled
            else "Alert video switched off",
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

    async def send_test_notification(self) -> dict[str, str]:
        """Sends a harmless test over every configured channel, so the user can
        confirm alerts really arrive *before* relying on them for a break-in.

        Unlike a real alert this waits for the outcome (it is the user asking
        "does it work?"). One entry per channel, "" meaning delivered; an
        empty dict means no channel is set up at all.
        """
        return await notifier.send_test(
            self.ntfy_server,
            self.ntfy_topic,
            self.alert_emails,
            self.escalation_emails,
            self._alert_photo(),
            self._alert_clip(),
        )

    def record_clip_frame(self, jpeg: bytes, *, stream: bool = False) -> None:
        """Remembers one camera frame for the alarm clip, dropping anything
        older than a minute. `stream=True` is a frame of the live stream (about
        20 a second); otherwise it is a still pushed for face recognition (1-2 a
        second), which is only kept while the stream is not delivering."""
        now = time.time()
        if stream:
            self._stream_frame_at = now
        elif now - self._stream_frame_at < 3:
            return
        self.clip_frames.append((now, jpeg))
        while self.clip_frames and now - self.clip_frames[0][0] > clip.CLIP_SECONDS:
            self.clip_frames.popleft()

    def _alert_clip(self) -> tuple[tuple[float, bytes], ...] | None:
        """The frames of the last minute (fewer if that is all there is), or
        None. A snapshot, taken the moment the alarm fires; the video is made
        later, off the event loop, by `alerts`."""
        if not self.clip_enabled:
            return None
        cutoff = time.time() - self.clip_seconds
        frames = tuple(f for f in self.clip_frames if f[0] >= cutoff)
        return frames if len(frames) >= 2 else None

    def _alert_photo(self) -> bytes | None:
        """The latest camera frame, for the alarm email, or None.

        Only while the camera is really delivering (a frame in the last
        `BOARD_TIMEOUT`): the cache keeps the last frame forever, and a picture
        from hours ago next to "someone is in the house" would mislead."""
        if self.camera_frame_source is None or not self.camera_online:
            return None
        return self.camera_frame_source()

    def _alert_config(self) -> alerts.AlertsConfig:
        """The local alerts file with the recipients set in the app applied."""
        return alerts.load_config().with_recipients(self.alert_emails, self.escalation_emails)

    async def set_alert_emails(self, alert_to: list[str], escalation_to: list[str]) -> None:
        """Replaces who is emailed. The caller has already validated the
        addresses. Logged as counts only: the event log is shown in the app and
        sits in `state.json`, and addresses are nobody else's business."""
        self.alert_emails = list(alert_to)
        self.escalation_emails = list(escalation_to)
        self._log(
            f"Alert recipients updated ({len(alert_to)} alarm, {len(escalation_to)} emergency)",
            EventCategory.info,
        )
        self._persist()
        await self._broadcast()

    async def set_police_call_delay(self, seconds: int) -> None:
        """Stores how long to wait after a triggered alarm before escalating
        (see `_start_escalation` for what "escalating" means here, since
        nothing dials a phone).

        Clamped, unlike before — its sibling `set_night_mode_hours` always
        was. A negative value is the one that matters: `asyncio.sleep()`
        returns immediately for one, so `_escalate_after_delay` would send the
        urgent "alarm still active, nobody has dealt with it" push in the same
        instant as the alarm it is supposed to be following up on, leaving no
        window at all to disarm first.
        """
        self.police_call_delay_seconds = _clamp_police_call_delay(seconds)
        self._persist()
        await self._broadcast()

    # ---- Camera connectivity ---------------------------------------------------

    async def note_camera_frame(self) -> None:
        """Records that a camera frame just arrived, from /camera/recognize.

        Broadcasts only on the offline->online transition, not on every
        frame — the ESP32-CAM sends two a second, and a full-state push each
        time would be 2.6 MB a minute to every connected phone to say
        nothing new. The reverse transition needs no broadcast from here at
        all: it is time-derived, so `_monitor_boards` picks it up on its next
        tick, the same way a board going quiet does.
        """
        was_online = self.camera_online
        self._camera_last_frame_at = datetime.now()
        if not was_online:
            await self._broadcast()


state = AppState()
