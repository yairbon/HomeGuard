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
settings, all three ESP32 endpoints, and the `/ws` WebSocket (connect
snapshot plus live pushes for settings changes, arming, alerts and
disarm). Run it against a live server:

```powershell
.\venv\Scripts\python.exe simulate.py
```

Takes about a minute (it genuinely waits out the 30-second lockout rather
than faking the clock). Last run: 65/65 passed.

Two of those sections are regression tests for bugs that were live in this
backend, and both fail against the code as it was before:

- **Lockout budget** — the failed-attempt counter stayed pinned at 3 once a
  lockout was set, so the first wrong password *after* the cooldown
  re-locked instantly and `attempts_remaining` reported 0 while the user
  wasn't locked out at all. The check deliberately uses a wrong password: a
  correct one resets the counter either way and hides it.
- **Home bypass on the controller-reported path** — the door controller
  runs its own exit-delay countdown and can report `armed` before the
  backend's timer gets there, and that report carries no arm mode. Home
  mode therefore armed with the vibration sensor still live. `AppState` now
  remembers the mode the last `/arm` requested and recomputes the bypass
  from it, resetting to `away` on disarm so an arm straight from the
  physical keypad (whose mode we never learn) bypasses nothing.

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

**Known remaining gap:** `/arm` and `/disarm` update local state and
command hardware non-atomically — if the door-controller HTTP call fails,
local state still proceeds through its own exit-delay timer regardless.
The controller's own `/esp32/state-change` reports are what reconcile
this. Closing it properly means a two-phase handshake with hardware that
isn't wired up yet, so it stays documented rather than half-built.

`night_mode` is still only a stored flag: nothing auto-arms on a schedule.
Its log line now says just "Night mode enabled" — it used to claim
"auto-arms overnight", which asserted a protection the system doesn't
provide, in the one place a user goes to check what the system did.

## Matching the Flutter side

`backend/models.py` mirrors `app/lib/services/backend_service.dart`
field-for-field. If you rename or add something on one side, mirror it on
the other.
