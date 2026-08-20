/// The states the alarm can be in. Mirrors `AlarmState` in
/// python/backend/models.py.
///
/// `transitional` is the exit delay: the system is arming and you're walking
/// out. `entryDelay` is its mirror image — someone opened the door on an
/// armed system and has a limited time to disarm before the alarm sounds.
///
/// The door controller has always run that entry window (its own
/// `ENTRY_DELAY` state), but it used to report only the outcome, so the app
/// jumped straight from ARMED to TRIGGERED and never showed the countdown the
/// person walking in is actually racing.
enum AlarmState { disarmed, transitional, armed, entryDelay, triggered }

/// Away arms every sensor. Home arms the perimeter (ultrasonic + door) but
/// bypasses the vibration sensor, so people can move around inside.
enum ArmMode { away, home }

enum EventCategory { auth, armDisarm, alert, faceId, info }

/// Coarser severity grouping for the event log's noise filter — several
/// categories can share a severity (e.g. auth/faceId/info are all routine).
enum EventSeverity { critical, warning, info }

class SecurityEvent {
  final DateTime timestamp;
  final String message;
  final EventCategory category;

  const SecurityEvent({
    required this.timestamp,
    required this.message,
    this.category = EventCategory.info,
  });

  /// Maps this event's [category] down to the coarser [EventSeverity]
  /// bucket the event log's noise filter groups by.
  EventSeverity get severity {
    switch (category) {
      case EventCategory.alert:
        return EventSeverity.critical;
      case EventCategory.armDisarm:
        return EventSeverity.warning;
      case EventCategory.auth:
      case EventCategory.faceId:
      case EventCategory.info:
        return EventSeverity.info;
    }
  }
}

/// Matches the actual sensor hardware: ultrasonic distance sensor (HC-SR04,
/// on its own ESP32 board), magnetic door sensor (MC38, on the door/keypad
/// board), vibration sensor (SW420, on its own ESP32 board). Three separate
/// boards, not one — see esp32/.
enum SensorType { ultrasonic, door, vibration }

class SensorReading {
  final SensorType type;
  final String label;
  final bool ok; // true = normal, false = triggered/breach
  final bool bypassed; // armed but not being monitored (Home mode)

  const SensorReading({
    required this.type,
    required this.label,
    required this.ok,
    this.bypassed = false,
  });
}

class ApprovedFace {
  final String id;
  final String name;

  const ApprovedFace({required this.id, required this.name});
}

/// Liveness of one of the three ESP32 boards. Mirrors `BoardStatus` in
/// python/backend/models.py.
///
/// The dashboard used to show a single ONLINE/OFFLINE badge for "the ESP32",
/// which couldn't express one board being down while the others were fine —
/// and which, because the backend's flag latched on permanently, kept
/// claiming ONLINE for a board that had lost power hours earlier. The backend
/// now derives each board's status from its last heartbeat; this carries that
/// per-board detail to the UI.
///
/// [lastSeen] is null for a board that hasn't reported at all since the
/// backend started — "never showed up" reads differently from "was here, went
/// quiet".
class BoardStatus {
  final String name;
  final bool online;
  final DateTime? lastSeen;

  const BoardStatus({required this.name, required this.online, this.lastSeen});

  /// Human-readable board name for the dashboard, since the wire format uses
  /// the backend's identifiers (`door_controller`, not "Door controller").
  String get label => switch (name) {
        'ultrasonic' => 'Ultrasonic board',
        'vibration' => 'Vibration board',
        'door_controller' => 'Door controller',
        _ => name,
      };
}

/// The backend is the sole lockout authority — client-side-only lockout
/// is trivially bypassed by just restarting the app, so it can't be the
/// real security boundary. AuthService just relays whatever comes back
/// here rather than counting attempts itself.
class LoginResult {
  final bool ok;
  final bool lockedOut;
  final int lockoutRemainingSeconds;
  final int attemptsRemaining;

  const LoginResult({
    required this.ok,
    this.lockedOut = false,
    this.lockoutRemainingSeconds = 0,
    this.attemptsRemaining = 0,
  });
}

/// Push-notification target: an ntfy server and topic.
///
/// On the public ntfy.sh the topic name is the only thing keeping strangers
/// from reading this house's alarm notifications, so it should be long and
/// random, or the server should be self-hosted. The settings screen says so
/// next to the field.
class NtfyConfig {
  final String server;
  final String topic;

  const NtfyConfig({required this.server, required this.topic});

  /// Whether push notifications are actually on. Pushing is opt-in: no topic
  /// means the backend never sends anything.
  bool get enabled => topic.isNotEmpty;
}

/// Everything the app needs from the backend. HttpBackendService talks to
/// the real FastAPI backend (python/); MockBackendService simulates one
/// in-memory for UI work without needing it running.
abstract class BackendService {
  /// Attempts to authenticate with [password]. The backend is the sole
  /// lockout authority (see [LoginResult]), so the returned result carries
  /// not just success/failure but also lockout state and attempts
  /// remaining — the caller (AuthService) doesn't compute any of that
  /// itself, only relays it.
  Future<LoginResult> login(String password);

  /// Ends the current session. Implementations backed by a real server must
  /// invalidate the session token there, not just locally — otherwise
  /// "logging out" leaves a credential that stays valid for hours and can
  /// still arm or disarm the system.
  Future<void> logout();

  /// Changes the login password from [oldPassword] to [newPassword].
  /// Returns whether the change succeeded (implementations should reject
  /// when [oldPassword] doesn't match the current password).
  Future<bool> changePassword(String oldPassword, String newPassword);

  /// Arms the system in the given [mode] (see [ArmMode] for what each mode
  /// bypasses). Implementations are expected to pass through a
  /// transitional/exit-delay state before actually reaching `armed`.
  Future<void> arm(ArmMode mode);

  /// Disarms the system, whether it's currently armed, transitional
  /// (mid exit-delay), or triggered.
  Future<void> disarm();

  /// Generalized trigger: source identifies which sensor caused it (null
  /// for non-sensor causes, e.g. a failed-login lockout). Always safe to
  /// call — no-ops if the system isn't armed.
  Future<void> triggerAlert({SensorType? source, required String reason});

  /// Emits whenever the alarm's top-level state changes (disarmed →
  /// transitional → armed → triggered, or back to disarmed).
  Stream<AlarmState> get alarmStateStream;

  /// Emits the full event log (newest first) each time a new event is
  /// recorded, not just the new entry — simplest way for listeners to
  /// stay in sync without tracking a diff themselves.
  Stream<List<SecurityEvent>> get eventLogStream;

  /// Emits the full sensor list whenever any sensor's reading or bypass
  /// state changes.
  Stream<List<SensorReading>> get sensorStream;

  /// Emits whether *any* ESP32 board is currently reachable — the single
  /// badge on the dashboard. See [boardsStream] for which specific boards.
  Stream<bool> get connectivityStream;

  /// Emits the status of each of the three ESP32 boards whenever any of them
  /// changes (see [BoardStatus]).
  Stream<List<BoardStatus>> get boardsStream;

  /// Emits a human-readable message whenever a request fails for an
  /// infrastructural reason — the backend is unreachable, or it rejected the
  /// session as expired.
  ///
  /// Exists because every REST call in [HttpBackendService] swallows its
  /// errors and returns an empty response, which made "the backend is down"
  /// indistinguishable from "the backend said no". On an alarm system that
  /// matters concretely: a disarm request that never left the phone looked
  /// exactly like one the backend had processed. Listeners surface these to
  /// the user; nothing depends on them for control flow.
  Stream<String> get errorStream;

  // Approved faces (biometric layer)
  /// Emits the full list of approved faces whenever it changes.
  Stream<List<ApprovedFace>> get facesStream;

  /// Registers a new approved face under [name] for the face-ID flow.
  Future<void> addFace(String name);

  /// Removes the approved face with the given [id].
  Future<void> removeFace(String id);

  // Camera / face-ID module
  /// Emits whether the camera module is currently reachable — tracked
  /// separately from [connectivityStream] since the camera and the
  /// sensor ESP32 boards are independent pieces of hardware that can go
  /// offline independently of each other.
  Stream<bool> get cameraOnlineStream;

  /// The backend's `/camera/latest` URL — the most recent frame a camera
  /// worker uploaded, served as a JPEG — or null when there's no way to
  /// authenticate a request right now (not logged in yet) or no real
  /// backend to ask at all ([MockBackendService]). Callers should append a
  /// cache-busting query parameter before each fetch: the URL is otherwise
  /// identical between refreshes, and Image.network caches by URL.
  String? get cameraFrameUrl;

  /// Headers a request to [cameraFrameUrl] needs to authenticate.
  Map<String, String> get cameraFrameHeaders;

  /// The backend's `/camera/stream` URL — a continuous MJPEG multipart
  /// stream proxied from the ESP32-CAM's own /stream endpoint, replacing
  /// the poll-a-JPEG-repeatedly approach [cameraFrameUrl] used. Same
  /// null-when-unavailable contract as [cameraFrameUrl]; same
  /// [cameraFrameHeaders] authenticate a request to it too.
  String? get cameraStreamUrl;

  /// Emits whether face-ID recognition is currently enabled.
  Stream<bool> get faceIdEnabledStream;

  /// Turns face-ID recognition on/off.
  Future<void> setFaceIdEnabled(bool enabled);

  /// Emits whether a recognized face is allowed to disarm the system.
  ///
  /// Distinct from [faceIdEnabledStream]: that's whether recognition runs at
  /// all, this is whether a match may turn the alarm off. Off by default —
  /// the recognizer has no liveness detection, so a photo may pass, which
  /// makes this a convenience feature rather than a security one.
  Stream<bool> get faceDisarmEnabledStream;

  /// Turns face-disarm on/off.
  Future<void> setFaceDisarmEnabled(bool enabled);

  /// Emits the seconds left to disarm during an [AlarmState.entryDelay],
  /// and 0 at all other times.
  Stream<int> get entryDelayStream;

  /// Demo-only: simulates the camera recognizing one of the approved
  /// faces, so the flow can be seen without a real camera/model.
  Future<void> simulateFaceRecognition();

  // Settings — loaded once by SettingsScreen, no need to stream these
  /// Whether night mode (auto-arm overnight) is currently enabled.
  Future<bool> getNightMode();

  /// Enables/disables night mode.
  Future<void> setNightMode(bool enabled);

  /// The configured emergency contact number (empty string if unset).
  Future<String> getEmergencyNumber();

  /// Sets the emergency contact [number].
  Future<void> setEmergencyNumber(String number);

  /// How long, in seconds, the system waits after a triggered alert
  /// before auto-dialing the emergency number.
  Future<int> getPoliceCallDelaySeconds();

  /// Sets the police-call delay, in [seconds].
  Future<void> setPoliceCallDelaySeconds(int seconds);

  /// Where the backend pushes alerts so they arrive with this app closed —
  /// the ntfy server URL and topic name (see `python/backend/notifier.py`).
  ///
  /// This exists because the app's own notifications only fire while its
  /// process is alive: swipe it away and a break-in produces nothing. An
  /// empty topic means pushing is off.
  Future<NtfyConfig> getNtfyConfig();

  /// Sets the push target. An empty [topic] disables pushing.
  Future<void> setNtfyConfig(String server, String topic);

  /// Sends a test push so the user can confirm the topic works before
  /// relying on it. Returns false if no topic is configured; delivery
  /// itself isn't confirmed.
  Future<bool> sendTestNotification();

  /// Releases any resources (stream controllers, sockets, timers) held by
  /// the implementation. Must be called when the service is no longer
  /// needed to avoid leaking open StreamControllers/sockets.
  void dispose();
}
