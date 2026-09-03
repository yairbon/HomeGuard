# HomeGuard Backend

FastAPI backend that unifies the four ESP32 boards' communication and face
recognition, and serves the Flutter app.

## Setup

```powershell
cd python
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
```

Dependencies are pinned (`~=`) to the versions this was verified against —
see the comment at the top of `requirements.txt` for why that matters here
specifically.

## Run

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Docs at `http://localhost:8000/docs` (FastAPI auto-generates this).

On the very first run the backend generates a **device key** and logs it at
startup. Copy that value into each board's `secrets.h` (see
`../esp32/secrets.h.example`). It's saved to `state.json`, so it stays the
same across restarts. To set it explicitly instead, export
`HOMEGUARD_DEVICE_KEY` before starting.

Default login password is `homeguard`; change it from the app's Settings
screen. It's stored as a scrypt hash in `state.json`, never in plaintext.

## Login password / keypad PIN: one shared secret

`POST /change-password` changes a single value that is **both** this app's
login password **and** the physical keypad's disarm PIN. These used to be
two independent credentials behind two endpoints (this one, plus a
since-removed `/settings/keypad-pin`) — unified into one at the project
owner's explicit request. A new value must be 4-8 digits: the one shape a
4x4 physical keypad can type, which used to constrain only the keypad PIN
and now constrains the app password too.

**This is a real security trade-off, stated plainly rather than left
implicit in the diff.** The app password used to be free-form and
scrypt-hashed — any length, any character. The single value that replaces
it — which unlocks not just the front door but remote arm/disarm, the live
camera feed, and every setting — now lives in a space of at most 10⁸ digit
combinations. That was an informed choice for this household's own system;
it is worth re-weighing before relying on it somewhere less trusted than a
private Tailnet. See `AppState.change_password` in `backend/state.py` for
the full account, including why the *old* value has no such constraint (so
the factory-default `homeguard` still works as `old_password` once) and why
it can never be set again as a `new_password` once changed away from.

The response carries `hardware` (did the door controller acknowledge the
new PIN over the network) and `secrets_updated` (was
`esp32/.../secrets.h` rewritten to match, gated behind `HOMEGUARD_SECRETS_H`)
alongside `ok`. Both are the same two-outcome honesty `/settings/keypad-pin`
used to report on its own: "saved here, but the board didn't answer" is a
real, non-fatal outcome, distinct from both success and failure, and it stays
that way now that one call does both jobs.

## Authentication

Nothing here is open. Two credentials, for two kinds of caller:

| Caller | Credential | How it's sent |
|---|---|---|
| Flutter app | Session token from `/login` (12h) | `Authorization: Bearer <token>` |
| ESP32 boards | Shared device key | `X-Device-Key` header, or `?key=` |

`/health`, `/login` and FastAPI's own docs routes (`/docs`, `/redoc`,
`/openapi.json`) are the unauthenticated surface — note that means the full
API schema is public. Everything else requires a credential.
`tests/test_auth.py` enumerates `app.routes` and fails if a new route is
added without being covered, so this list cannot drift again.

The WebSocket takes its token as
`?token=` (the browser WebSocket API can't set headers) and is refused
*before* `accept()`, so an unauthenticated client never receives a state
frame.

## Persistence

`state.json` (next to this README; gitignored) holds the password hash,
device key, approved faces, settings and the event log, written atomically on
every change.

`alarm_state` is deliberately **not** persisted. A restarted backend genuinely
doesn't know whether the house is armed — coming up `disarmed` and letting the
door controller's next `/esp32/state-change` report establish the truth is
self-correcting, whereas writing a stale `armed` back would make the app
display a state nothing has verified.

Override the location with `HOMEGUARD_STATE_FILE` (the test suite does this to
keep each test on a fresh install).

## Board liveness

Each *sensor* board POSTs `/esp32/heartbeat?board=<name>` every 10 seconds.
A board is reported offline after 30 seconds of silence, and `/state` carries
a `boards` array with per-board status — the dashboard names the specific
board that went quiet.

The ESP32-CAM deliberately sends no heartbeat and has no entry in
`BOARD_NAMES`; its liveness is signalled by frames arriving at
`/camera/recognize`.

This replaced a single `esp32_online` boolean that latched: once any board had
ever checked in, nothing ever set it back, so a board could lose power and the
app would keep showing it online indefinitely.

## Face recognition

OpenCV's own model zoo: **YuNet** for detection, **SFace** for recognition,
both ONNX files in `models/`, both loaded by `opencv-contrib-python` — which
this project already depends on. No new dependency, and critically no native
build step.

That last point is why the obvious alternatives were rejected:
`face_recognition` wraps dlib and `insightface` ships a Cython extension;
both need a working MSVC/CMake toolchain on Windows, which is exactly the
class of problem this project has already lost days to (see `../CLAUDE.md`).

This replaced a Haar cascade + LBPH pipeline. Haar misses turned, occluded
and small faces; LBPH compares local texture histograms and is very sensitive
to lighting and alignment — the exact things that differ between an
enrollment photo and a doorway camera at night. SFace embeds a
landmark-aligned crop, so a head tilt no longer changes the descriptor much.

The old path is kept as an automatic fallback: if the ONNX files are missing,
the service logs a warning and uses Haar+LBPH rather than refusing to start.
`FaceRecognitionService.backend_name` reports which is live.

## Push notifications (ntfy)

The app's own notifications only fire while its process is alive — swipe it
away and a break-in produces nothing. `backend/notifier.py` closes that gap by
POSTing to an [ntfy](https://ntfy.sh) topic, which the ntfy Android app
receives independently of HomeGuard.

Chosen over Firebase Cloud Messaging because FCM needs a Firebase project and
a `google-services.json` only the project owner can create, and adding the
Gradle plugin without that file breaks the Android build outright (see
`../CLAUDE.md`). ntfy needs no account, no new Flutter dependency, and no
change to the app's build.

Set the server and topic in the app's Settings screen, then install the ntfy
app and subscribe to the same topic. There's a **Send test** button — use it
before relying on this.

**The topic name is a secret.** On the public ntfy.sh anyone who knows it can
read your alarm notifications, so use a long random name or self-host. The
topic is deliberately never written to the event log.

Two kinds of push are sent: one the moment the alarm triggers, and — if the
alarm is *still* uncleared after `police_call_delay_seconds` — a second,
urgent one naming the emergency contact. That escalation is what
`emergency_number` now actually drives; it previously drove nothing while the
UI claimed the number would be "auto-called". Placing a real call needs a
telephony provider, which is out of scope, so the wording was corrected to
match what happens.

## Camera worker

`camera_worker.py` is what actually feeds face recognition. Without it the
recognizer worked but nothing ever called `/camera/recognize`, so no face was
ever recognized in normal operation.

```powershell
$env:HOMEGUARD_DEVICE_KEY = "<the backend's device key>"
.\venv\Scripts\python.exe camera_worker.py --url http://localhost:8000 --camera 0
```

A separate process rather than a thread inside the backend: grabbing frames is
a blocking OpenCV call and the backend is a single event loop serving every
WebSocket, so a blocking read there would stall the whole alarm UI. It also
lets the camera live on a different machine.

### Face-ID disarm

Off by default. When enabled (Camera tab), a recognized approved face disarms
the system — including from a sounding alarm or mid entry-delay.

**This is convenience, not security.** SFace compares face embeddings and has
no liveness detection, so a printed photo or a picture on a phone screen may
pass. Enabling it lowers disarming from "knows the PIN or password" to "has a
photo of a resident". The toggle says so, and every face-disarm is attributed
in the event log.

## Night mode and entry delay

`night_mode` now runs a real scheduler (`AppState._night_mode_tick`): it arms
at `night_mode_arm_hour` and disarms at `night_mode_disarm_hour`, whole hours,
checked once a minute. It was previously a stored flag with no code behind it
while the UI said it auto-armed.

It never silences a live alarm — it arms only from `disarmed` and disarms only
from `armed`, never from `triggered` or `entryDelay`. A human decides when an
alert is over.

`entryDelay` is a new alarm state: the door controller now reports
(`/esp32/entry-delay`) the moment it starts its disarm countdown, so the app
shows the countdown the person walking in is racing. Previously the board ran
that window silently and reported only the outcome, so the app jumped straight
from ARMED to TRIGGERED.

## Testing

Two suites, deliberately:

```powershell
.\venv\Scripts\pip.exe install -r requirements-dev.txt

.\venv\Scripts\python.exe -m pytest        # 231 tests, ~105s
.\venv\Scripts\python.exe -m ruff check .
```

`pytest` runs in-process against the ASGI app — no server, no ports, and no
real waiting (lockout expiry and heartbeat staleness are tested by shifting
timestamps, not by sleeping). Each test gets a fresh install via a reloaded
state module pointed at a temp directory.

```powershell
# In one terminal:
$env:HOMEGUARD_DEVICE_KEY = "simulation-device-key"
.\venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
# In another (delete state.json first — it assumes the default password):
.\venv\Scripts\python.exe simulate.py
```

`simulate.py` is the live counterpart: 96 checks against a real uvicorn
process over a real socket, so it also covers the ASGI server, the WebSocket
transport and genuine wall-clock timing (it really does wait out the
30-second lockout and the 30-second board timeout — takes a couple of
minutes). Run pytest while editing; run this before believing the system
works.

Both run in CI on every push (`.github/workflows/ci.yml`).

## Talking to the hardware

`backend/esp32_client.py`'s `send_arm`/`send_disarm` target the door
controller specifically — the only board that receives commands. Set
`HOMEGUARD_DOOR_CONTROLLER_URL` (e.g. `http://192.168.1.50`) once that board
is flashed and on the network. Without it, `/arm` and `/disarm` still work for
the app, they just don't reach real hardware — and say so, via the `hardware`
field in their response, which the app surfaces rather than silently assuming
success.

## Known gaps, deliberately not implemented

- `/arm` and `/disarm` update local state and command the hardware
  non-atomically. If the board call fails, local state still runs its own
  exit-delay timer to `armed`. The controller's `/esp32/state-change` reports
  reconcile this, and the `hardware` flag makes the discrepancy visible.
- Arming from the *physical keypad* carries no arm mode, so the backend falls
  back to the last mode requested through the app, defaulting to Away — the
  stricter of the two, so an unknown mode never silently disables a sensor.
- Nothing places an actual phone call. `emergency_number` drives the urgent
  escalation notification instead — see the ntfy section above.

## Matching the Flutter side

`backend/models.py` must stay in step with
**`app/lib/services/http_backend_service.dart`** — that is where the JSON
keys are actually decoded. The abstract `app/lib/services/backend_service.dart`
holds the Dart-side *types* and contains no wire keys at all, so mirroring a
field there and stopping is the mistake to avoid: it compiles, and then the
app throws on the first state frame.

If you rename or add something on one side, mirror it on the other — a
mismatch is a runtime crash in the app's JSON decoding, not a compile error.
`test_state_shape_matches_the_flutter_client` pins the top-level shape so the
two can't drift silently.
