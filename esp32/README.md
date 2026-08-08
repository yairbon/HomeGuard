# HomeGuard ESP32 Firmware — 3 Boards

Three separate ESP32 boards, each its own sketch (Arduino IDE requires the
folder name to match the `.ino` filename — already set up that way).

| Board | Sketch | Role |
|---|---|---|
| 1 | `ultrasonic_sensor/` | HC-SR04 distance sensor — perimeter breach detection |
| 2 | `vibration_sensor/` | Digital vibration sensor (SW420-style) — forced-entry detection |
| 3 | `door_keypad_controller/` | Magnetic door sensor + keypad + buzzers — **the arm/disarm authority** |

Boards 1 and 2 are simple: they detect a breach and POST it to the
backend. They don't know or care whether the system is armed — the
backend decides that (see `python/backend/state.py`, `trigger_alert`).

Board 3 is different: it owns arm/disarm (it has the keypad), so it both
sends reports to the backend *and* receives commands from it (the app's
arm/disarm buttons reach this board through the backend).

## Before flashing — `secrets.h`

Copy `secrets.h.example` into **each of the three sketch folders** as
`secrets.h` and fill in the real values:

```
esp32/ultrasonic_sensor/secrets.h
esp32/vibration_sensor/secrets.h
esp32/door_keypad_controller/secrets.h
```

Three copies rather than one shared file because the Arduino IDE compiles one
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

Each board pings `/esp32/heartbeat?board=<name>` every 10 seconds, using the
name the backend knows it by (`ultrasonic`, `vibration`, `door_controller` —
see `BOARD_NAMES` in `python/backend/state.py`). The backend marks a board
offline after 30 seconds of silence and the app names it on the dashboard, so
a board that loses power is visible rather than silently uncovered.

## Verifying changes still compile

```powershell
& "C:\Program Files\Arduino CLI\arduino-cli.exe" compile --fqbn esp32:esp32:esp32 <sketch-folder>
```

(Requires `arduino-cli core install esp32:esp32` once, already done on
this dev machine.)
