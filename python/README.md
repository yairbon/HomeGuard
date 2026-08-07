# HomeGuard Backend

FastAPI backend that unifies the three ESP32 boards' communication and face
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

## Authentication

Nothing here is open. Two credentials, for two kinds of caller:

| Caller | Credential | How it's sent |
|---|---|---|
| Flutter app | Session token from `/login` (12h) | `Authorization: Bearer <token>` |
| ESP32 boards | Shared device key | `X-Device-Key` header, or `?key=` |

`/health` is the only unauthenticated route. The WebSocket takes its token as
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

Each board POSTs `/esp32/heartbeat?board=<name>` every 10 seconds. A board is
reported offline after 30 seconds of silence, and `/state` carries a `boards`
array with per-board status — the dashboard names the specific board that
went quiet.

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

## Testing

Two suites, deliberately:

```powershell
.\venv\Scripts\pip.exe install -r requirements-dev.txt

.\venv\Scripts\python.exe -m pytest        # 97 tests, ~30s
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

`simulate.py` is the live counterpart: 78 checks against a real uvicorn
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
- `emergency_number` / `police_call_delay_seconds` are stored but nothing
  dials anything.
- `night_mode` is stored but there's no scheduler; it does not auto-arm.

## Matching the Flutter side

`backend/models.py` mirrors `app/lib/services/backend_service.dart`
field-for-field. If you rename or add something on one side, mirror it on the
other — a mismatch is a runtime crash in the app's JSON decoding, not a
compile error. `test_state_shape_matches_the_flutter_client` pins the wire
shape so the two can't drift silently.
