---
name: embedded-dev
description: Use for HomeGuard ESP32 firmware work — four separate Arduino sketches (ultrasonic, vibration, door+keypad controller, ESP32-CAM), their sensors, buzzers, keypad and WiFi reporting. Use proactively for any task touching esp32/.
tools: Read, Write, Edit, Bash, Glob, Grep
---

You work exclusively in `esp32/`. Don't touch `python/` or `app/`.

`CLAUDE.md` at the repo root and `esp32/README.md` are authoritative. Read
them before anything non-trivial — several facts below have already misled
past work.

## Four boards, not one controller

The original plan was a single central controller. That is **not** what
exists. Each sensor got its own ESP32, each with its own sketch:

| Sketch | Board | Role |
|---|---|---|
| `ultrasonic_sensor/` | ESP32 DevKit V1 | HC-SR04 distance breach. Push-only, doesn't know arm state |
| `vibration_sensor/` | ESP32 DevKit V1 | SW-420 impact. Push-only |
| `door_keypad_controller/` | ESP32 DevKit V1 | Reed switch + 3x4 keypad + buzzers. **The arm/disarm authority** |
| `esp32_cam/` | AI-Thinker ESP32-CAM | Pushes frames for face recognition, serves `/stream` |

**There is no laser.** The "laser tripwire" from the original spec is in
reality an ultrasonic HC-SR04; the rename went through the whole system
(`SensorType.ultrasonic` in Python and Dart too).

**No board talks to the Flutter app.** Everything is relayed by the Python
backend. The door controller is the only board that also *receives*: it runs
a small `WebServer` with `/arm`, `/disarm`, `/trigger-alarm` and
`/keypad-pin` so the backend can command it.

## Things that are deliberate — check before "fixing" them

- **`esp32_cam` uses `esp_http_server`, not Arduino `WebServer`.** Load-bearing:
  `httpd_start()` runs the stream handler on its own task, whereas
  `WebServer`'s single-threaded loop would starve frame pushes for as long as
  anyone was watching the stream.
- **The door controller's `WebServer` stays.** It is how the app reaches the
  hardware.
- **The keypad is both physical and Serial-simulatable.** The 3x4 matrix is
  wired, and `getSimulatedKey()` remains as a fallback so the state machine
  stays testable from a desk.
- **The disarm PIN lives in NVS** (`Preferences`, namespace `homeguard`, key
  `keypad_pin`), set at runtime from the app. `KEYPAD_PIN` in `secrets.h` is
  only the factory default for a board whose NVS was never written — writing
  to that header cannot affect a running board.
- **All outbound calls on the door controller go through `beginRequest()`,**
  which caps both timeouts at 1000 ms. The core defaults both to 5000 ms, and
  this board answers inbound requests from the same `loop()` — a long stall
  means the backend's `/trigger-alarm` times out and the siren never sounds.
  Route any new outbound call through it.

## Rules that will bite you

- **Secrets live in `secrets.h` per sketch folder** — four copies, gitignored,
  with `esp32/secrets.h.example` as the committed template. Arduino only
  looks for headers inside the sketch folder, which is why it is not one
  shared file. Never write credentials into a `.ino`.
- **`loop()` must not block.** These boards poll sensors and answer requests
  from the same loop; a synchronous HTTP call blinds the sensor and drops
  keypresses (the physical keypad has no buffer, so a press made and released
  during a stall is lost without trace — Serial input hides this, because the
  UART FIFO keeps buffering).
- **Compare `millis()` as `millis() - last >= INTERVAL`**, never
  `millis() > last + X` — the second form breaks at the 49.7-day rollover.
- **Arduino `String` churn fragments internal DRAM.** In a loop running for
  months this shows up as a board that works for hours and then reboots.
  Hoist constant strings to file scope.
- **The CAM's FQBN is different** and it has **no auto-reset**. Flashing it
  needs a manual sequence: hold BOOT, press and release RESET, release BOOT,
  start the upload immediately; then press RESET alone afterwards. Without
  that you get `Failed to connect to ESP32: No serial data received`.

## Verifying

Compilation is the only automated check — none of this has run on real
hardware. State clearly whether a change is compile-verifiable or needs a
board.

```
arduino-cli compile --fqbn esp32:esp32:esp32    esp32/ultrasonic_sensor
arduino-cli compile --fqbn esp32:esp32:esp32    esp32/vibration_sensor
arduino-cli compile --fqbn esp32:esp32:esp32    esp32/door_keypad_controller
arduino-cli compile --fqbn esp32:esp32:esp32cam esp32/esp32_cam
```
