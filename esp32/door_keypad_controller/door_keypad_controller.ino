// ==========================================
// HomeGuard - Door & Keypad Controller (board 3 of 4)
// Features: Real Door Sensor, Serial Keypad, Configurable Silent Delay,
// Accelerating Beeps, Siren, Keypad/Status Sounds, WiFi + Backend Integration
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

// Bench-test hook: 1 = characters typed into the Serial Monitor count as key
// presses (arm with '*', PIN + '#'), so the state machine can be driven
// without the keypad. 0 (the shipped default) = only the physical keypad,
// because anyone holding a USB cable should not be able to type at the panel.
#ifndef ENABLE_SERIAL_KEYPAD
#define ENABLE_SERIAL_KEYPAD 0
#endif

// ==========================================================================
// SOUNDS - every sound this board makes is defined in this one block, so
// tuning by ear is a number edit. Nothing in here decides WHEN a sound plays:
// that is setState() and the entry-delay code further down.
// ==========================================================================
//
// Buzzer type. Both buzzers are driven with a plain HIGH/LOW level and they
// beep, so they are ACTIVE buzzers: each has its own oscillator and sounds
// one fixed pitch whenever its pin is HIGH. For those, the "Hz" numbers below
// only mean on (non-zero) or off (0); the sounds differ by rhythm and by
// which buzzer plays. If the buzzers turn out to be passive piezos, set this
// to 0: the very same table then plays the Hz values as real pitches (LEDC).
#ifndef BUZZERS_ACTIVE
#define BUZZERS_ACTIVE 1
#endif

// One step of a sound: what each buzzer does for `ms` milliseconds.
struct SoundStep {
  uint16_t hz1;  // buzzer 1 (GPIO23): 0 = silent, else pitch (passive) / "on" (active)
  uint16_t hz2;  // buzzer 2 (GPIO22): same
  uint16_t ms;   // length of the step, must be > 0
};

// Priority, highest first:  siren  >  entry-delay beeps  >  status sounds
// >  key clicks.  A higher one silences the lower ones; while the siren runs,
// clicks and status sounds are not played at all. The entry-delay beeps only
// claim the buzzer(s) named in ENTRY_BEEP_HZ1/2 below, so a click or status
// sound still plays on a buzzer they leave free. On top of that the countdown
// beeps pause while someone is typing (TYPING_QUIET_MS below), so keys and the
// wrong-PIN / disarmed result are never played over them.

// Key press: one short click, buzzer 1 only, every key.
const SoundStep CLICK_STEPS[] = { {3000, 0, 40} };

// Armed: short-short-long. On passive buzzers it rises in pitch; on active
// ones it is B1, B1, then B2, which rises only if B2 is the higher-pitched one.
const SoundStep ARMED_STEPS[] = {
  {2000, 0, 90}, {0, 0, 60}, {2600, 0, 90}, {0, 0, 60}, {0, 3200, 220},
};

// Disarmed (also "arming cancelled"): long-short-short, the mirror of armed.
const SoundStep DISARMED_STEPS[] = {
  {0, 3200, 220}, {0, 0, 60}, {2600, 0, 90}, {0, 0, 60}, {2000, 0, 90},
};

// Wrong PIN: one long buzz on both buzzers (low, on passive buzzers). Much
// longer than any beep or click, and not a rhythm, so it can't be mistaken for
// the armed / disarmed sounds or the countdown.
const SoundStep WRONG_PIN_STEPS[] = { {400, 400, 700} };

// Refused ("can't do that", e.g. arming with the door open): a rapid stutter of
// four short pulses on both buzzers. Nothing else sounds like it: a click is one
// 40 ms pulse, the countdown beeps are 150 ms, the jingles end or start long.
const SoundStep REFUSED_STEPS[] = {
  {2400, 2400, 60}, {0, 0, 60}, {2400, 2400, 60}, {0, 0, 60},
  {2400, 2400, 60}, {0, 0, 60}, {2400, 2400, 60},
};

// Siren: plays in a loop for as long as the state is ALARM.
#if BUZZERS_ACTIVE
// Pitch can't change, so the two buzzers swap: B1 on / B2 off, then B1 off /
// B2 on, with no silent gap. 2 steps x 50 ms = 10 swaps per second.
const SoundStep SIREN_STEPS[] = { {2800, 0, 50}, {0, 3400, 50} };
#else
// Each buzzer sweeps up and down, mirrored so they are never at the same
// pitch. 8 steps x 15 ms = 120 ms per up-down cycle, about 8 per second.
const SoundStep SIREN_STEPS[] = {
  {2000, 3500, 15}, {2500, 3000, 15}, {3000, 2500, 15}, {3500, 2000, 15},
  {3500, 2000, 15}, {3000, 2500, 15}, {2500, 3000, 15}, {2000, 3500, 15},
};
#endif

// Entry-delay beeps (the countdown while waiting for a PIN). Which buzzer
// carries them is set here, 0 = that buzzer is left free: both buzzers together
// is the loudest; set one to 0 to leave that buzzer free for clicks. For
// passive buzzers the value is the pitch.
const uint16_t ENTRY_BEEP_HZ1 = 2700;   // buzzer 1 (GPIO23)
const uint16_t ENTRY_BEEP_HZ2 = 2700;   // buzzer 2 (GPIO22)

// While someone types during the countdown, the beeps stop at the first key
// and stay off until this long after the last key. Every key is then heard on
// its own, and the wrong-PIN buzz or disarmed sound plays in silence. If they
// stop typing, a burst follows at once. Only the sound pauses: the entry timer
// still runs out after ENTRY_TIME_LIMIT and the alarm still sounds.
const unsigned long TYPING_QUIET_MS = 1800;

// Shape of the countdown. It starts after SILENCE_BEFORE_BEEP (below). Each
// burst is COUNTDOWN_BEEPS_PER_BURST beeps of BEEP_ON_DURATION ms with
// COUNTDOWN_BURST_GAP_MS between them, then a pause that shrinks in a straight
// line from COUNTDOWN_PAUSE_START_MS to COUNTDOWN_PAUSE_END_MS as the entry
// window runs out. The classic accelerating beeps are one beep per burst with
// a pause of 1500 -> 60 ms. For a triple beep use 3 beeps per burst and a
// pause of 2500 -> 300; for a steady rhythm set START and END equal.
const int BEEP_ON_DURATION = 150;
const int COUNTDOWN_BEEPS_PER_BURST = 1;
const int COUNTDOWN_BURST_GAP_MS = 100;
const int COUNTDOWN_PAUSE_START_MS = 1500;
const int COUNTDOWN_PAUSE_END_MS = 60;

// The one-shot sounds, by id. `priority`: a new sound replaces one that is
// playing if its priority is the same or higher, and is dropped otherwise.
enum SoundId : uint8_t {
  SND_CLICK, SND_ARMED, SND_DISARMED, SND_WRONG_PIN, SND_REFUSED, SOUND_COUNT
};

struct SoundDef {
  const SoundStep* steps;
  uint8_t count;
  uint8_t priority;
};

#define SOUND_STEPS(a) (a), (sizeof(a) / sizeof((a)[0]))
const SoundDef SOUNDS[SOUND_COUNT] = {
  {SOUND_STEPS(CLICK_STEPS), 1},
  {SOUND_STEPS(ARMED_STEPS), 2},
  {SOUND_STEPS(DISARMED_STEPS), 2},
  {SOUND_STEPS(WRONG_PIN_STEPS), 2},
  {SOUND_STEPS(REFUSED_STEPS), 2},
};
const char* const SOUND_NAMES[SOUND_COUNT] = {"click", "armed", "disarmed", "wrong-pin",
                                              "refused"};

const uint32_t SOUND_TICK_MS = 2;       // how often the sound task updates the pins
const int SOUND_QUEUE_DEPTH = 8;
const uint32_t SOUND_STACK_BYTES = 4096;

// What loop() asks for. loop() never writes the buzzer pins itself.
volatile bool sirenWanted = false;      // true exactly while the state is ALARM
volatile bool entryBeepHeld = false;    // true while one entry-delay beep is on
QueueHandle_t soundQueue = nullptr;     // one-shot sound ids
bool soundTaskRunning = false;

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
// read by getPhysicalKey() below. Typing into the Serial Monitor can stand in
// for it in a bench-test build (-DENABLE_SERIAL_KEYPAD=1, see the top of the
// file); the shipped build does not accept keys over USB.

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

const unsigned long ENTRY_TIME_LIMIT = 30000;   // 30 seconds total to disarm before ALARM (reported to the backend, which mirrors it in the app)
const unsigned long SILENCE_BEFORE_BEEP = 2000; // 2 seconds of silence before warning beeps start
const unsigned long REMOTE_ARM_DELAY = 3000;    // exit delay for app-initiated arming (no door needed)

// --- Beep/Siren Variables ---
unsigned long lastBeepTime = 0;
bool isBuzzerCurrentlyOn = false;
int beepsInBurst = COUNTDOWN_BEEPS_PER_BURST;  // beeps of the current countdown burst so far
unsigned long lastKeyMs = 0;                   // when a key was last pressed (any state)

// --- Heartbeat ---
unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL = 10000;

enum ReportKind : uint8_t {
  REPORT_STATE_CHANGE,
  REPORT_ENTRY_DELAY,
  REPORT_DOOR_ALERT,
  REPORT_HEARTBEAT,
};

struct ReportMessage {
  ReportKind kind;
  bool armed;  // only meaningful for REPORT_STATE_CHANGE
};

// Small on purpose. If reports are backing up this far the backend is down,
// and the useful behaviour then is to drop them rather than to accumulate a
// burst that will all arrive at once when it returns. Heartbeats are resent
// every interval anyway, and a stale arm/disarm report is worse than none —
// the backend would apply it as though it had just happened.
const int REPORT_QUEUE_DEPTH = 8;

// 8 KB: HTTPClient plus the underlying WiFiClient need a few KB of stack for
// TLS-capable buffers even on plain HTTP, and a stack overflow here would
// look like a random reboot rather than a failed report.
const uint32_t REPORTER_STACK_BYTES = 8192;

QueueHandle_t reportQueue = nullptr;

// Boot sequence: configure pins, force the buzzers quiet so they don't
// chirp on power-up, read the door's initial state, then bring up WiFi
// and the remote-control WebServer.
// ---- WiFi watchdog ------------------------------------------------------
// The core reconnects by itself, but after a long outage the radio can stay wedged.
// Nudge it every 20 s while the link is down. `REBOOT_AFTER_MS` of silence reboots the
// board (0 = never); see the call site for why the door board only does that when disarmed.
unsigned long wifiDownSince = 0;
unsigned long wifiLastNudge = 0;

void wifiWatchdog(unsigned long rebootAfterMs) {
  if (WiFi.status() == WL_CONNECTED) {
    wifiDownSince = 0;
    return;
  }
  unsigned long now = millis();
  if (wifiDownSince == 0) {
    wifiDownSince = now;
    wifiLastNudge = now;
  }
  if (now - wifiLastNudge >= 20000) {
    wifiLastNudge = now;
    WiFi.disconnect();
    WiFi.reconnect();
  }
  if (rebootAfterMs > 0 && now - wifiDownSince >= rebootAfterMs) {
    Serial.println("WiFi down for too long - restarting.");
    Serial.flush();
    delay(200);
    ESP.restart();
  }
}

void setup() {
  Serial.begin(9600); // 9600 to ensure stable serial reading

  // Setup pins
  pinMode(DOOR_SENSOR_PIN, INPUT_PULLUP);

  // Keypad: rows idle HIGH (driven), columns read with internal pull-ups so
  // no external resistors are needed.
  for (int r = 0; r < 4; r++) {
    pinMode(ROW_PINS[r], OUTPUT);
    digitalWrite(ROW_PINS[r], HIGH);
  }
  for (int c = 0; c < 3; c++) {
    pinMode(COL_PINS[c], INPUT_PULLUP);
  }

  // Buzzers: set up quiet, then the sound task takes them over
  soundInit();

  loadKeypadPin();

  // Get initial door state
  lastDoorState = digitalRead(DOOR_SENSOR_PIN);
  isDoorOpen = (lastDoorState == HIGH); // HIGH means magnet is removed (open)
  delay(1000); // Boot settle pause. Longer than an INPUT_PULLUP actually
               // needs to stabilize (that's microseconds) — carried over
               // unexplained from the original draft; harmless, so left
               // as-is since this pass is documentation-only.

  // --- WiFi + remote-control server ---
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  // The SSID can be offered by several mesh nodes. Look at all channels and join the
  // strongest one instead of the first one found.
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected");
  Serial.print("My IP: ");
  Serial.println(WiFi.localIP());
  WiFi.setSleep(false);  // power save off: no 100+ ms waits for every packet

  // WebServer discards every request header it wasn't explicitly told to
  // keep, so this line is load-bearing: without it server.header("X-Device-Key")
  // always returns "" and authorized() would reject every legitimate command
  // from the backend.
  const char* collectedHeaders[] = {"X-Device-Key"};
  server.collectHeaders(collectedHeaders, 1);

  reportQueue = xQueueCreate(REPORT_QUEUE_DEPTH, sizeof(ReportMessage));
  if (reportQueue == nullptr) {
    Serial.println("Could not create the report queue — reports will block loop().");
  } else {
    // Core 0: the Arduino loop task runs on core 1, so the blocking HTTP work
    // sits on the other core alongside the WiFi stack rather than competing
    // with the keypad scan and handleClient() for the same one.
    xTaskCreatePinnedToCore(reporterTask, "hgReporter", REPORTER_STACK_BYTES,
                            nullptr, 1, nullptr, 0);
  }

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

// ---- Sound player --------------------------------------------------------
//
// loop() never writes the buzzer pins. It only asks: soundPlay() drops a
// one-shot sound id on a queue, and two flags say what is held on (the siren
// while the state is ALARM, an entry-delay beep while one is sounding). A
// small task, at a higher priority than loop(), turns those into pin writes
// every SOUND_TICK_MS. That means
//  - no sound code ever waits: there is no delay() anywhere in it, so a
//    keypress, a PIN or a /disarm is never held up by a sound;
//  - clearing the flag silences the siren on the next tick (2 ms); and
//  - if loop() stalls (server.handleClient() on a slow HTTP client), the siren
//    keeps stepping instead of freezing on one tone.
// The task owns everything below up to soundTask(); only the two flags above
// and the queue are shared with loop().

const SoundDef* voiceDef = nullptr;   // the one-shot currently playing, if any
uint8_t voiceIdx = 0;
uint32_t voiceStart = 0;
bool sirenRunning = false;
uint8_t sirenIdx = 0;
uint32_t sirenStart = 0;

void writeBuzzer(int pin, uint16_t hz) {
#if BUZZERS_ACTIVE
  digitalWrite(pin, hz ? HIGH : LOW);
#else
  ledcWriteTone(pin, hz);  // 0 = silent, else ~50% duty at that frequency
#endif
}

// Touches a pin only when its value changes.
void driveBuzzers(uint16_t hz1, uint16_t hz2) {
  static uint16_t last1 = 0, last2 = 0;
  if (hz1 != last1) { writeBuzzer(BUZZER_1_PIN, hz1); last1 = hz1; }
  if (hz2 != last2) { writeBuzzer(BUZZER_2_PIN, hz2); last2 = hz2; }
}

// Decides what the buzzers do right now: siren > entry beep > one-shot, where
// the entry beep only takes over the buzzer(s) it names.
void soundTick(uint32_t now) {
  // 1. New one-shot requests. They are dropped while the siren runs, and a
  //    lower-priority one never interrupts a higher-priority one.
  uint8_t id;
  while (soundQueue != nullptr && xQueueReceive(soundQueue, &id, 0) == pdTRUE) {
    if (sirenWanted || id >= SOUND_COUNT) continue;
    const SoundDef* def = &SOUNDS[id];
    if (voiceDef == nullptr || def->priority >= voiceDef->priority) {
      voiceDef = def;
      voiceIdx = 0;
      voiceStart = now;
    }
  }

  // 2. Step the one-shot along. It keeps its own time even while an entry beep
  //    covers it, so it ends on schedule instead of resuming late.
  while (voiceDef != nullptr && now - voiceStart >= voiceDef->steps[voiceIdx].ms) {
    voiceStart += voiceDef->steps[voiceIdx].ms;
    if (++voiceIdx >= voiceDef->count) voiceDef = nullptr;
  }

  // 3. Who gets the buzzers.
  uint16_t hz1 = 0, hz2 = 0;
  if (sirenWanted) {
    voiceDef = nullptr;  // whatever was half-played is moot now
    const uint8_t sirenCount = sizeof(SIREN_STEPS) / sizeof(SIREN_STEPS[0]);
    if (!sirenRunning) {
      sirenRunning = true;
      sirenIdx = 0;
      sirenStart = now;
    }
    while (now - sirenStart >= SIREN_STEPS[sirenIdx].ms) {
      sirenStart += SIREN_STEPS[sirenIdx].ms;
      sirenIdx = (sirenIdx + 1) % sirenCount;
    }
    hz1 = SIREN_STEPS[sirenIdx].hz1;
    hz2 = SIREN_STEPS[sirenIdx].hz2;
  } else {
    sirenRunning = false;
    if (voiceDef != nullptr) {
      hz1 = voiceDef->steps[voiceIdx].hz1;
      hz2 = voiceDef->steps[voiceIdx].hz2;
    }
    if (entryBeepHeld) {
      if (ENTRY_BEEP_HZ1) hz1 = ENTRY_BEEP_HZ1;
      if (ENTRY_BEEP_HZ2) hz2 = ENTRY_BEEP_HZ2;
    }
  }
  driveBuzzers(hz1, hz2);
}

void soundTask(void* param) {
  for (;;) {
    soundTick(millis());
    vTaskDelay(pdMS_TO_TICKS(SOUND_TICK_MS));
  }
}

// Sets the buzzers up quiet and starts the sound task. Called once from setup().
void soundInit() {
#if BUZZERS_ACTIVE
  pinMode(BUZZER_1_PIN, OUTPUT);
  pinMode(BUZZER_2_PIN, OUTPUT);
  digitalWrite(BUZZER_1_PIN, LOW);
  digitalWrite(BUZZER_2_PIN, LOW);
#else
  // Two different attach frequencies on purpose: ledcAttach() puts pins with
  // identical (frequency, resolution) on ONE LEDC timer, and a timer has a
  // single frequency, so the buzzers could then never sound different pitches
  // at once. Different arguments give each pin its own timer.
  ledcAttach(BUZZER_1_PIN, 2000, 10);
  ledcAttach(BUZZER_2_PIN, 2001, 10);
  ledcWriteTone(BUZZER_1_PIN, 0);
  ledcWriteTone(BUZZER_2_PIN, 0);
#endif

  soundQueue = xQueueCreate(SOUND_QUEUE_DEPTH, sizeof(uint8_t));
  // Core 1 beside loop(), one priority above it: its work is a few
  // microseconds every 2 ms, and it must be able to cut in on loop() at once.
  soundTaskRunning = xTaskCreatePinnedToCore(soundTask, "hgSound", SOUND_STACK_BYTES,
                                             nullptr, 2, nullptr, 1) == pdPASS;
  if (!soundTaskRunning) {
    // loop() then runs soundTick() itself, so the siren still sounds; it just
    // shares loop()'s timing.
    Serial.println("Could not start the sound task - sound runs from loop() instead.");
  }
}

// Asks for a one-shot sound. Never blocks; a full queue drops the request.
// Clicks and status sounds are not played while the siren runs.
void soundPlay(SoundId id) {
  if (soundQueue == nullptr || sirenWanted) return;
  if (id != SND_CLICK) {
    Serial.print("[SOUND] ");
    Serial.println(SOUND_NAMES[id]);
  }
  uint8_t v = id;
  xQueueSend(soundQueue, &v, 0);
}

// One entry-delay beep on or off (both buzzers). The accelerating schedule
// that calls this is unchanged and still lives in the ENTRY_DELAY case.
void entryBeep(bool on) {
  entryBeepHeld = on;
  isBuzzerCurrentlyOn = on;
}

// Every change of `currentState` goes through here, so the sounds that belong
// to a transition play exactly once, whichever path caused it: the keypad,
// the app's /arm and /disarm, the backend's /trigger-alarm, or a timeout.
void setState(State next) {
  State prev = currentState;
  currentState = next;
  if (next == prev) return;  // e.g. /disarm while already disarmed: no change, no sound

  // The siren belongs to ALARM and nothing else. This comes before the status
  // sounds below on purpose: soundPlay() drops everything while it is set.
  sirenWanted = (next == ALARM);
  if (next != ENTRY_DELAY) entryBeep(false);  // leaving the entry delay ends its beeps

  if (next == ARMED) {
    soundPlay(SND_ARMED);
  } else if (next == DISARMED) {
    soundPlay(SND_DISARMED);
  }
}

#if ENABLE_SERIAL_KEYPAD
// Reads one character typed into the Serial Monitor, standing in for the
// physical keypad. Filters out '\n'/'\r' so pressing
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
#endif

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

// The physical keypad, plus Serial Monitor input only in a bench-test build
// (ENABLE_SERIAL_KEYPAD, off by default — see its definition at the top).
char getKey() {
  char physical = getPhysicalKey();
  if (physical != '\0') return physical;
#if ENABLE_SERIAL_KEYPAD
  return getSimulatedKey();
#else
  return '\0';
#endif
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
void sendStateChangeNow(bool armed) {
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
void sendEntryDelayNow() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  beginRequest(http, backendUrl("/esp32/entry-delay"));
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  // The seconds LEFT, not the full window: when WiFi is slow this report arrives late, and the
  // app counted a full 30 s from then while the board's own 30 s was already running.
  unsigned long used = millis() - delayStartTime;
  unsigned long left = used < ENTRY_TIME_LIMIT ? ENTRY_TIME_LIMIT - used : 0;
  http.POST("{\"seconds\":" + String((left + 999) / 1000) + "}");
  http.end();
}

// Reports the door-breach-timeout alarm, same pattern as the other two
// sensor boards use for their own sensors.
void sendDoorAlertNow() {
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
void sendHeartbeatNow() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  beginRequest(http, backendUrl("/esp32/heartbeat?board=") + BOARD_NAME);
  http.addHeader("X-Device-Key", DEVICE_KEY);
  http.POST("");
  http.end();
}

// ---- Outbound reporting runs on its own task ---------------------------
//
// Everything above sends synchronously and can block for up to
// HTTP_TIMEOUT_MS on connect plus the same again waiting for a response.
// That was survivable on the three push-only sensor boards, but not here,
// because this is the board that also *answers*: `loop()` calls
// `server.handleClient()` exactly once per pass, so for the whole duration
// of a report this board replies to nothing — not /arm, not /disarm, and
// not /trigger-alarm, which the backend calls (with a 3-second budget) when
// a different board reports a break-in. A siren that never sounds during an
// actual intrusion was the cost.
//
// Capping the timeouts shrank that window; moving the sends off `loop()`
// removes it. `loop()` now only drops a small struct on a queue and returns
// immediately, and this task does the waiting.
//
// The task deliberately shares no mutable state with `loop()`. It reads the
// queue and nothing else — not `currentState`, not `enteredPin` — so there
// is nothing here to guard with a mutex and no way for a half-updated state
// machine to be observed mid-transition. Everything it needs travels in the
// message.


void reporterTask(void* param) {
  ReportMessage message;
  for (;;) {
    // Blocks indefinitely; this task consumes no CPU while idle.
    if (xQueueReceive(reportQueue, &message, portMAX_DELAY) != pdTRUE) continue;
    switch (message.kind) {
      case REPORT_STATE_CHANGE: sendStateChangeNow(message.armed); break;
      case REPORT_ENTRY_DELAY:  sendEntryDelayNow();               break;
      case REPORT_DOOR_ALERT:   sendDoorAlertNow();                break;
      case REPORT_HEARTBEAT:    sendHeartbeatNow();                break;
    }
  }
}

// Hands a report to the task. Never blocks: a zero tick timeout means a full
// queue drops the message rather than stalling `loop()`, which is the entire
// reason this indirection exists.
//
// Falls back to sending inline if the queue doesn't exist, so a failure to
// create it degrades to the old blocking behaviour instead of silently
// reporting nothing at all.
void enqueueReport(ReportKind kind, bool armed = false) {
  ReportMessage message = {kind, armed};
  // No queue means creation failed at boot. Sending inline blocks loop()
  // again — the very thing this indirection exists to avoid — but going
  // silent would be worse, so it degrades rather than drops.
  if (reportQueue == nullptr) {
    switch (kind) {
      case REPORT_STATE_CHANGE: sendStateChangeNow(armed); break;
      case REPORT_ENTRY_DELAY:  sendEntryDelayNow();       break;
      case REPORT_DOOR_ALERT:   sendDoorAlertNow();        break;
      case REPORT_HEARTBEAT:    sendHeartbeatNow();        break;
    }
    return;
  }
  if (xQueueSend(reportQueue, &message, 0) != pdTRUE) {
    Serial.println("Report queue full — dropping a report (is the backend up?)");
  }
}

void reportStateChange(bool armed) { enqueueReport(REPORT_STATE_CHANGE, armed); }
void reportEntryDelay()            { enqueueReport(REPORT_ENTRY_DELAY); }
void reportDoorAlert()             { enqueueReport(REPORT_DOOR_ALERT); }
void sendHeartbeat()               { enqueueReport(REPORT_HEARTBEAT); }

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

  // With the door open, arming would finish straight into ENTRY_DELAY (ARMED
  // sees the open door), i.e. a countdown to the alarm the instant it is armed.
  // The keypad refuses the same way ("Door must be closed first"). Answer 409
  // so the backend does not arm its own state either and the app can say why.
  if (currentState == DISARMED && isDoorOpen) {
    Serial.println("[REMOTE] Arm refused: the door is open");
    soundPlay(SND_REFUSED);
    server.send(409, "text/plain", "Door open");
    return;
  }

  // Remote arming doesn't require physically walking out the door —
  // skip straight to a short countdown instead of PENDING_ARM/EXITING.
  if (currentState == DISARMED) {
    if (isHomeMode) {
      // Home: somebody stays inside, nobody walks out, so no exit countdown.
      setState(ARMED);
      reportStateChange(true);
    } else {
      setState(REMOTE_ARMING);
      delayStartTime = millis();
    }
  }
  // Always responds 200 OK even when the request above was a no-op
  // (e.g. system was already ARMED) — caller currently can't tell
  // "armed" apart from "ignored, already armed" from the response alone.
  server.send(200, "text/plain", "OK");
}

// WebServer handler for POST /disarm. Unlike handleRemoteArm, this has
// no state guard: disarming has to work from any state (including
// mid-ALARM), so it unconditionally resets to DISARMED, clears any
// partially-typed PIN, and (via setState) cuts the siren.
void handleRemoteDisarm() {
  if (!authorized()) return;
  Serial.println("\n[REMOTE] Disarm requested via app");
  setState(DISARMED);
  enteredPin = "";
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
  setState(ALARM);
  enteredPin = "";
  server.send(200, "text/plain", "OK");
}

// Main loop: services the remote-control WebServer, polls the physical
// door sensor and the simulated keypad, advances the arm/disarm state
// machine below, and sends a periodic heartbeat.
void loop() {
  // Only reboot over a long outage while DISARMED: a reboot forgets the armed state.
  wifiWatchdog(currentState == DISARMED ? 300000UL : 0UL);

  // Only if the sound task could not start (see soundInit()).
  if (!soundTaskRunning) soundTick(millis());

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

  // Every key press clicks. (The Serial fallback can type any character; only
  // the twelve real keys count.) soundPlay() keeps it quiet during the siren,
  // and the entry-delay logic below pauses its beeps while keys are being
  // pressed (lastKeyMs), so the click is never played over them.
  if ((input >= '0' && input <= '9') || input == '*' || input == '#') {
    lastKeyMs = millis();
    soundPlay(SND_CLICK);
  }

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
        setState(PENDING_ARM);
        Serial.println("\nSystem PENDING. Please open the door to exit.");
      } else if (input == '*' && isDoorOpen) {
        Serial.println("\nCannot arm: Door must be closed first.");
        soundPlay(SND_REFUSED);
      }
      break;

    // -----------------------------------------
    case PENDING_ARM:
      // Waiting for you to open the door to leave
      if (isDoorOpen) {
        setState(EXITING);
        Serial.println("\nDoor opened. Please exit and close the door to arm.");
      }
      if (input == '*') { // Cancel arming
        setState(DISARMED);
        Serial.println("\nArming cancelled. State: DISARMED");
      }
      break;

    // -----------------------------------------
    case EXITING:
      // Waiting for the physical act of closing the door behind you
      if (!isDoorOpen) {
        setState(ARMED);
        Serial.println("\nDoor closed. System is now ARMED.");
        reportStateChange(true);
      }
      if (input == '*') { // Cancel arming
        setState(DISARMED);
        Serial.println("\nArming cancelled. State: DISARMED");
      }
      break;

    // -----------------------------------------
    case REMOTE_ARMING:
      // App-initiated arm — no door cycle needed, just a short countdown
      // (mirrors the backend's own ARMING_EXIT_DELAY, so the app's
      // "Arming..." UI and the physical hardware agree on timing).
      if (millis() - delayStartTime >= REMOTE_ARM_DELAY) {
        setState(ARMED);
        Serial.println("\nRemote arm countdown complete. System is now ARMED.");
        reportStateChange(true);
      }
      if (input == '*') { // Cancel arming
        setState(DISARMED);
        Serial.println("\nArming cancelled. State: DISARMED");
      }
      break;

    // -----------------------------------------
    case ARMED:
      // If the door opens, trigger the countdown
      if (isDoorOpen) {
        setState(ENTRY_DELAY);
        delayStartTime = millis();
        lastBeepTime = millis();
        isBuzzerCurrentlyOn = false;
        beepsInBurst = COUNTDOWN_BEEPS_PER_BURST;  // so the first burst follows a full pause
        lastKeyMs = 0;  // keys typed before the door opened must not delay the beeps
        // Start the disarm window with an empty buffer. Digits accumulate in
        // every state, and none of DISARMED/PENDING_ARM/EXITING/REMOTE_ARMING/
        // ARMED handles '#', so anything typed or brushed against the keypad
        // earlier is still sitting in enteredPin. Without this reset, one
        // stray digit turns a correctly-typed PIN into "51234" != "1234" and
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
          setState(ALARM);
          enteredPin = "";
          Serial.println("\n*** ALARM TRIGGERED! SIRENS SOUNDING! ***");
          reportDoorAlert();
        }
        else {
          // 2. Handle keypad to disarm
          if (input == '#') {
            Serial.println();
            if (enteredPin == secretPin) {
              setState(DISARMED);
              enteredPin = "";
              Serial.println("Correct PIN. System DISARMED.");
              reportStateChange(false);
              // Without this break, execution falls through to the
              // accelerating-beep logic below (still inside this case,
              // same loop() pass) — by then enough time has elapsed since
              // lastBeepTime that it immediately turns the beep back on,
              // undoing the silence setState() just applied. Disarming is
              // done; nothing after this point should run this iteration.
              break;
            } else {
              enteredPin = "";
              Serial.println("Wrong PIN. Keep trying!");
              soundPlay(SND_WRONG_PIN);
            }
          }

          // 3. Accelerating Beep Logic WITH SILENT DELAY
          if (elapsed < SILENCE_BEFORE_BEEP) {
            // Do nothing yet, keep it silent
            entryBeep(false);
          }
          else if (millis() - lastKeyMs < TYPING_QUIET_MS) {
            // Someone is typing: no countdown beeps (see TYPING_QUIET_MS). The
            // burst counter is rewound so a fresh burst follows once they stop.
            entryBeep(false);
            beepsInBurst = COUNTDOWN_BEEPS_PER_BURST;
          }
          else {
            // The silent time has passed, start the warning beeps!
            // Map the remaining time (from when silence ends to the time limit)
            int currentOffDuration = map(elapsed, SILENCE_BEFORE_BEEP, ENTRY_TIME_LIMIT,
                                         COUNTDOWN_PAUSE_START_MS, COUNTDOWN_PAUSE_END_MS);
            // Between the beeps of one burst the gap is short; after a
            // burst's last beep it is the long, shrinking pause above. (With
            // one beep per burst that is always the long pause: the original
            // accelerating beeps.)
            if (beepsInBurst > 0 && beepsInBurst < COUNTDOWN_BEEPS_PER_BURST) {
              currentOffDuration = COUNTDOWN_BURST_GAP_MS;
            }
            unsigned long timeSinceLastChange = millis() - lastBeepTime;

            if (isBuzzerCurrentlyOn) {
              if (timeSinceLastChange >= BEEP_ON_DURATION) {
                entryBeep(false);
                lastBeepTime = millis();
              }
            } else {
              if (timeSinceLastChange >= currentOffDuration) {
                if (beepsInBurst >= COUNTDOWN_BEEPS_PER_BURST) beepsInBurst = 0;  // new burst
                beepsInBurst++;
                entryBeep(true);
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
        // The siren itself plays from the sound task for as long as the state
        // is ALARM (see setState()); nothing to do for it here.

        // The only way to stop the alarm is entering the correct PIN
        if (input == '#') {
          Serial.println();
          if (enteredPin == secretPin) {
            setState(DISARMED);
            enteredPin = "";
            Serial.println("Correct PIN. ALARM SILENCED. System DISARMED.");
            reportStateChange(false);
          } else {
            enteredPin = "";
            Serial.println("Wrong PIN! ALARM STILL SOUNDING!");
            soundPlay(SND_WRONG_PIN);  // muted while the siren runs
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

