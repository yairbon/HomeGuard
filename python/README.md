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

The interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are **off by
default**: they have no login, so they would show the whole API to anyone on
the network. Start with `HOMEGUARD_DOCS=1` to turn them on for development.

`..\scripts\start-backend.ps1` does the same with the board addresses and
`HOMEGUARD_BOARDS` preset (`-DoorIp` / `-CameraIp` override them), and
`..\scripts\start-backend.cmd` opens it in its own visible window (double-click).
The window **is** the backend: closing it stops it. It prints what happens as it
happens (`[armDisarm] System armed (Away)`, `[alert] ALERT ...`, the email sent),
and leaves out the boards' heartbeats and the camera's frame pushes so those lines
stay readable; `-LogAllRequests` (or `HOMEGUARD_LOG_ALL_REQUESTS=1`) puts every
request back, for diagnosing. A session token in a log line (the WebSocket takes it
in the URL) is always replaced by `token=***`.

### The web app, from the same port

If `..\app\build\web` exists, the backend also serves the Flutter web app
from `/`, so a phone or browser on the same network just opens
`http://<this machine>:8000/` and logs in. Page and API share one origin, so
there is no CORS list to maintain, and the web build defaults its backend
address to the address it was loaded from. Build it with
`..\scripts\build-web.ps1` (re-run after any app change; restart the backend
only if the folder did not exist when it started). `HOMEGUARD_WEB_DIR` points
at a build somewhere else. The files are the public compiled app, so they are
served without a credential; every API route is still protected. Unknown paths
are a plain 404.

On the very first run the backend generates a **device key** and logs it at
startup. Copy that value into each board's `secrets.h` (see
`../esp32/secrets.h.example`). It's saved to `state.json`, so it stays the
same across restarts. To set it explicitly instead, export
`HOMEGUARD_DEVICE_KEY` before starting.

On a first run the login password is the door keypad's own PIN, read from
`KEYPAD_PIN` in the door controller's `secrets.h` when `HOMEGUARD_SECRETS_H`
names that file (`scripts\start-backend.ps1` sets it), so the app password and
the keypad PIN are one secret from the very start. Without that file it falls
back to the factory default `homeguard`. Change it from the app's Settings
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

`/health` and `/login` are the unauthenticated surface (plus FastAPI's docs
routes, only when `HOMEGUARD_DOCS=1`). Everything else requires a credential.
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

### A board reporting offline may just be a stale IP

A board that never got past its first HTTP request looks identical, from the
app's side, to one that's simply offline — but the most common real cause on
this project has been the dev machine's IP changing (WiFi DHCP reassigns it,
including across sessions) while a board is still flashed with the old one in
its `secrets.h`. `ip_check.py` checks every board's `secrets.h` against this
machine's current LAN IP once, at backend startup (see `lifespan` in
`main.py`), and logs a loud warning naming exactly which board(s) are stale
and what to fix. It never blocks startup and stays silent for a board whose
`secrets.h` doesn't exist yet (a fresh checkout, or CI).

## Face recognition

OpenCV's own model zoo: **YuNet** for detection, **SFace** for recognition,
both ONNX files in `models/`, both loaded by `opencv-contrib-python` — which
this project already depends on. No new dependency, and critically no native
build step.

That last point is why the obvious alternatives were rejected:
`face_recognition` wraps dlib and `insightface` ships a Cython extension;
both need a working MSVC/CMake toolchain on Windows, which is exactly the
class of problem this project has already lost days to.

This replaced a Haar cascade + LBPH pipeline. Haar misses turned, occluded
and small faces; LBPH compares local texture histograms and is very sensitive
to lighting and alignment — the exact things that differ between an
enrollment photo and a doorway camera at night. SFace embeds a
landmark-aligned crop, so a head tilt no longer changes the descriptor much.

The old path is kept as an automatic fallback: if the ONNX files are missing,
the service logs a warning and uses Haar+LBPH rather than refusing to start.
`FaceRecognitionService.backend_name` reports which is live.

## Alerts: WhatsApp and email

The app's own notifications only fire while it is open, and the iPhone web
app cannot receive push at all, so an alarm is also sent as a message. Two
channels, both configured in **one local file that is never committed**:
`alerts.local.json` next to this README (copy `alerts.example.json`; a blank
`alerts.local.json` already exists). The credentials never go into the app,
`state.json` or a chat, and the file is read on every send, so editing it needs
no restart. Either channel, or both:

- **WhatsApp via CallMeBot** (free): add the bot's number to your contacts and
  send it the activation message; it replies with a key. Both the number and the
  exact message are on https://www.callmebot.com/blog/free-api-whatsapp-messages/
  — copy them from there, because the bot's number has changed over time. Put
  your own number (with country code) and the key in the `whatsapp` list. The
  page says the free API is only for personal use and that sending to other
  people or groups, or from your own WhatsApp number, needs a different
  service; it is run by one person and promises no uptime. The alarm text
  passes through their server. It is not a way to reach the emergency contact.
- **Email** (free): a Gmail account that sends, with a Google *app password*
  (Google account → Security → 2-Step Verification → App passwords); that
  account and password are the only email settings in the file. **Who receives
  the emails is edited in the app** (Settings → Alerts): one list gets every
  alarm, and the emergency contacts are added when the alarm has not been
  cleared after the delay set there. The `alert_to` / `escalation_to` lists in
  the file are only the starting values, copied into `state.json` once on the
  first start; after that the app owns them. Settings also has a master switch,
  "Send alerts by email", that stops all outgoing messages (the siren and the
  app are unaffected) so a bench test does not flood an inbox. Nothing dials a
  phone. On an iPhone use the Gmail app for instant notifications; Apple's Mail
  app only checks Gmail from time to time.

  **Video in the alarm email.** The backend watches the ESP32-CAM's stream and keeps
  the last minute of frames in memory (`clip.py`). When an alarm fires it encodes
  the last 5-60 seconds into an MP4 (H.264, under 10 MB) in a worker thread and
  attaches it next to the picture. Settings has a switch and a length slider
  (`POST /settings/alert-clip`, saved in `state.json`; on and 60 s by default).
  Without the stream the camera's stills for face recognition are used instead; if
  no video can be made the email goes out with the picture only.

Both channels need internet access from whatever network the laptop is on.
Settings has a **SEND TEST ALERT** button (`POST /settings/alerts/test`) that
sends a real message on every configured channel and shows which one worked,
without ever echoing a password, key or number. Failures are logged without
secrets and never delay or break the alarm itself; the "escalation sent" line in
the event log is only written when a channel actually exists. The throwaway
`simulate.py` server must point `HOMEGUARD_ALERTS_FILE` at a file that does not
exist, or its test alarms would message you for real.

## Push notifications (ntfy) — optional, API only

ntfy still works if a topic is set through `POST /settings/ntfy`, but the app no
longer shows it: it needs its own phone app, which is what the channels above
avoid.

The app's own notifications only fire while its process is alive — swipe it
away and a break-in produces nothing. `backend/notifier.py` closes that gap by
POSTing to an [ntfy](https://ntfy.sh) topic, which the ntfy Android app
receives independently of HomeGuard.

Chosen over Firebase Cloud Messaging because FCM needs a Firebase project and
a `google-services.json` only the project owner can create, and adding the
Gradle plugin without that file breaks the Android build outright. ntfy needs no account, no new Flutter dependency, and no
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

**This is convenience, not security.** SFace itself compares face embeddings
only and has no notion of liveness. Two independent checks sit in front of
it — facial-landmark motion over a short window, and a MiniFASNetV2
anti-spoofing model — closing both the static-photo case and the
jiggled-photo/screen-replay case either check misses alone, but neither is a
hardened defense and the model's threshold is still best-effort. Enabling
face-disarm lowers disarming from "knows the PIN or password" to "presented
a face convincing enough to pass both checks". The toggle says so, and every
face-disarm is attributed in the event log.

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

.\venv\Scripts\python.exe -m pytest        # 285 tests, ~75s
.\venv\Scripts\python.exe -m ruff check .
```

`pytest` runs in-process against the ASGI app — no server, no ports, and no
real waiting (lockout expiry and heartbeat staleness are tested by shifting
timestamps, not by sleeping). Each test gets a fresh install via a reloaded
state module pointed at a temp directory.

```powershell
# In one terminal — a THROWAWAY server, never your real one: the script
# changes the password and arms and disarms. Its own port and state file, and
# no HOMEGUARD_DOOR_CONTROLLER_URL / HOMEGUARD_SECRETS_H, so no real board is
# commanded and no secrets.h is rewritten.
$env:HOMEGUARD_STATE_FILE = "$env:TEMP\sim-state.json"
$env:HOMEGUARD_ALERTS_FILE = "$env:TEMP\no-alerts.json"   # must not exist: no real messages
$env:HOMEGUARD_DEVICE_KEY = "simulation-device-key"
.\venv\Scripts\python.exe -m uvicorn backend.main:app --port 8765
# In another (it assumes the factory-default password, so start with no state file):
$env:HOMEGUARD_BASE_URL = "http://localhost:8765"
.\venv\Scripts\python.exe simulate.py
```

`simulate.py` is the live counterpart: 95 checks against a real uvicorn
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

## Optional environment variables

| Variable | Default | What it does |
|---|---|---|
| `HOMEGUARD_ALERTS_FILE` | `alerts.local.json` | Where the WhatsApp/email alert settings are read from (see "Alerts"). Point it at a missing file for any server that must not message anyone. |
| `HOMEGUARD_FACES_DIR` | next to the state file | Where the enrolled faces' photos live (`python/faces` for a normal install). It follows `HOMEGUARD_STATE_FILE`, so a test or simulation server with its own state file gets its own photo folder and can never delete a real enrolment (that happened once: `simulate.py` deleted the real face with id 4). |
| `HOMEGUARD_BOARDS` | every board | Comma-separated boards this install expects heartbeats from: any of `ultrasonic`, `vibration`, `door_controller`. Set `ultrasonic,door_controller` when both sensors are wired to the one ultrasonic board (its sketch also carries the vibration sensor) — otherwise `vibration` shows as permanently offline and the app raises a SENSOR OFFLINE banner for it. Unknown names are ignored with a warning. |
| `HOMEGUARD_RECORD_CAMERA` | `1` | Set to `0` to stop the always-on recording of the camera stream; the alarm video is then made from the stills the camera pushes for face recognition. |
| `HOMEGUARD_CORS_ORIGINS` | none | Comma-separated extra browser origins allowed to call the API, e.g. `http://localhost:5000,http://192.168.1.20:5000`. Only needed when the Flutter **web** build is served from its own port (`flutter run -d web-server`); the Android app isn't a browser and ignores CORS. Exact origins only — `*` is refused. |

Both are read once at startup, so changing one needs a backend restart.

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
