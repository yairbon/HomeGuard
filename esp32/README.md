# HomeGuard ESP32 Firmware — 4 Boards

Four separate ESP32 boards, each its own sketch (Arduino IDE requires the
folder name to match the `.ino` filename — already set up that way).

| Board | Sketch | Role |
|---|---|---|
| 1 | `ultrasonic_sensor/` | HC-SR04 distance sensor — perimeter breach detection |
| 2 | `vibration_sensor/` | Digital vibration sensor (SW420-style) — forced-entry detection |
| 3 | `door_keypad_controller/` | Magnetic door sensor + keypad + buzzers — **the arm/disarm authority** |
| 4 | `esp32_cam/` | ESP32-CAM (AI-Thinker) — pushes frames for face recognition, serves the live video stream |

Boards 1 and 2 are simple: they detect a breach and POST it to the
backend. They don't know or care whether the system is armed — the
backend decides that (see `python/backend/state.py`, `trigger_alert`).

Board 3 is different: it owns arm/disarm (it has the keypad), so it both
sends reports to the backend *and* receives commands from it (the app's
arm/disarm buttons reach this board through the backend).

Board 4 does both directions too, but for a different reason. It *pushes*
a frame every `CAPTURE_INTERVAL_MS` to `/camera/recognize` (this is what
feeds face recognition — same endpoint `python/camera_worker.py` posts to
from a PC webcam, so the backend can't tell the two apart), and it also
*serves* `/stream`, a continuous MJPEG feed the backend pulls and relays
on to the app (`/camera/stream`). Polling single JPEGs was tried first and
was far too slow to look live; MJPEG is what actually reaches a usable
frame rate. See "Board 4 — the camera" below.

## Before flashing — `secrets.h`

Copy `secrets.h.example` into **each of the four sketch folders** as
`secrets.h` and fill in the real values:

```
esp32/ultrasonic_sensor/secrets.h
esp32/vibration_sensor/secrets.h
esp32/door_keypad_controller/secrets.h
esp32/esp32_cam/secrets.h
```

Four copies rather than one shared file because the Arduino IDE compiles one
sketch folder at a time and only looks for headers inside that folder.

`secrets.h` is gitignored; only the `.example` is committed. These values —
the WiFi password, the device key, and the keypad PIN — used to be literals
in the `.ino` files, which meant they were committed to this repository in
plain text.

What goes in it:

| Setting | Where to get it |
|---|---|
| `WIFI_SSID` / `WIFI_PASSWORD` | your network |
| `BACKEND_HOST` | `ipconfig` on the machine running the backend, same WiFi as the boards |
| `DEVICE_KEY` | printed by the backend at startup, or set via `HOMEGUARD_DEVICE_KEY` |
| `KEYPAD_PIN` | your choice; only the door controller uses it |

### The device key

Every `/esp32/*` call now sends `X-Device-Key`, and the backend returns 401
without it. This is what stops anything else on the network from forging a
sensor breach — or forging a "system disarmed" report, which would make the
app show a false all-clear.

It works in the other direction too: the backend sends the same key to the
door controller's `/arm` and `/disarm`, and the sketch rejects commands
without it. That board listens on port 80 and `/disarm` silences a sounding
alarm, so an unauthenticated POST from anyone who found its IP would
otherwise be enough to defeat the system.

If a board logs `REJECTED (401)`, its `DEVICE_KEY` doesn't match the
backend's.

## Wiring up two-way communication with the door controller

After flashing `door_keypad_controller`, open its Serial Monitor (9600
baud) — it prints its own IP once WiFi connects. Set that as an
environment variable before starting the backend:

```powershell
$env:HOMEGUARD_DOOR_CONTROLLER_URL = "http://<that IP>"
.\venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Without this, the app's arm/disarm buttons still work (the backend
simulates the timing itself), they just won't reach real hardware.

## Testing the keypad without physical wiring

The keypad is currently simulated through the Serial Monitor —
intentional, so the state machine could be tested before the real 4x4
keypad is wired. Type digits + `*` to arm, digits + `#` to submit a PIN
(the PIN is whatever you set as `KEYPAD_PIN` in `secrets.h`). Swap in the
`Keypad` library once the physical keypad's pin mapping is known.

## Heartbeats

Each *sensor* board pings `/esp32/heartbeat?board=<name>` every 10 seconds,
using the name the backend knows it by (`ultrasonic`, `vibration`,
`door_controller` — see `BOARD_NAMES` in `python/backend/state.py`). The
backend marks a board offline after 30 seconds of silence and the app names it
on the dashboard, so a board that loses power is visible rather than silently
uncovered.

The camera board (4) is the exception: it sends no heartbeat, because its
frame pushes already are one. Every `/camera/recognize` POST calls
`state.set_camera_online(True)` on arrival — a board that's sending frames is
by definition alive, so a separate liveness ping would be redundant traffic.

## Board 4 — the camera (ESP32-CAM)

Two independent jobs, on two independent paths:

1. **Face recognition (push).** `sendFrame()` in `loop()` POSTs one JPEG to
   the backend's `/camera/recognize` every `CAPTURE_INTERVAL_MS`, with the
   shared device key. Byte-for-byte the same request `python/camera_worker.py`
   sends from a PC webcam, so the backend needs no special case for it.
2. **Live view (pull).** `startCameraServer()` serves `/stream` — a
   `multipart/x-mixed-replace` MJPEG feed. The backend connects to it and
   relays the bytes on to the app (`/camera/stream` in
   `python/backend/main.py`), so the phone never talks to this board directly
   and the feed works from anywhere the backend is reachable (Tailscale
   included), not just on the local WiFi.

The stream server uses **`esp_http_server`, not Arduino's `WebServer`** — and
that's load-bearing, not a style choice. `httpd_start()` runs the handler on
its own FreeRTOS task, so `streamHandler`'s per-client infinite loop never
touches `loop()`. With Arduino's `WebServer`, `handleClient()` would block
inside that handler for the whole streaming session, starving `sendFrame()`
completely for as long as anyone was watching — face recognition would stop
the moment someone opened the camera tab.

`/stream` checks the same device key, as an `X-Device-Key` header (what the
backend sends) or a `?key=` query param (a browser or VLC generally can't set
a custom header). Note the tradeoff this inherits from the project's existing
trust model: the key that views the stream is the same key that can report a
sensor breach.

`initCamera()` sets `grab_mode = CAMERA_GRAB_LATEST` with `fb_count = 2` on
purpose. Two consumers now pull frames concurrently (the stream task and
`sendFrame()`); `CAMERA_GRAB_LATEST` drops stale frames instead of queuing
them, so a slow consumer doesn't accumulate lag over a long-lived stream. On a
board without PSRAM the fallback is `fb_count = 1`, where those two consumers
contend for a single buffer and roughly halve each other — don't run both
features on a PSRAM-less module.

### Flashing it is different from the other three

The AI-Thinker ESP32-CAM has no auto-reset circuit, so a normal upload fails
with `Failed to connect to ESP32: No serial data received`. It has to be put
into bootloader mode by hand, every time:

1. Hold **BOOT** (IO0) down
2. Press and release **RESET** while still holding BOOT
3. Release BOOT
4. Start the upload immediately

After the upload finishes, press **RESET** alone to run the new firmware —
the post-flash auto-reset doesn't fire on this board either.

It also needs its own FQBN — `esp32:esp32:esp32cam`, not `esp32:esp32:esp32`.

`setup()` disables the brownout detector (`RTC_CNTL_BROWN_OUT_REG`) because
these modules are well known to brown out on the camera's init current spike
over a marginal USB cable. Worth remembering that this also *masks* a real
brownout during sustained streaming — which draws far more current, for far
longer, than the old interval capture did — so a hang or reset under load is a
power problem until proven otherwise.

## Verifying changes still compile

```powershell
& "C:\Program Files\Arduino CLI\arduino-cli.exe" compile --fqbn esp32:esp32:esp32 <sketch-folder>
```

(Requires `arduino-cli core install esp32:esp32` once, already done on
this dev machine.) Board 4 needs `--fqbn esp32:esp32:esp32cam` instead.
