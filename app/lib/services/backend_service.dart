/// The states the ESP32's central state machine can report. `transitional`
/// is app-side only (exit delay while arming) — the real hardware only
/// needs to report disarmed/armed/triggered.
enum AlarmState { disarmed, transitional, armed, triggered }

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

  /// Emits whether the ESP32 hardware is currently reachable.
  Stream<bool> get connectivityStream;

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

  /// Emits whether face-ID recognition is currently enabled.
  Stream<bool> get faceIdEnabledStream;

  /// Turns face-ID recognition on/off.
  Future<void> setFaceIdEnabled(bool enabled);

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

  /// Releases any resources (stream controllers, sockets, timers) held by
  /// the implementation. Must be called when the service is no longer
  /// needed to avoid leaking open StreamControllers/sockets.
  void dispose();
}
