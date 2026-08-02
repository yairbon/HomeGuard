# HomeGuard Backend

FastAPI backend that unifies the three ESP32 boards' communication and face
recognition, and serves the Flutter app. In-memory state — fine for a
course project, doesn't survive restarts.

## Setup

```powershell
cd python
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
```

## Run

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Docs at `http://localhost:8000/docs` (FastAPI auto-generates this).

## Verifying it works

`simulate.py` is a live simulation script (not a mocked unit-test suite —
it hits a real running server) covering every endpoint: auth + the full
3-attempt lockout cycle, arm/disarm timing, the Home-mode sensor-bypass
logic, all three sensor types' alerts, faces CRUD, face-ID/camera, all
settings, and all three ESP32 endpoints. Run it against a live server:

```powershell
.\venv\Scripts\python.exe simulate.py
```

Takes about a minute (it genuinely waits out the 30-second lockout rather
than faking the clock). Last run: 51/51 passed.

## Status

**The Flutter app talks to this backend for real** — `HttpBackendService`
(`app/lib/services/http_backend_service.dart`) implements the same
`BackendService` interface `MockBackendService` does, over REST + the `/ws`
WebSocket, and `main.dart` uses it by default. `MockBackendService` still
exists for widget tests (no live server in a test run) and offline UI work.

**Real and tested end-to-end** — REST API (see `backend/main.py`), the
`/ws` WebSocket (pushes full state on connect and after every change),
and face recognition (`backend/face_recognition_service.py` — OpenCV Haar
cascade + LBPH, deliberately not `dlib`/`face_recognition`, to avoid a
native CMake/Visual-Studio build on a machine with a documented history of
toolchain pain — see `../CLAUDE.md`). `opencv-contrib-python` doesn't
bundle cascade XML files in this version, so
`models/haarcascade_frontalface_default.xml` is fetched from OpenCV's
official repo and checked into this project as an asset.

**ESP32 communication is real too** — all three board sketches exist
(`../esp32/`), compile cleanly, and this backend's endpoints match what
they actually call. `backend/esp32_client.py`'s `send_arm`/`send_disarm`
target the door controller specifically (the only board that receives
commands — see its own WebServer routes); set
`HOMEGUARD_DOOR_CONTROLLER_URL` once that board is flashed and on the
network. Without it, `/arm`/`/disarm` still work for the app (state and
timing are simulated here), they just don't reach real hardware.

**Known, deliberately-not-fixed gaps** (found while documenting, left as
comments in the code rather than silently patched):
- `/arm` and `/disarm` update local state and command hardware
  non-atomically — if the door-controller HTTP call fails, local state
  still proceeds through its own exit-delay timer regardless. The
  controller's own `/esp32/state-change` reports are what reconcile this.
- `report_state_from_esp32` (used when the door controller reports its
  state) can't reapply Home-mode's vibration-sensor bypass, since that
  path doesn't carry the arm mode.
- `set_night_mode`'s log message describes auto-arm-overnight behavior
  that isn't actually implemented anywhere yet.

## Matching the Flutter side

`backend/models.py` mirrors `app/lib/services/backend_service.dart`
field-for-field. If you rename or add something on one side, mirror it on
the other.
