// ==========================================
// HomeGuard - Door & Keypad Controller (board 3 of 3)
// Features: Real Door Sensor, Serial Keypad, Configurable Silent Delay,
// Accelerating Beeps, Alternating Siren, WiFi + Backend Integration
// ==========================================
//
// This is the arm/disarm AUTHORITY: it's the board with the keypad, so
// it's the one that decides whether the system is armed. The other two
// boards (ultrasonic, vibration) are dumb sensor-push clients that don't
// know or care about arm state at all — the backend decides whether
// their reports matter. This board is different: it needs to both SEND
// reports (state changes, door-breach alerts) and RECEIVE commands (the
// app arming/disarming remotely), so it runs a small WebServer in
// addition to being a WiFi client.
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

// --- Network settings ---
const char* ssid = "Free-TAU";
const char* password = "free-tau";

// The Python backend's address. Update to the real IP of the machine
// running uvicorn (see python/README.md). Port 8000 is uvicorn's default
// in this project.
const char* backendHost = "192.168.X.X";  // TODO: set to the real IP
const int backendPort = 8000;

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
const String SECRET_PIN = "1234";
// Digits are accepted in every state (so the PIN can be pre-typed before
// the door swings open and ENTRY_DELAY starts), but only ENTRY_DELAY and
// ALARM ever consume the buffer with '#'. Without a cap, keys pressed in
// any other state — or just something leaning on the keypad — grow this
// String forever on a device with ~300 KB of heap and no way to clear it
// short of a reboot. At the cap the buffer restarts rather than silently
// ignoring further digits, so a wrong entry can't wedge the keypad.
const unsigned int MAX_PIN_LENGTH = 8;  // SECRET_PIN is 4; the slack absorbs a mistype
// TODO: this reads the "keypad" from the Serial Monitor (type digits +
// '*' to arm / '#' to submit a PIN) — a deliberate stand-in for testing
// the state machine before the physical 4x4 keypad is wired up. Once it
// is, swap getSimulatedKey() for the Keypad library reading the real
// matrix (pin mapping isn't set yet, so not guessing it here).

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

// --- WiFi ---
// How long setup() waits for the initial association before giving up and
// booting anyway. See connectWiFi() for why it gives up at all.
const unsigned long WIFI_CONNECT_TIMEOUT = 15000;

// Brings up WiFi with a bounded wait instead of spinning until it
// succeeds. This board's keypad, door sensor and sirens are the parts
// that must work with no network whatsoever — an unreachable router is
// exactly the situation where you still want the house alarm to arm and
// scream. Blocking here meant a board that powered up before the router
// did (or during an outage) never reached loop() at all, so the entire
// local alarm stayed dead until someone noticed and rebooted it.
// setAutoReconnect lets the ESP32 core keep retrying in the background,
// so the backend link comes back on its own once the network does; every
// outbound report already checks WiFi.status() before trying.
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  unsigned long startedAt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startedAt < WIFI_CONNECT_TIMEOUT) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected");
    Serial.print("My IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nWiFi unavailable — running the alarm locally, retrying in background.");
    Serial.println("Remote arm/disarm and backend reports stay offline until it connects.");
  }
}

// Boot sequence: configure pins, force the buzzers quiet so they don't
// chirp on power-up, read the door's initial state, then bring up WiFi
// and the remote-control WebServer.
void setup() {
  Serial.begin(9600); // 9600 to ensure stable serial reading

  // Setup pins
  pinMode(DOOR_SENSOR_PIN, INPUT_PULLUP);
  pinMode(BUZZER_1_PIN, OUTPUT);
  pinMode(BUZZER_2_PIN, OUTPUT);

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
  connectWiFi();

  server.on("/arm", HTTP_POST, handleRemoteArm);
  server.on("/disarm", HTTP_POST, handleRemoteDisarm);
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

// ---- Backend reporting (new) ------------------------------------------

// Tells the backend this board's authoritative armed/disarmed state, so
// the app reflects reality even when arming/disarming happens at the
// physical keypad rather than through the app. Safe to call even if the
// backend already knows — it no-ops on its side if nothing changed.
void reportStateChange(bool armed) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot report state — WiFi disconnected");
    return;
  }
  HTTPClient http;
  String url = "http://" + String(backendHost) + ":" + String(backendPort) + "/esp32/state-change";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  String body = armed ? "{\"armed\":true}" : "{\"armed\":false}";
  int code = http.POST(body);
  if (code <= 0) {
    Serial.printf("Error reporting state: %s\n", http.errorToString(code).c_str());
  }
  http.end();
}

// Reports the door-breach-timeout alarm, same pattern as the other two
// boards use for their own sensors.
void reportDoorAlert() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = "http://" + String(backendHost) + ":" + String(backendPort) +
               "/esp32/sensor-event?sensor=door&reason=Door%20not%20disarmed%20in%20time";
  http.begin(url);
  http.POST("");
  http.end();
}

// Sends a periodic "I'm alive" ping to the backend (same pattern as the
// other two boards) so the app can show this board as online. Doesn't
// check the response code — a missed heartbeat just gets retried on the
// next interval, no special handling needed for a one-off failure.
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = "http://" + String(backendHost) + ":" + String(backendPort) + "/esp32/heartbeat";
  http.begin(url);
  http.POST("");
  http.end();
}

// ---- Remote control (new) ----------------------------------------------
// Only reachable from the backend, which itself only calls this from the
// app's own arm/disarm buttons — see esp32_client.py.

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
  Serial.println("\n[REMOTE] Disarm requested via app");
  currentState = DISARMED;
  enteredPin = "";
  silenceBuzzers();
  reportStateChange(false);
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

  // 2. Read the SIMULATED keypad
  char input = getSimulatedKey();

  // If a number is typed, hide it with an asterisk
  if (input >= '0' && input <= '9') {
    if (enteredPin.length() >= MAX_PIN_LENGTH) {
      enteredPin = "";  // see MAX_PIN_LENGTH — restart instead of growing
      Serial.println("\n[KEYPAD] Too many digits — entry cleared, start again.");
    }
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
        Serial.println("\nWARNING: Door opened! Enter PIN to disarm.");
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
            if (enteredPin == SECRET_PIN) {
              currentState = DISARMED;
              enteredPin = "";
              silenceBuzzers();
              Serial.println("Correct PIN. System DISARMED.");
              reportStateChange(false);
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
          if (enteredPin == SECRET_PIN) {
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
