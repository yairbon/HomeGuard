import 'dart:async';
import 'dart:math';

import 'backend_service.dart';

/// Simulates the ESP32 + FastAPI backend so the UI can be built and tested
/// before those exist. Password and timings are placeholders for demo use.
class MockBackendService implements BackendService {
  String _password = 'homeguard';

  final _alarmStateController = StreamController<AlarmState>.broadcast();
  final _eventLogController = StreamController<List<SecurityEvent>>.broadcast();
  final _sensorController = StreamController<List<SensorReading>>.broadcast();
  final _connectivityController = StreamController<bool>.broadcast();
  final _facesController = StreamController<List<ApprovedFace>>.broadcast();
  final _cameraOnlineController = StreamController<bool>.broadcast();
  final _faceIdEnabledController = StreamController<bool>.broadcast();

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
  int _nextFaceId = 3;

  bool _nightMode = false;
  String _emergencyNumber = '';
  int _policeCallDelaySeconds = 60;

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
      _cameraOnlineController.add(true);
      _faceIdEnabledController.add(_faceIdEnabled);
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
  Stream<List<ApprovedFace>> get facesStream => _facesController.stream;

  @override
  Stream<bool> get cameraOnlineStream => _cameraOnlineController.stream;

  @override
  Stream<bool> get faceIdEnabledStream => _faceIdEnabledController.stream;

  /// Checks [password] against the in-memory password. The artificial
  /// delay simulates network latency so UI loading states (spinners) have
  /// something real to show during development. On success, re-emits the
  /// current alarm state and sensor readings so a freshly-logged-in
  /// listener (which only just subscribed) sees the current picture
  /// rather than waiting for the next unrelated change.
  @override
  Future<LoginResult> login(String password) async {
    await Future.delayed(const Duration(milliseconds: 500));
    final ok = password == _password;
    if (ok) {
      _logEvent('Logged in', EventCategory.auth);
      _emitState();
      _emitSensors();
    }
    // Mock doesn't simulate lockout — that's tested against the real
    // backend, which is the actual authority on it.
    return LoginResult(ok: ok, attemptsRemaining: ok ? 3 : 2);
  }

  /// Changes the stored password, provided [oldPassword] matches the
  /// current one and [newPassword] isn't blank. Returns whether the
  /// change was applied.
  @override
  Future<bool> changePassword(String oldPassword, String newPassword) async {
    await Future.delayed(const Duration(milliseconds: 400));
    if (oldPassword != _password) return false;
    if (newPassword.trim().isEmpty) return false;
    _password = newPassword;
    _logEvent('Password changed', EventCategory.auth);
    return true;
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
  @override
  Future<void> triggerAlert({SensorType? source, required String reason}) async {
    if (_state != AlarmState.armed) return;

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
  Future<void> addFace(String name) async {
    await Future.delayed(const Duration(milliseconds: 300));
    _faces.add(ApprovedFace(id: '${_nextFaceId++}', name: name));
    _facesController.add(List.unmodifiable(_faces));
    _logEvent('Approved face added: $name', EventCategory.faceId);
  }

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

  @override
  Future<bool> getNightMode() async => _nightMode;

  /// Enables/disables night mode and logs the change (unlike most other
  /// settings setters here, this one is user-visible enough in the event
  /// log to be worth recording).
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
  Future<String> getEmergencyNumber() async => _emergencyNumber;

  @override
  Future<void> setEmergencyNumber(String number) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _emergencyNumber = number;
  }

  @override
  Future<int> getPoliceCallDelaySeconds() async => _policeCallDelaySeconds;

  @override
  Future<void> setPoliceCallDelaySeconds(int seconds) async {
    await Future.delayed(const Duration(milliseconds: 200));
    _policeCallDelaySeconds = seconds;
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
    _facesController.close();
    _cameraOnlineController.close();
    _faceIdEnabledController.close();
  }
}
