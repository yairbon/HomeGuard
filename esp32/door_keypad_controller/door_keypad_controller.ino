// ==========================================
// HomeGuard - Door & Keypad Controller (board 3 of 4)
// Features: Real Door Sensor, Serial Keypad, Configurable Silent Delay,
// Accelerating Beeps, Alternating Siren, WiFi + Backend Integration
// ==========================================
//
// This is the arm/disarm AUTHORITY: it's the board with the keypad, so
// it's the one that decides whether the system is armed. The other two
// sensor boards (ultrasonic, vibration) are dumb sensor-push clients that
// don't know or care about arm state at all — the backend decides whether
// their reports matter — and the fourth board (esp32_cam) only pushes
// frames and serves its own stream. This board is different: it needs to
// both SEND reports (state changes, door-breach alerts) and RECEIVE
// commands (the app arming/disarming remotely), so it runs a small
// WebServer in addition to being a WiFi client.
//
// Everything under "STATE MACHINE LOGIC" below is unchanged from the
// original draft — it already worked. What's new: WiFi connection, the
// WebServer for remote /arm and /disarm, and reportStateChange() /
// reportDoorAlert() calls at the state-machine's existing transition
// points so the backend (and therefore the app) stay in sync with
// whatever this board decides, whether that came from the keypad or
// from the app.

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <Preferences.h>

// WiFi credentials, backend address, the shared device key and the keypad
// PIN all live in secrets.h, which is gitignored. Copy
// esp32/secrets.h.example into this folder as secrets.h and fill it in.
// These used to be literals in this file, which meant the WiFi password and
// the disarm PIN were committed to the repository in plain text.
#include "secrets.h"

// --- Network settings ---
const char* ssid = WIFI_SSID;
const char* password = WIFI_PASSWORD;

// The Python backend's address — see secrets.h.
const char* backendHost = BACKEND_HOST;
const int backendPort = BACKEND_PORT;

// How this board identifies itself to the backend's per-board liveness
// tracking (see BOARD_NAMES in python/backend/state.py).
const char* BOARD_NAME = "door_controller";

// This board also runs its OWN server on port 80, so the backend can
// send it /arm and /disarm commands (see python/backend/esp32_client.py).
// After boot, check the Serial Monitor for "My IP" and set
// HOMEGUARD_DOOR_CONTROLLER_URL to http://<that IP> when running the
// backend.
WebServer server(80);

// --- Pin Definitions ---
const int DOOR_SENSOR_PIN = 4;  // Magnetic Reed Switch
const int BUZZER_1_PIN = 23;    // Buzzer 1
const int BUZZER_2_PIN = 22;    // Buzzer 2

// --- Physical keypad (3x4: digits 1-9, *, 0, #) ---
// Rows are driven OUTPUT (idle HIGH, pulled LOW one at a time while
// scanning); columns are read INPUT_PULLUP (idle HIGH, read LOW when the
// active row's pulse reaches them through a pressed key). None of these
// are strapping pins except R3=12 (MTDI) — that one only matters at boot,
// and this board's own bring-up test showed it doesn't block flashing in
// practice, so it's left as wired rather than asking for a rewire.
// Order confirmed against the physical wiring (right-to-left on the
// connector): R1=25 R2=13 R3=12 R4=27, C1=26 C2=33 C3=14.
const int ROW_PINS[4] = {25, 13, 12, 27};
const int COL_PINS[3] = {26, 33, 14};
const char KEYPAD_LAYOUT[4][3] = {
  {'1', '2', '3'},
  {'4', '5', '6'},
  {'7', '8', '9'},
  {'*', '0', '#'}
};

// --- State Machine Definitions ---
enum State {
  DISARMED,
  PENDING_ARM,   // Waiting to open door (local arm, via keypad)
  EXITING,       // Waiting to close door (local arm, via keypad)
  REMOTE_ARMING, // Countdown after an app-initiated /arm — no door walk-out needed
  ARMED,         // House is secured
  ENTRY_DELAY,   // Countdown running (someone opened the door — could be the resident, or a break-in)
  ALARM          // Sirens sounding
};

State currentState = DISARMED;

// --- Keypad & Security Variables ---
String enteredPin = "";

// The physical 3x4 matrix is now wired (see ROW_PINS/COL_PINS above) and
// read by getPhysicalKey() below. getSimulatedKey() (Serial Monitor input)
// is kept as a fallback, not removed — see getKey() — so the state machine
// stays testable from a desk even without the physical keypad in hand, and
// so a wiring mistake on one row/column doesn't block testing the rest.

// The disarm PIN, changeable from the app at runtime.
//
// KEYPAD_PIN in secrets.h is the *factory default*, compiled in and used
// only on a board whose NVS has never been written. The live value lives in
// NVS (flash) and is what loadKeypadPin() below puts here at every boot, so
// a PIN set from the app survives power loss and reboots. It has to work
// this way: secrets.h is a C++ header baked into the binary at compile time,
// so writing to it can only affect the *next* flash, never a board already
// running. The backend keeps secrets.h in sync anyway, which is what stops a
// reflash from silently restoring an old PIN.
String secretPin = KEYPAD_PIN;

// NVS handle for the "homeguard" namespace. Opened per operation rather
// than held open, so a write is committed and the handle released before
// anything else can touch it.
Preferences prefs;
const char* PREFS_NAMESPACE = "homeguard";
const char* PREFS_PIN_KEY = "keypad_pin";

// A PIN must be 4-8 characters and digits only — the keypad's other two
// keys ('*' arms, '#' submits) can never appear in one, so accepting them
// would create a PIN that cannot be typed. Enforced identically on the
// backend; both sides validate because either can be reached alone.
bool isValidPin(const String& pin) {
  if (pin.length() < 4 || pin.length() > 8) return false;
  for (unsigned int i = 0; i < pin.length(); i++) {
    if (!isDigit(pin[i])) return false;
  }
  return true;
}

// Reads the stored PIN out of NVS into `secretPin`, leaving the secrets.h
// default in place if nothing valid is stored. Called once from setup().
void loadKeypadPin() {
  prefs.begin(PREFS_NAMESPACE, /*readOnly=*/true);
  String stored = prefs.getString(PREFS_PIN_KEY, "");
  prefs.end();

  if (isValidPin(stored)) {
    secretPin = stored;
    Serial.println("Keypad PIN loaded from NVS (set via the app).");
  } else {
    Serial.println("No PIN in NVS — using the KEYPAD_PIN default from secrets.h.");
  }
}

// Persists a new PIN to NVS and switches to it immediately. Returns false
// without changing anything if the PIN is malformed or the write fails.
bool saveKeypadPin(const String& pin) {
  if (!isValidPin(pin)) return false;

  prefs.begin(PREFS_NAMESPACE, /*readOnly=*/false);
  size_t written = prefs.putString(PREFS_PIN_KEY, pin);
  prefs.end();

  if (written == 0) {
    Serial.println("Failed to write the new PIN to NVS — keeping the previous one.");
    return false;
  }
  secretPin = pin;
  // Deliberately never printed. This board's Serial output is the one place
  // a PIN could leak to someone with physical USB access to a board they
  // don't own the code for.
  Serial.println("Keypad PIN updated and stored in NVS.");
  return true;
}

// --- Sensor & Timer Variables ---
bool isDoorOpen = false;
int lastDoorState = LOW;
unsigned long delayStartTime = 0;

const unsigned long ENTRY_TIME_LIMIT = 25000;   // 25 seconds total to disarm before ALARM
const unsigned long SILENCE_BEFORE_BEEP = 2000; // 2 seconds of silence before warning beeps start
const unsigned long REMOTE_ARM_DELAY = 3000;    // exit delay for app-initiated arming (no door needed)

// --- Beep/Siren Variables ---
unsigned long lastBeepTime = 0;
bool isBuzzerCurrentlyOn = false;
const int BEEP_ON_DURATION = 150;     // Beep stays on for 150ms

// --- Heartbeat ---
unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 10000;

// Boot sequence: configure pins, force the buzzers quiet so they don't
// chirp on power-up, read the door's initial state, then bring up WiFi
// and the remote-control WebServer.
void setup() {
  Serial.begin(9600); // 9600 to ensure stable serial reading

  // Setup pins
  pinMode(DOOR_SENSOR_PIN, INPUT_PULLUP);
  pinMode(BUZZER_1_PIN, OUTPUT);
  pinMode(BUZZER_2_PIN, OUTPUT);

  // Keypad: rows idle HIGH (driven), columns read with internal pull-ups so
  // no external resistors are needed.
  for (int r = 0; r < 4; r++) {
    pinMode(ROW_PINS[r], OUTPUT);
    digitalWrite(ROW_PINS[r], HIGH);
  }
  for (int c = 0; c < 3; c++) {
    pinMode(COL_PINS[c], INPUT_PULLUP);
  }

  // Ensure buzzers are quiet on boot
  silenceBuzzers();

  // Get initial door state
  lastDoorState = digitalRead(DOOR_SENSOR_PIN);
  isDoorOpen = (lastDoorState == HIGH); // HIGH means magnet is removed (open)
  delay(1000); // Boot settle pause. Longer than an INPUT_PULLUP actually
               // needs to stabilize (that's microseconds) — carried over
               // unexplained from the original draft; harmless, so left
               // as-is since this pass is documentation-only.

  // --- WiFi + remote-control server ---
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected");
  Serial.print("My IP: ");
  Serial.println(WiFi.localIP());

  // WebServer discards every request header it wasn't explicitly told to
  // keep, so this line is load-bearing: without it server.header("X-Device-Key")
  // always returns "" and authorized() would reject every legitimate command
  // from the backend.
  const char* collectedHeaders[] = {"X-Device-Key"};
  server.collectHeaders(collectedHeaders, 1);

  server.on("/arm", HTTP_POST, handleRemoteArm);
  server.on("/disarm", HTTP_POST, handleRemoteDisarm);
  server.on("/trigger-alarm", HTTP_POST, handleRemoteTriggerAlarm);
  server.on("/keypad-pin", HTTP_POST, handleSetKeypadPin);
  server.begin();
  Serial.println("Remote control server started.");

  Serial.println("\n====================================");
  Serial.println("Security System Booted.");
  Serial.print("Initial State: DISARMED | Door: ");
  Serial.println(isDoorOpen ? "OPEN" : "CLOSED");
  Serial.println("Type '*' to arm (Door must be closed).");
  Serial.println("====================================");
}

// Function to safely turn off the alarm hardware
void silenceBuzzers() {
  digitalWrite(BUZZER_1_PIN, LOW);
  digitalWrite(BUZZER_2_PIN, LOW);
  isBuzzerCurrentlyOn = false;
}

// Reads one character typed into the Serial Monitor, standing in for the
// physical keypad (see TODO above). Filters out '\n'/'\r' so pressing
// Enter after typing a line doesn't get treated as a keypress, and
// returns '\0' — a sentinel a real keystroke never produces — when
// nothing new is waiting; callers just compare against digits/'*'/'#'
// and otherwise ignore it.
char getSimulatedKey() {
  if (Serial.available() > 0) {
    char incoming = Serial.read();
    if (incoming != '\n' && incoming != '\r') {
      return incoming;
    }
  }
  return '\0';
}

// Scans the physical 3x4 matrix: drives one row LOW at a time (all others
// idle HIGH) and checks which column reads LOW — that intersection is the
// key currently pressed. Returns '\0' when nothing is pressed, same
// contract as getSimulatedKey(), so callers don't need to care which
// source produced a keypress.
//
// Debounced by requiring a full release (key reads '\0') before the same
// key can register again, plus a minimum gap between presses — without
// this, a single physical press would be read dozens of times across
// consecutive loop() iterations (loop has no fixed delay) and flood
// enteredPin with repeated digits.
const unsigned long KEYPAD_DEBOUNCE_MS = 200;
unsigned long lastKeyTime = 0;
bool keyWasDown = false;

char getPhysicalKey() {
  char found = '\0';
  for (int r = 0; r < 4 && found == '\0'; r++) {
    for (int rr = 0; rr < 4; rr++) {
      digitalWrite(ROW_PINS[rr], rr == r ? LOW : HIGH);
    }
    delayMicroseconds(50); // let the line settle before sampling columns
    for (int c = 0; c < 3; c++) {
      if (digitalRead(COL_PINS[c]) == LOW) {
        found = KEYPAD_LAYOUT[r][c];
        break;
      }
    }
  }
  for (int rr = 0; rr < 4; rr++) digitalWrite(ROW_PINS[rr], HIGH); // back to idle

  if (found == '\0') {
    keyWasDown = false;
    return '\0';
  }
  if (keyWasDown || millis() - lastKeyTime < KEYPAD_DEBOUNCE_MS) {
    return '\0'; // still holding the previous press, or debounce window open
  }
  keyWasDown = true;
  lastKeyTime = millis();
  return found;
}

// Tries the physical keypad first, falls back to Serial input — see the
// comment above secretPin for why both stay live at once.
char getKey() {
  char physical = getPhysicalKey();
  if (physical != '\0') return physical;
  return getSimulatedKey();
}

// ---- Backend reporting (new) ------------------------------------------

// Backend URL builder, so the host/port concatenation isn't repeated in
// every reporting function below.
String backendUrl(const char* path) {
  return "http://" + String(backendHost) + ":" + String(backendPort) + String(path);
}

// How long any single outbound call may take before we give up.
//
// This matters far more on this board than on the sensor boards, because
// this is the only one that also *answers* requests. Every reporting
// function below runs synchronously inside loop(), and loop() calls
// server.handleClient() exactly once per pass — so for as long as a report
// is in flight, this board answers nothing: not /arm, not /disarm, and not
// /trigger-alarm, which the backend calls when a *different* board reports a
// break-in. The backend gives that call 3 seconds (esp32_client.py) and then
// reports hardware: false, leaving the buzzers silent through an actual
// intrusion.
//
// The Arduino core defaults both the connect and the response timeout to
// HTTPCLIENT_DEFAULT_TCP_TIMEOUT = 5000ms, so one unlucky call could block
// for ~10 seconds. 1000ms each caps the worst case at ~2s, comfortably
// inside the backend's budget. Reports are already fire-and-forget with no
// retry, so the cost of being stricter is a dropped report, not a wrong one.
//
// Same window silently swallows keypresses: getPhysicalKey() only sees a key
// that is down at the sampling instant and there is no buffer, so anything
// pressed and released while we're blocked is lost. Serial-simulated input
// doesn't show this (the UART FIFO keeps buffering), which is why it will
// first appear when the physical keypad is wired.
const uint16_t HTTP_TIMEOUT_MS = 1000;

// Opens an outbound request with the timeouts applied. Every call site below
// goes through this rather than calling http.begin() directly, so a new
// reporting function can't quietly reintroduce the 10-second stall.
void beginRequest(HTTPClient& http, const String& url) {
  http.begin(url);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
}

// Tells the backend this board's authoritative armed/disarmed state, so
// the app reflects reality even when arming/disarming happens at the
// physical keypad rather than through the app. Safe to call even if the
// backend already knows — it no-ops on its side if nothing changed.
//
// This is the single most security-sensitive message this board sends: a
// forged "armed:false" would make the app show a disarmed system. Hence the
// X-Device-Key header, which the backend requires on every /esp32/* route.
void reportStateChange(bool armed) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot report state — WiFi disconnected");
    return;
  }
  HTTPClient http;
  beginRequest(http, backendUrl("/esp32/state-change"));
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  String body = armed ? "{\"armed\":true}" : "{\"armed\":false}";
  int code = http.POST(body);
  if (code == 401) {
    Serial.println("State report REJECTED (401) — DEVICE_KEY in secrets.h doesn't match the backend's.");
  } else if (code <= 0) {
    Serial.printf("Error reporting state: %s\n", http.errorToString(code).c_str());
  }
  http.end();
}

// Tells the backend the disarm countdown has started, and how long it runs.
//
// Without this the board ran the whole ENTRY_TIME_LIMIT window silently and
// only reported the outcome, so the app jumped straight from ARMED to
// TRIGGERED — someone watching remotely never saw that a person had walked
// in, and the person walking in had no countdown on their phone. The duration
// is sent rather than hardcoded on the backend so changing ENTRY_TIME_LIMIT
// here stays correct everywhere.
void reportEntryDelay() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  beginRequest(http, backendUrl("/esp32/entry-delay"));
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.POST("{\"seconds\":" + String(ENTRY_TIME_LIMIT / 1000) + "}");
  http.end();
}

// Reports the door-breach-timeout alarm, same pattern as the other two
// sensor boards use for their own sensors.
void reportDoorAlert() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  beginRequest(http, backendUrl("/esp32/sensor-event?sensor=door&board=door_controller"
                                "&reason=Door%20not%20disarmed%20in%20time"));
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.POST("");
  http.end();
}

// Sends a periodic "I'm alive" ping to the backend (same pattern as the
// other two sensor boards; the camera board sends none, because its frame
// pushes already are one) so the app can show this board as online. Doesn't
// check the response code — a missed heartbeat just gets retried on the
// next interval, no special handling needed for a one-off failure.
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  beginRequest(http, backendUrl("/esp32/heartbeat?board=") + BOARD_NAME);
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.POST("");
  http.end();
}

// ---- Remote control (new) ----------------------------------------------
// Called by the backend, which itself only calls it from the app's own
// arm/disarm buttons — see esp32_client.py.

// Verifies the caller presented the shared device key, replying 401 and
// returning false if not.
//
// This board listens on port 80 for anything on the local network, and
// /disarm unconditionally silences a sounding alarm — so without this check,
// anyone who found this board's IP could disarm the system with a single
// unauthenticated POST. The backend sends the key as X-Device-Key on every
// command (see _post in python/backend/esp32_client.py).
bool authorized() {
  if (server.hasHeader("X-Device-Key") && server.header("X-Device-Key") == String(DEVICE_KEY)) {
    return true;
  }
  Serial.println("\n[REMOTE] Rejected a command with a missing/wrong device key");
  server.send(401, "text/plain", "Unauthorized");
  return false;
}

// WebServer handler for POST /arm. Pulls the mode out of the JSON body
// with a plain substring search instead of a real JSON parser — no JSON
// library is pulled in just for this one field, and it's safe because
// the backend always sends exactly {"mode":"home"} or {"mode":"away"}
// (see esp32_client.py send_arm). Note: isHomeMode below only affects
// the Serial log — this board's state machine doesn't branch on Home vs
// Away; that distinction is enforced entirely on the backend side
// (state.py bypasses the vibration sensor's alerts in Home mode
// regardless of what this board reports).
void handleRemoteArm() {
  if (!authorized()) return;
  String body = server.arg("plain");
  bool isHomeMode = body.indexOf("home") >= 0;
  Serial.print("\n[REMOTE] Arm requested via app (");
  Serial.print(isHomeMode ? "Home" : "Away");
  Serial.println(" mode)");

  // Remote arming doesn't require physically walking out the door —
  // skip straight to a short countdown instead of PENDING_ARM/EXITING.
  if (currentState == DISARMED) {
    currentState = REMOTE_ARMING;
    delayStartTime = millis();
  }
  // Always responds 200 OK even when the request above was a no-op
  // (e.g. system was already ARMED) — caller currently can't tell
  // "armed" apart from "ignored, already armed" from the response alone.
  server.send(200, "text/plain", "OK");
}

// WebServer handler for POST /disarm. Unlike handleRemoteArm, this has
// no state guard: disarming has to work from any state (including
// mid-ALARM), so it unconditionally resets to DISARMED, clears any
// partially-typed PIN, and cuts the buzzers.
void handleRemoteDisarm() {
  if (!authorized()) return;
  Serial.println("\n[REMOTE] Disarm requested via app");
  currentState = DISARMED;
  enteredPin = "";
  silenceBuzzers();
  // Answer first, then report. The backend's /disarm is blocked on this
  // response with a 3-second budget; reporting first chained a second
  // backend round trip inside that budget, so one WiFi retransmit made a
  // disarm that actually worked come back as hardware: false — the app
  // telling the user the board wasn't reached, while the siren it just
  // silenced sits quiet next to them. (No deadlock either way: by the time
  // this report lands, state.disarm() has already run and
  // report_state_from_esp32 early-returns on the state it already agrees
  // with.)
  server.send(200, "text/plain", "OK");
  reportStateChange(false);
}

// WebServer handler for POST /trigger-alarm. Called by the backend when a
// *different* board (ultrasonic, vibration) reports a breach while armed —
// this board's own buzzers otherwise only react to its own door sensor and
// keypad, so without this, a breach seen anywhere else in the house shows
// up in the app but stays silent here. No state guard, same reasoning as
// handleRemoteDisarm: trust the backend's decision over whatever this board
// locally thinks the state is.
// WebServer handler for POST /keypad-pin. Sets the disarm PIN from the app,
// via the backend (see set_keypad_pin in python/backend/state.py).
//
// Body is {"pin":"1234"}, parsed with the same substring approach the other
// handlers use rather than pulling in a JSON library for one field. The
// value is validated here as well as on the backend: this route is reachable
// by anything on the network that has the device key, so it cannot rely on
// the backend having checked first.
//
// Answers before doing anything slow, for the same reason handleRemoteDisarm
// does — the backend is blocked on this response inside a 3-second budget.
// The NVS write is local flash (single-digit milliseconds), not a network
// call, so ordering matters less here than it does there; it is done this way
// for consistency and so a future retry loop can't stall the caller.
void handleSetKeypadPin() {
  if (!authorized()) return;

  String body = server.arg("plain");
  int start = body.indexOf("\"pin\"");
  if (start >= 0) {
    start = body.indexOf('"', body.indexOf(':', start) + 1);
  }
  int end = start >= 0 ? body.indexOf('"', start + 1) : -1;
  String pin = (start >= 0 && end > start) ? body.substring(start + 1, end) : "";

  if (!saveKeypadPin(pin)) {
    // The PIN itself never goes into the response or the log — only whether
    // it was accepted.
    Serial.println("\n[REMOTE] Rejected a keypad-PIN change (must be 4-8 digits)");
    server.send(400, "text/plain", "Invalid PIN");
    return;
  }

  Serial.println("\n[REMOTE] Keypad PIN changed via app");
  server.send(200, "text/plain", "OK");
}

void handleRemoteTriggerAlarm() {
  if (!authorized()) return;
  Serial.println("\n[REMOTE] Alarm triggered remotely (another sensor reported a breach)");
  currentState = ALARM;
  enteredPin = "";
  lastBeepTime = millis();
  server.send(200, "text/plain", "OK");
}

// Main loop: services the remote-control WebServer, polls the physical
// door sensor and the simulated keypad, advances the arm/disarm state
// machine below, and sends a periodic heartbeat.
void loop() {
  // Must be called every loop for the WebServer to actually respond to
  // incoming /arm and /disarm requests.
  server.handleClient();

  // 1. Read the PHYSICAL door sensor
  int currentDoorState = digitalRead(DOOR_SENSOR_PIN);

  // Detect if the door just changed state
  if (currentDoorState != lastDoorState) {
    isDoorOpen = (currentDoorState == HIGH);

    if (isDoorOpen) {
      Serial.println("\n[SENSOR] Door OPENED");
    } else {
      Serial.println("\n[SENSOR] Door CLOSED");
    }
    lastDoorState = currentDoorState;
  }

  // 2. Read a keypress — physical keypad first, Serial Monitor as fallback
  char input = getKey();

  // If a number is typed, hide it with an asterisk
  if (input >= '0' && input <= '9') {
    enteredPin += input;
    Serial.print("*");
  }

  // 3. --- STATE MACHINE LOGIC ---
  switch (currentState) {

    // -----------------------------------------
    case DISARMED:
      // You must have the door closed to start the arming process
      if (input == '*' && !isDoorOpen) {
        currentState = PENDING_ARM;
        Serial.println("\nSystem PENDING. Please open the door to exit.");
      } else if (input == '*' && isDoorOpen) {
        Serial.println("\nCannot arm: Door must be closed first.");
      }
      break;

    // -----------------------------------------
    case PENDING_ARM:
      // Waiting for you to open the door to leave
      if (isDoorOpen) {
        currentState = EXITING;
        Serial.println("\nDoor opened. Please exit and close the door to arm.");
      }
      if (input == '*') { // Cancel arming
        currentState = DISARMED;
        Serial.println("\nArming cancelled. State: DISARMED");
      }
      break;

    // -----------------------------------------
    case EXITING:
      // Waiting for the physical act of closing the door behind you
      if (!isDoorOpen) {
        currentState = ARMED;
        Serial.println("\nDoor closed. System is now ARMED.");
        reportStateChange(true);
      }
      if (input == '*') { // Cancel arming
        currentState = DISARMED;
        Serial.println("\nArming cancelled. State: DISARMED");
      }
      break;

    // -----------------------------------------
    case REMOTE_ARMING:
      // App-initiated arm — no door cycle needed, just a short countdown
      // (mirrors the backend's own ARMING_EXIT_DELAY, so the app's
      // "Arming..." UI and the physical hardware agree on timing).
      if (millis() - delayStartTime >= REMOTE_ARM_DELAY) {
        currentState = ARMED;
        Serial.println("\nRemote arm countdown complete. System is now ARMED.");
        reportStateChange(true);
      }
      if (input == '*') { // Cancel arming
        currentState = DISARMED;
        Serial.println("\nArming cancelled. State: DISARMED");
      }
      break;

    // -----------------------------------------
    case ARMED:
      // If the door opens, trigger the countdown
      if (isDoorOpen) {
        currentState = ENTRY_DELAY;
        delayStartTime = millis();
        lastBeepTime = millis();
        isBuzzerCurrentlyOn = false;
        // Start the disarm window with an empty buffer. Digits accumulate in
        // every state, and none of DISARMED/PENDING_ARM/EXITING/REMOTE_ARMING/
        // ARMED handles '#', so anything typed or brushed against the keypad
        // earlier is still sitting in enteredPin. Without this reset, one
        // stray digit turns a correctly-typed PIN into "51913" != "1913" and
        // the siren goes off on a legitimate entry — recoverable only by
        // knowing to press '#' twice. The ENTRY_DELAY -> ALARM transition
        // already cleared it; this is the matching clear on the way in.
        enteredPin = "";
        Serial.println("\nWARNING: Door opened! Enter PIN to disarm.");
        reportEntryDelay();
      }
      break;

    // -----------------------------------------
    case ENTRY_DELAY:
      {
        unsigned long elapsed = millis() - delayStartTime;

        // 1. Check if they ran out of time
        if (elapsed >= ENTRY_TIME_LIMIT) {
          currentState = ALARM;
          enteredPin = "";
          lastBeepTime = millis();
          Serial.println("\n*** ALARM TRIGGERED! SIRENS SOUNDING! ***");
          reportDoorAlert();
        }
        else {
          // 2. Handle keypad to disarm
          if (input == '#') {
            Serial.println();
            if (enteredPin == secretPin) {
              currentState = DISARMED;
              enteredPin = "";
              silenceBuzzers();
              Serial.println("Correct PIN. System DISARMED.");
              reportStateChange(false);
              // Without this break, execution falls through to the
              // accelerating-beep logic below (still inside this case,
              // same loop() pass) — by then enough time has elapsed since
              // lastBeepTime that it immediately turns the buzzers back
              // on, undoing silenceBuzzers() above. Disarming is done;
              // nothing after this point should run this iteration.
              break;
            } else {
              enteredPin = "";
              Serial.println("Wrong PIN. Keep trying!");
            }
          }

          // 3. Accelerating Beep Logic WITH SILENT DELAY
          if (elapsed < SILENCE_BEFORE_BEEP) {
            // Do nothing yet, keep it silent
            silenceBuzzers();
          }
          else {
            // The silent time has passed, start the warning beeps!
            // Map the remaining time (from when silence ends to the time limit)
            int currentOffDuration = map(elapsed, SILENCE_BEFORE_BEEP, ENTRY_TIME_LIMIT, 1500, 60);
            unsigned long timeSinceLastChange = millis() - lastBeepTime;

            if (isBuzzerCurrentlyOn) {
              if (timeSinceLastChange >= BEEP_ON_DURATION) {
                silenceBuzzers();
                lastBeepTime = millis();
              }
            } else {
              if (timeSinceLastChange >= currentOffDuration) {
                digitalWrite(BUZZER_1_PIN, HIGH);
                digitalWrite(BUZZER_2_PIN, HIGH);
                isBuzzerCurrentlyOn = true;
                lastBeepTime = millis();
              }
            }
          }
        }
      }
      break;

    // -----------------------------------------
    case ALARM:
      {
        // 1. Continuous Alternating Siren Effect
        unsigned long timeSinceLastSirenChange = millis() - lastBeepTime;
        if (timeSinceLastSirenChange > 100) { // Toggle every 100ms for a harsh alarm sound
          isBuzzerCurrentlyOn = !isBuzzerCurrentlyOn;
          digitalWrite(BUZZER_1_PIN, isBuzzerCurrentlyOn ? HIGH : LOW);
          digitalWrite(BUZZER_2_PIN, isBuzzerCurrentlyOn ? LOW : HIGH);
          lastBeepTime = millis();
        }

        // 2. The only way to stop the alarm is entering the correct PIN
        if (input == '#') {
          Serial.println();
          if (enteredPin == secretPin) {
            currentState = DISARMED;
            enteredPin = "";
            silenceBuzzers();
            Serial.println("Correct PIN. ALARM SILENCED. System DISARMED.");
            reportStateChange(false);
          } else {
            enteredPin = "";
            Serial.println("Wrong PIN! ALARM STILL SOUNDING!");
          }
        }
      }
      break;
  }

  // Periodic heartbeat, doesn't block the state machine's tight timing
  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }
}

