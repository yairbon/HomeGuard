import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'backend_service.dart';
import 'notification_service.dart';

/// Bridges [BackendService]'s streams into a single [ChangeNotifier] the
/// widget tree can `watch`/`read` with `provider`, and adds the small
/// bits of derived/demo behavior the UI needs on top (triggered-alert
/// notifications, [simulateIntrusion], [simulateFaceRecognition]).
///
/// Must be constructed eagerly (see the `lazy: false` + comment on its
/// provider in main.dart), not lazily on first read: its constructor is
/// what subscribes to the backend's broadcast streams, and broadcast
/// streams don't replay past events to a subscriber that attaches late.
/// A lazy provider would only construct this the first time a widget
/// watched it — by which point the initial state/sensor/event emissions
/// from login could already have fired into the void with no listener
/// attached yet.
class AlarmService extends ChangeNotifier {
  /// Wires up one listener per backend stream, each of which just copies
  /// the new value into local state and calls [notifyListeners] so
  /// `provider`-watching widgets rebuild. Also layers in local-only
  /// behavior that isn't part of the backend contract: detecting the
  /// disarmed/other → triggered transition (`justTriggered`) to fire a
  /// device notification exactly once per triggering, rather than the
  /// backend needing to know anything about local notifications.
  AlarmService(this._backend) {
    _stateSub = _backend.alarmStateStream.listen((state) {
      final justTriggered = state == AlarmState.triggered && _state != AlarmState.triggered;
      // Entering the entry delay is its own notification-worthy moment: it
      // means a door just opened on an armed system. Waiting for the alarm
      // to actually fire would waste the whole countdown the user could
      // have used to disarm.
      final justEnteredEntryDelay =
          state == AlarmState.entryDelay && _state != AlarmState.entryDelay;
      final enteredTransitional = state == AlarmState.transitional && _state != AlarmState.transitional;
      _state = state;
      if (enteredTransitional) {
        _startArmingCountdown();
      } else if (state != AlarmState.transitional) {
        _armingTimer?.cancel();
        _armingSecondsRemaining = 0;
      }
      if (justTriggered) {
        NotificationService.showAlert(
          _events.isNotEmpty ? _events.first.message : 'Intrusion detected',
        );
      } else if (justEnteredEntryDelay) {
        NotificationService.showAlert('Door opened — disarm the system');
      }
      notifyListeners();
    });
    _logSub = _backend.eventLogStream.listen((events) {
      _events = events;
      notifyListeners();
    });
    _sensorSub = _backend.sensorStream.listen((sensors) {
      _sensors = sensors;
      notifyListeners();
    });
    _connectivitySub = _backend.connectivityStream.listen((online) {
      _isOnline = online;
      notifyListeners();
    });
    _boardsSub = _backend.boardsStream.listen((boards) {
      _boards = boards;
      notifyListeners();
    });
    // Infrastructural failures (backend unreachable, session expired) are
    // held as `lastError` rather than pushed straight to a snackbar, so the
    // widget layer decides how to present them and can clear them on
    // acknowledgement. See BackendService.errorStream for why these exist.
    _errorSub = _backend.errorStream.listen((message) {
      _lastError = message;
      notifyListeners();
    });
    _facesSub = _backend.facesStream.listen((faces) {
      _faces = faces;
      notifyListeners();
    });
    _cameraOnlineSub = _backend.cameraOnlineStream.listen((online) {
      _isCameraOnline = online;
      notifyListeners();
    });
    _faceIdSub = _backend.faceIdEnabledStream.listen((enabled) {
      _faceIdEnabled = enabled;
      notifyListeners();
    });
    _faceDisarmSub = _backend.faceDisarmEnabledStream.listen((enabled) {
      _faceDisarmEnabled = enabled;
      notifyListeners();
    });
    // Unlike the exit-delay countdown, this is not ticked locally: the door
    // controller owns the real timer and the backend re-broadcasts what's
    // left, so the number shown always comes from the authority rather than
    // a second clock that could drift away from it.
    _entryDelaySub = _backend.entryDelayStream.listen((seconds) {
      _entryDelaySecondsRemaining = seconds;
      notifyListeners();
    });
  }

  /// Mirrors `ARMING_EXIT_DELAY` in python/backend/state.py — the backend
  /// doesn't expose its exit-delay length over the API, so this is a
  /// display-only local countdown kept in sync by convention with that
  /// constant, not by asking the backend. If the two ever drift, the
  /// backend's actual `armed` transition (observed via [alarmStateStream])
  /// still wins; this timer only controls what number is shown while
  /// waiting for it, the same "local ticker doesn't re-check with the
  /// server" pattern AuthService uses for its lockout countdown.
  static const _armingExitDelay = Duration(seconds: 3);

  final BackendService _backend;
  late final StreamSubscription<AlarmState> _stateSub;
  late final StreamSubscription<List<SecurityEvent>> _logSub;
  late final StreamSubscription<List<SensorReading>> _sensorSub;
  late final StreamSubscription<bool> _connectivitySub;
  late final StreamSubscription<List<BoardStatus>> _boardsSub;
  late final StreamSubscription<String> _errorSub;
  late final StreamSubscription<List<ApprovedFace>> _facesSub;
  late final StreamSubscription<bool> _cameraOnlineSub;
  late final StreamSubscription<bool> _faceIdSub;
  late final StreamSubscription<bool> _faceDisarmSub;
  late final StreamSubscription<int> _entryDelaySub;

  AlarmState _state = AlarmState.disarmed;
  List<SecurityEvent> _events = const [];
  List<SensorReading> _sensors = const [];
  bool _isOnline = true;
  List<BoardStatus> _boards = const [];
  String? _lastError;
  List<ApprovedFace> _faces = const [];
  bool _isCameraOnline = true;
  bool _faceIdEnabled = true;
  bool _faceDisarmEnabled = false;
  int _entryDelaySecondsRemaining = 0;
  int _armingSecondsRemaining = 0;
  Timer? _armingTimer;

  AlarmState get state => _state;
  /// Seconds left in the local arming countdown, only meaningful while
  /// [state] is [AlarmState.transitional] — see [_armingExitDelay].
  int get armingSecondsRemaining => _armingSecondsRemaining;
  List<SecurityEvent> get events => _events;
  List<SensorReading> get sensors => _sensors;
  bool get isOnline => _isOnline;

  /// Per-board liveness for the dashboard (see [BoardStatus]). Empty until
  /// the first state frame arrives, or when talking to a backend that
  /// predates per-board tracking.
  List<BoardStatus> get boards => _boards;

  /// Boards the backend has stopped hearing from — the ones worth calling
  /// out on the dashboard. An empty list means everything is reporting.
  List<BoardStatus> get offlineBoards => _boards.where((b) => !b.online).toList();

  /// Most recent infrastructural failure, or null if none is outstanding.
  /// Cleared via [clearError] once shown.
  String? get lastError => _lastError;
  List<ApprovedFace> get faces => _faces;
  bool get isCameraOnline => _isCameraOnline;
  bool get faceIdEnabled => _faceIdEnabled;

  /// Whether a recognized face may disarm the system (opt-in — see
  /// [BackendService.faceDisarmEnabledStream]).
  bool get faceDisarmEnabled => _faceDisarmEnabled;

  /// Seconds left to disarm during [AlarmState.entryDelay], straight from
  /// the backend rather than a local ticker.
  int get entryDelaySecondsRemaining => _entryDelaySecondsRemaining;
  /// Just the subset of [events] relevant to the Camera/face-ID tab,
  /// filtered by category rather than tracked as a separate list.
  List<SecurityEvent> get faceIdEvents =>
      _events.where((e) => e.category == EventCategory.faceId).toList();

  // Thin pass-throughs to the backend — the resulting state change is
  // observed back through the stream subscriptions wired up in the
  // constructor, not through these calls' return values.
  Future<void> arm(ArmMode mode) => _backend.arm(mode);
  Future<void> disarm() => _backend.disarm();

  Future<void> addFace(String name) => _backend.addFace(name);
  Future<void> removeFace(String id) => _backend.removeFace(id);
  Future<void> setFaceIdEnabled(bool enabled) => _backend.setFaceIdEnabled(enabled);
  Future<void> setFaceDisarmEnabled(bool enabled) => _backend.setFaceDisarmEnabled(enabled);

  /// Demo-only: picks one of the currently-monitored (non-bypassed)
  /// sensors and triggers it, so the alert flow can be seen without real
  /// hardware. Works against either backend — triggerAlert is a real
  /// interface method, not mock-specific.
  void simulateIntrusion() {
    final monitored = _sensors.where((s) => !s.bypassed).toList();
    if (monitored.isEmpty) return;
    final sensor = monitored[Random().nextInt(monitored.length)];
    _backend.triggerAlert(source: sensor.type, reason: '${sensor.label} breached');
  }

  /// Demo-only pass-through (see [BackendService.simulateFaceRecognition]).
  void simulateFaceRecognition() => _backend.simulateFaceRecognition();

  /// Marks [lastError] as handled, so the same message isn't shown twice on
  /// the next rebuild. Called by the widget that displayed it.
  void clearError() {
    if (_lastError == null) return;
    _lastError = null;
    notifyListeners();
  }

  /// Starts (or restarts) the display-only 1-second countdown shown on
  /// [AlarmStatusIndicator] while [state] is transitional. Counts down
  /// from [_armingExitDelay] regardless of how the backend's own timer is
  /// actually progressing — it exists purely so the arming screen shows a
  /// concrete number instead of an indefinite spinner-style pulse.
  void _startArmingCountdown() {
    _armingTimer?.cancel();
    _armingSecondsRemaining = _armingExitDelay.inSeconds;
    _armingTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      _armingSecondsRemaining--;
      if (_armingSecondsRemaining <= 0) {
        _armingTimer?.cancel();
        _armingSecondsRemaining = 0;
      }
      notifyListeners();
    });
  }

  /// Cancels every backend stream subscription so this service stops
  /// receiving (and reacting to) updates once it's torn down — otherwise
  /// each subscription would keep the underlying broadcast stream (and
  /// this now-disposed ChangeNotifier) alive indefinitely.
  @override
  void dispose() {
    _stateSub.cancel();
    _logSub.cancel();
    _sensorSub.cancel();
    _connectivitySub.cancel();
    _boardsSub.cancel();
    _errorSub.cancel();
    _facesSub.cancel();
    _cameraOnlineSub.cancel();
    _faceIdSub.cancel();
    _faceDisarmSub.cancel();
    _entryDelaySub.cancel();
    _armingTimer?.cancel();
    super.dispose();
  }
}
