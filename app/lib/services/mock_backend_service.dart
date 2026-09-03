import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'backend_service.dart';

/// Simulates the ESP32 + FastAPI backend so the UI can be built and tested
/// before those exist. Password and timings are placeholders for demo use.
///
/// It is also the only backend a widget test can have (there is no server
/// during `flutter test`), which makes anything it *can't* do a piece of the
/// app no test can reach. It had quietly drifted into exactly that: the error
/// stream never emitted, so the failure→SnackBar path was untested; lockout
/// was unreachable, so [LoginScreen]'s entire locked branch was dead code as
/// far as the suite was concerned; and [AlarmState.entryDelay] — the most
/// time-critical screen in the app — was never produced. The
/// `simulate*`/[emitError] seams below exist to close that gap, and are
/// marked [visibleForTesting] rather than left as general API because
/// nothing in the running app should be forcing these states.
class MockBackendService implements BackendService {
  String _password = 'homeguard';

  final _alarmStateController = StreamController<AlarmState>.broadcast();
  final _eventLogController = StreamController<List<SecurityEvent>>.broadcast();
  final _sensorController = StreamController<List<SensorReading>>.broadcast();
  final _connectivityController = StreamController<bool>.broadcast();
  final _linkController = StreamController<bool>.broadcast();
  final _boardsController = StreamController<List<BoardStatus>>.broadcast();
  final _facesController = StreamController<List<ApprovedFace>>.broadcast();
  final _cameraOnlineController = StreamController<bool>.broadcast();
  final _faceIdEnabledController = StreamController<bool>.broadcast();
  final _faceDisarmEnabledController = StreamController<bool>.broadcast();
  final _entryDelayController = StreamController<int>.broadcast();
  final _errorController = StreamController<String>.broadcast();

  /// All three boards reported healthy, since there's no hardware to model.
  static const _mockBoards = [
    BoardStatus(name: 'ultrasonic', online: true),
    BoardStatus(name: 'vibration', online: true),
    BoardStatus(name: 'door_controller', online: true),
  ];

  AlarmState _state = AlarmState.disarmed;
  final List<SecurityEvent> _events = [];
  List<SensorReading> _sensors = const [
    SensorReading(type: SensorType.ultrasonic, label: 'Ultrasonic sensor', ok: true),
    SensorReading(type: SensorType.door, label: 'Door sensor', ok: true),
    SensorReading(type: SensorType.vibration, label: 'Vibration sensor', ok: true),
  ];
  Timer? _armingTimer;

  final List<ApprovedFace> _faces = [
    const ApprovedFace(id: '1', name: 'Guy Bar'),
    const ApprovedFace(id: '2', name: 'Yair Bonfil'),
  ];
  bool _faceIdEnabled = true;
  // Matches the backend default: face-disarm is opt-in.
  bool _faceDisarmEnabled = false;
  int _nextFaceId = 3;

  bool _nightMode = false;
  int _nightModeArmHour = 23;
  int _nightModeDisarmHour = 7;
  String _emergencyNumber = '';
  int _policeCallDelaySeconds = 60;
  String _ntfyServer = 'https://ntfy.sh';
  String _ntfyTopic = '';

  /// Mirrors the real backend's lockout rule (python/backend/state.py):
  /// three wrong passwords, then a cooldown during which even the right one
  /// is refused — itself a copy of how the physical keypad behaves.
  ///
  /// Simulated here, not enforced here-as-authority: the backend remains the
  /// only lockout authority in production, and [AuthService] still just
  /// relays whatever a [LoginResult] says. This exists so a widget test can
  /// actually reach the locked-out screen, which is otherwise unreachable
  /// without a live server.
  static const _maxAttempts = 3;
  static const _lockoutDuration = Duration(seconds: 30);
  int _failedAttempts = 0;
  DateTime? _lockedOutUntil;

  /// Sets up the in-memory state and, on the next microtask, publishes the
  /// initial values on the streams that need a first value (connectivity,
  /// camera, face-ID enabled, faces). Deferred to a microtask rather than
  /// emitted synchronously here because listeners (e.g. AlarmService)
  /// subscribe right after construction — emitting before any subscriber
  /// attaches would mean the first value is lost on a broadcast stream.
  MockBackendService() {
    // No real connectivity to model yet — starts online. A real
    // implementation would report this from the ESP32's WiFi status, and
    // the camera's own connection status separately.
    scheduleMicrotask(() {
      _connectivityController.add(true);
      // Emitted once and never retracted: there is no socket here to drop,
      // so from the UI's point of view the link is permanently up. Without
      // this the dashboard would render its (correct, but alarming)
      // no-live-connection treatment throughout every widget test.
      _linkController.add(true);
      _boardsController.add(_mockBoards);
      _cameraOnlineController.add(true);
      _faceIdEnabledController.add(_faceIdEnabled);
      _faceDisarmEnabledController.add(_faceDisarmEnabled);
      _entryDelayController.add(0);
      _facesController.add(List.unmodifiable(_faces));
    });
  }

  @override
  Stream<AlarmState> get alarmStateStream => _alarmStateController.stream;

  @override
  Stream<List<SecurityEvent>> get eventLogStream => _eventLogController.stream;

  @override
  Stream<List<SensorReading>> get sensorStream => _sensorController.stream;

  @override
  Stream<bool> get connectivityStream => _connectivityController.stream;

  @override
  Stream<bool> get backendLinkStream => _linkController.stream;

  @override
  Stream<List<BoardStatus>> get boardsStream => _boardsController.stream;

  @override
  Stream<String> get errorStream => _errorController.stream;

  /// Publishes [message] on [errorStream] as the real service does when a
  /// request fails infrastructurally.
  ///
  /// The mock has no network to fail, so before this the stream was declared
  /// and never used, and the whole failure→`AlarmService.lastError`→SnackBar
  /// path in [MainShell] had no test covering it at all.
  @visibleForTesting
  void emitError(String message) {
    if (!_errorController.isClosed) _errorController.add(message);
  }

  /// Puts the system into [AlarmState.entryDelay] with [seconds] left, the
  /// way the door controller reporting `/esp32/entry-delay` does.
  ///
  /// Nothing in the mock's own arm/disarm flow produces this state (it has no
  /// door to open), so the countdown screen the person walking in is racing
  /// was never rendered in a test.
  @visibleForTesting
  void simulateEntryDelay({int seconds = 20}) {
    _state = AlarmState.entryDelay;
    _emitState();
    if (!_entryDelayController.isClosed) _entryDelayController.add(seconds);
  }

  @override
  Stream<List<ApprovedFace>> get facesStream => _facesController.stream;

  @override
  Stream<bool> get cameraOnlineStream => _cameraOnlineController.stream;

  // No real backend to stream from — the camera screen falls back to its
  // placeholder icon whenever this is null.
  @override
  Map<String, String> get cameraFrameHeaders => const {};
  @override
  String? get cameraStreamUrl => null;

  @override
  Stream<bool> get faceIdEnabledStream => _faceIdEnabledController.stream;

  @override
  Stream<bool> get faceDisarmEnabledStream => _faceDisarmEnabledController.stream;

  @override
  Stream<int> get entryDelayStream => _entryDelayController.stream;

  /// Checks [password] against the in-memory password. The artificial
  /// delay simulates network latency so UI loading states (spinners) have
  /// something real to show during development. On success, re-emits the
  /// current alarm state and sensor readings so a freshly-logged-in
  /// listener (which only just subscribed) sees the current picture
  /// rather than waiting for the next unrelated change.
  ///
  /// Wrong attempts accumulate towards a lockout exactly as the real backend
  /// counts them (see [_maxAttempts]); a correct password clears the count.
  /// While locked out the right password is refused too — that is the point
  /// of a lockout, and it is also what makes the refusal safe to simulate
  /// here without weakening anything: the real decision still happens
  /// server-side.
  @override
  Future<LoginResult> login(String password) async {
    await Future.delayed(const Duration(milliseconds: 500));

    final lockedUntil = _lockedOutUntil;
    if (lockedUntil != null) {
      final remaining = lockedUntil.difference(DateTime.now());
      if (remaining > Duration.zero) {
        return LoginResult(
          ok: false,
          lockedOut: true,
          lockoutRemainingSeconds: remaining.inSeconds + 1,
        );
      }
      _lockedOutUntil = null;
      _failedAttempts = 0;
    }

    if (password == _password) {
      _failedAttempts = 0;
      _logEvent('Logged in', EventCategory.auth);
      _emitState();
      _emitSensors();
      return const LoginResult(ok: true, attemptsRemaining: _maxAttempts);
    }

    _failedAttempts++;
    if (_failedAttempts >= _maxAttempts) {
      _lockedOutUntil = DateTime.now().add(_lockoutDuration);
      _logEvent('Too many failed logins — locked out', EventCategory.auth);
      return LoginResult(
        ok: false,
        lockedOut: true,
        lockoutRemainingSeconds: _lockoutDuration.inSeconds,
      );
    }
    return LoginResult(ok: false, attemptsRemaining: _maxAttempts - _failedAttempts);
  }

  /// No-op: the mock has no session to invalidate, since it never issued a
  /// token. Present so [AuthService.logout] can call the same method against
  /// either implementation.
  @override
  Future<void> logout() async {}

  /// Changes the stored password/keypad-PIN, provided [oldPassword] matches
  /// the current one and [newPassword] is 4-8 digits (mirrors the real
  /// service's validation so a widget test exercises the same accept/reject
  /// boundary). `hardware: true` because there is no board here to fail —
  /// the mock stands in for a working system, and the unreachable-board
  /// case belongs to the real service's tests.
  @override
  Future<ChangePasswordResult> changePassword(String oldPassword, String newPassword) async {
    await Future.delayed(const Duration(milliseconds: 400));
    if (oldPassword != _password) return const ChangePasswordResult(ok: false);
    if (!RegExp(r'^\d{4,8}$').hasMatch(newPassword)) {
      return const ChangePasswordResult(ok: false);
    }
    _password = newPassword;
    _logEvent('Password changed', EventCategory.auth);
    return const ChangePasswordResult(ok: true, hardware: true);
  }

  /// Arms the system in [mode]. Only takes effect if currently disarmed
  /// (arming an already-armed/triggered system is a no-op — there's
  /// nothing sensible to transition from). Immediately moves to
  /// [AlarmState.transitional] so the UI can show an exit-delay
  /// countdown, then after a fixed delay flips to [AlarmState.armed] and
  /// marks each sensor's bypass state (vibration is bypassed in Home
  /// mode — see [ArmMode]). The delay is a fixed demo value standing in
  /// for whatever exit-delay the real hardware/backend would use.
  @override
  Future<void> arm(ArmMode mode) async {
    if (_state != AlarmState.disarmed) return;
    _logEvent(
      mode == ArmMode.away ? 'Arming (Away) — exit now' : 'Arming (Home) — exit now',
      EventCategory.armDisarm,
    );
    _state = AlarmState.transitional;
    _emitState();
    _armingTimer?.cancel();
    _armingTimer = Timer(const Duration(seconds: 3), () {
      _state = AlarmState.armed;
      _sensors = _sensors
          .map((s) => SensorReading(
                type: s.type,
                label: s.label,
                ok: true,
                bypassed: mode == ArmMode.home && s.type == SensorType.vibration,
              ))
          .toList();
      _logEvent(
        'System armed (${mode == ArmMode.away ? 'Away' : 'Home'})',
        EventCategory.armDisarm,
      );
      _emitState();
      _emitSensors();
    });
  }

  /// Disarms the system from any state. Cancels a pending [_armingTimer]
  /// first so a disarm during the exit delay actually stops the
  /// in-flight transition to `armed` rather than letting it fire anyway a
  /// moment later. Sensors are reset (cleared of any breach/bypass
  /// flags) unconditionally, even if already disarmed, so the sensor
  /// panel never gets stuck showing a stale breach.
  @override
  Future<void> disarm() async {
    _armingTimer?.cancel();
    _resetSensors();
    if (_state == AlarmState.disarmed) return;
    _state = AlarmState.disarmed;
    _logEvent('System disarmed', EventCategory.armDisarm);
    _emitState();
  }

  /// Records a sensor breach (or a generic alert if [source] is null) and
  /// escalates to [AlarmState.triggered]. See interface doc on
  /// [BackendService.triggerAlert] for the no-op-when-disarmed contract.
  ///
  /// [AlarmState.entryDelay] escalates too, matching `trigger_alert` in
  /// python/backend/state.py. Guarding on `armed` alone meant a second sensor
  /// tripping while someone was mid-entry-countdown was silently ignored
  /// here but not by the backend — a mock that disagrees with the real thing
  /// about when the alarm goes off is worse than no mock.
  @override
  Future<void> triggerAlert({SensorType? source, required String reason}) async {
    if (_state != AlarmState.armed && _state != AlarmState.entryDelay) return;

    // A bypassed sensor (e.g. vibration in Home mode) shouldn't be able
    // to escalate to a full alarm — that's the whole point of bypassing
    // it. Real sensor hardware doesn't know about arm modes, so this
    // check has to live here, not on the reporting side.
    if (source != null) {
      final reportingSensor = _sensors.where((s) => s.type == source).firstOrNull;
      if (reportingSensor != null && reportingSensor.bypassed) return;

      _sensors = _sensors
          .map((s) => s.type == source
              ? SensorReading(type: s.type, label: s.label, ok: false, bypassed: s.bypassed)
              : s)
          .toList();
      _emitSensors();
    }
    _state = AlarmState.triggered;
    _logEvent('ALERT — $reason', EventCategory.alert);
    _emitState();
  }

  /// Picks a random approved face and logs a "recognized" event for it,
  /// standing in for what a real camera + face-recognition model would
  /// report. No-ops if there are no approved faces or face-ID is off, to
  /// mirror how the real flow couldn't recognize anyone in either case.
  @override
  Future<void> simulateFaceRecognition() async {
    if (_faces.isEmpty || !_faceIdEnabled) return;
    final face = _faces[Random().nextInt(_faces.length)];
    _logEvent('Face recognized: ${face.name}', EventCategory.faceId);
  }

  /// Adds [name] to the approved-faces list, assigning it the next
  /// sequential id.
  @override
  Future<String?> addFace(String name) async {
    await Future.delayed(const Duration(milliseconds: 300));
    final id = '${_nextFaceId++}';
    _faces.add(ApprovedFace(id: id, name: name));
    _facesController.add(List.unmodifiable(_faces));
    _logEvent('Approved face added: $name', EventCategory.faceId);
    return id;
  }

  /// Accepts any non-empty frame. There is no recognizer here to find a face
  /// in one, and inventing a rejection would make widget tests assert against
  /// a rule the real backend doesn't apply in the same way — the real 422
  /// case is covered on the Python side.
  @override
  Future<bool> addFacePhoto(String id, Uint8List jpeg) async {
    await Future.delayed(const Duration(milliseconds: 200));
    if (jpeg.isEmpty) return false;
    if (!_faces.any((f) => f.id == id)) return false;
    _logEvent('Training photo added', EventCategory.faceId);
    return true;
  }

  /// A one-pixel JPEG stand-in. Non-null so the enrolment flow is reachable
  /// in widget tests; `_cameraHasFrame` lets a test force the no-frame branch
  /// that the real service hits before any camera has ever connected.
  @override
  Future<Uint8List?> captureCameraFrame() async {
    await Future.delayed(const Duration(milliseconds: 200));
    return _cameraHasFrame ? Uint8List.fromList(const [0xFF, 0xD8, 0xFF, 0xD9]) : null;
  }

  /// Test seam for the branch above. Not `final`, and deliberately public:
  /// the mock's job is to make every real-service outcome reachable from a
  /// widget test, and this one is otherwise unreachable.
  bool _cameraHasFrame = true;
  set cameraHasFrame(bool value) => _cameraHasFrame = value;

  /// Removes the approved face matching [id], if any. Looks the face up
  /// before removing it purely so the log message can include its name
  /// (once removed from `_faces`, the name would no longer be available).
  @override
  Future<void> removeFace(String id) async {
    await Future.delayed(const Duration(milliseconds: 200));
    final face = _faces.where((f) => f.id == id).firstOrNull;
    _faces.removeWhere((f) => f.id == id);
    _facesController.add(List.unmodifiable(_faces));
    if (face != null) {
      _logEvent('Approved face removed: ${face.name}', EventCategory.faceId);
    }
  }

  /// Turns face-ID recognition on/off.
  @override
  Future<void> setFaceIdEnabled(bool enabled) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _faceIdEnabled = enabled;
    _faceIdEnabledController.add(_faceIdEnabled);
  }

  /// Mirrors the backend's opt-in face-disarm toggle.
  @override
  Future<void> setFaceDisarmEnabled(bool enabled) async {
    _faceDisarmEnabled = enabled;
    _faceDisarmEnabledController.add(enabled);
  }

  @override
  Future<AppSettings?> getSettings() async => AppSettings(
        nightMode: _nightMode,
        nightModeArmHour: _nightModeArmHour,
        nightModeDisarmHour: _nightModeDisarmHour,
        emergencyNumber: _emergencyNumber,
        policeCallDelaySeconds: _policeCallDelaySeconds,
        ntfy: NtfyConfig(server: _ntfyServer, topic: _ntfyTopic),
      );

  /// Enables/disables night mode and logs the change (unlike most other
  /// settings setters here, this one is user-visible enough in the event
  /// log to be worth recording).
  /// Clamps to 0-23 the way the backend does, so the mock can't accept a
  /// window the real service would silently correct.
  @override
  Future<void> setNightModeHours(int armHour, int disarmHour) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _nightModeArmHour = armHour.clamp(0, 23);
    _nightModeDisarmHour = disarmHour.clamp(0, 23);
  }

  @override
  Future<void> setNightMode(bool enabled) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _nightMode = enabled;
    _logEvent(
      enabled ? 'Night mode enabled — auto-arms overnight' : 'Night mode disabled',
      EventCategory.info,
    );
  }

  @override
  Future<void> setEmergencyNumber(String number) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _emergencyNumber = number;
  }

  @override
  Future<void> setPoliceCallDelaySeconds(int seconds) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _policeCallDelaySeconds = seconds;
  }

  @override
  Future<void> setNtfyConfig(String server, String topic) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _ntfyServer = server.trim().isEmpty ? 'https://ntfy.sh' : server.trim();
    _ntfyTopic = topic.trim();
  }

  /// Reports success whenever a topic is set. The mock has no network, so
  /// nothing is actually sent — enough for UI work, and the real delivery
  /// path is covered by the backend's own tests.
  @override
  Future<bool> sendTestNotification() async {
    await Future.delayed(const Duration(milliseconds: 200));
    return _ntfyTopic.isNotEmpty;
  }

  /// Clears every sensor back to "ok" and not bypassed. Used on disarm so
  /// the next arm cycle (and the sensor panel in the meantime) doesn't
  /// carry over a breach or a bypass flag from the previous armed period.
  void _resetSensors() {
    _sensors = _sensors.map((s) => SensorReading(type: s.type, label: s.label, ok: true)).toList();
    _emitSensors();
  }

  /// Publishes the current [_state] on [_alarmStateController]. Guarded
  /// by [StreamController.isClosed] because several callers (login,
  /// arm, disarm, triggerAlert) `await` a simulated delay before
  /// emitting — if [dispose] runs and closes the controllers while one
  /// of those is still pending, the resumed code would otherwise throw
  /// trying to add to a closed sink.
  void _emitState() {
    if (!_alarmStateController.isClosed) {
      _alarmStateController.add(_state);
    }
  }

  /// Publishes the current [_sensors] list on [_sensorController]. Same
  /// closed-controller guard as [_emitState], for the same reason.
  void _emitSensors() {
    if (!_sensorController.isClosed) {
      _sensorController.add(List.unmodifiable(_sensors));
    }
  }

  /// Prepends a new [SecurityEvent] (built from [message]/[category]) to
  /// the in-memory log and republishes the whole list. Inserted at index
  /// 0 rather than appended because the log and event streams present
  /// newest-first. Same closed-controller guard as [_emitState].
  void _logEvent(String message, [EventCategory category = EventCategory.info]) {
    _events.insert(
      0,
      SecurityEvent(timestamp: DateTime.now(), message: message, category: category),
    );
    if (!_eventLogController.isClosed) {
      _eventLogController.add(List.unmodifiable(_events));
    }
  }

  /// Cancels the pending arming timer (if any) and closes every stream
  /// controller. Must be called when this service is no longer needed —
  /// leaving broadcast StreamControllers open leaks them for the life of
  /// the app.
  @override
  void dispose() {
    _armingTimer?.cancel();
    _alarmStateController.close();
    _eventLogController.close();
    _sensorController.close();
    _connectivityController.close();
    _linkController.close();
    _boardsController.close();
    _errorController.close();
    _facesController.close();
    _cameraOnlineController.close();
    _faceIdEnabledController.close();
    _faceDisarmEnabledController.close();
    _entryDelayController.close();
  }
}
