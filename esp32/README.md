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

## Before flashing

Each sketch has two placeholders near the top to fill in:

```cpp
const char* ssid = "Free-TAU";      // your WiFi network
const char* password = "free-tau";  // your WiFi password
const char* backendHost = "192.168.X.X";  // the IP of the machine running the Python backend
```

Find the backend machine's IP with `ipconfig` (Windows) on that machine,
on the same WiFi network as the boards.

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
(secret PIN is `1234`, change `SECRET_PIN` in the sketch). Swap in the
`Keypad` library once the physical keypad's pin mapping is known.

## Verifying changes still compile

```powershell
& "C:\Program Files\Arduino CLI\arduino-cli.exe" compile --fqbn esp32:esp32:esp32 <sketch-folder>
```

(Requires `arduino-cli core install esp32:esp32` once, already done on
this dev machine.)
