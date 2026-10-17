import 'dart:async';

import 'package:flutter/foundation.dart';

import 'backend_service.dart';
import 'notification_service.dart';

/// Bridges [BackendService]'s streams into a single [ChangeNotifier] the
/// widget tree can `watch`/`read` with `provider`, and adds the small
/// bits of derived behavior the UI needs on top (triggered-alert
/// notifications).
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
      // Same discipline for the entry-delay ticker: it belongs to that state
      // and must not keep counting once a disarm (or the alarm itself) has
      // ended the window.
      if (state != AlarmState.entryDelay) {
        _entryDelayTimer?.cancel();
        _entryDelaySecondsRemaining = 0;
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
      // Same reasoning as _offlineBoards: the camera tab filtered the whole
      // log (up to 200 entries) on every single build to find the handful of
      // face-ID rows it shows.
      _faceIdEvents =
          events.where((e) => e.category == EventCategory.faceId).toList(growable: false);
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
    // Whether *this app* can still hear the backend, as opposed to whether
    // the backend can hear the boards. See BackendService.backendLinkStream.
    _linkSub = _backend.backendLinkStream.listen((linked) {
      _isBackendLinked = linked;
      notifyListeners();
    });
    _boardsSub = _backend.boardsStream.listen((boards) {
      _boards = boards;
      // Derived once per update rather than per build: the dashboard reads
      // this on every frame (and used to read it twice in the same build),
      // while boards change a few times an hour at most.
      _offlineBoards = boards.where((b) => !b.online).toList(growable: false);
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
    // Seeded by the backend, ticked locally in between.
    //
    // The comment that used to sit here said the backend "re-broadcasts
    // what's left", so no local clock was needed. It doesn't:
    // `begin_entry_delay` broadcasts exactly once, and the only periodic loop
    // (`_monitor_boards`) pushes solely when a board's liveness *changes*. So
    // the app received "20" and then nothing — the number froze at 20 until
    // the alarm fired. That is precisely the failure `AlarmState.entryDelay`
    // was introduced to fix: someone walking in, watching a countdown that
    // isn't counting.
    //
    // Every backend value re-seeds the local ticker, so the authority still
    // wins and drift is bounded to one broadcast interval. The ticker only
    // ever moves the *displayed* number — it must never drive the state
    // itself, which is why nothing here touches `_state`.
    _entryDelaySub = _backend.entryDelayStream.listen((seconds) {
      _entryDelaySecondsRemaining = seconds;
      _restartEntryDelayCountdown(seconds);
      notifyListeners();
    });
  }

  /// (Re)starts the local entry-delay countdown, or stops it when there is
  /// nothing left to count. Cancelled on any state change away from
  /// [AlarmState.entryDelay] and in [dispose], so a stale ticker can never
  /// outlive the window it belongs to.
  void _restartEntryDelayCountdown(int seconds) {
    _entryDelayTimer?.cancel();
    if (seconds <= 0) return;
    _entryDelayTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (_entryDelaySecondsRemaining <= 0) {
        _entryDelayTimer?.cancel();
        return;
      }
      _entryDelaySecondsRemaining--;
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
  late final StreamSubscription<bool> _linkSub;
  late final StreamSubscription<List<BoardStatus>> _boardsSub;
  late final StreamSubscription<String> _errorSub;
  late final StreamSubscription<List<ApprovedFace>> _facesSub;
  late final StreamSubscription<bool> _cameraOnlineSub;
  late final StreamSubscription<bool> _faceIdSub;
  late final StreamSubscription<bool> _faceDisarmSub;
  late final StreamSubscription<int> _entryDelaySub;

  AlarmState _state = AlarmState.disarmed;
  List<SecurityEvent> _events = const [];
  List<SecurityEvent> _faceIdEvents = const [];
  List<SensorReading> _sensors = const [];
  // Both start false: before the first state frame the app knows nothing, and
  // "nothing yet" must not render as "everything is fine" on an alarm panel.
  // _isBackendLinked being false is what keeps _isOnline's initial value off
  // the screen entirely — the badge shows the no-link treatment instead of a
  // board status it hasn't actually been told.
  bool _isOnline = false;
  bool _isBackendLinked = false;
  List<BoardStatus> _boards = const [];
  List<BoardStatus> _offlineBoards = const [];
  String? _lastError;
  List<ApprovedFace> _faces = const [];
  bool _isCameraOnline = true;
  bool _faceIdEnabled = true;
  bool _faceDisarmEnabled = false;
  int _entryDelaySecondsRemaining = 0;
  int _armingSecondsRemaining = 0;
  Timer? _armingTimer;
  Timer? _entryDelayTimer;

  AlarmState get state => _state;
  /// Seconds left in the local arming countdown, only meaningful while
  /// [state] is [AlarmState.transitional] — see [_armingExitDelay].
  int get armingSecondsRemaining => _armingSecondsRemaining;
  List<SecurityEvent> get events => _events;
  List<SensorReading> get sensors => _sensors;
  bool get isOnline => _isOnline;

  /// Whether the app still has a live link to the backend — i.e. whether
  /// anything on this screen is still being updated. False means every value
  /// here is the last one heard, however long ago that was.
  bool get isBackendLinked => _isBackendLinked;

  /// Per-board liveness for the dashboard (see [BoardStatus]). Empty until
  /// the first state frame arrives, or when talking to a backend that
  /// predates per-board tracking.
  List<BoardStatus> get boards => _boards;

  /// Boards the backend has stopped hearing from — the ones worth calling
  /// out on the dashboard. An empty list means everything is reporting.
  List<BoardStatus> get offlineBoards => _offlineBoards;

  /// Most recent infrastructural failure, or null if none is outstanding.
  /// Cleared via [clearError] once shown.
  String? get lastError => _lastError;
  List<ApprovedFace> get faces => _faces;
  bool get isCameraOnline => _isCameraOnline;
  bool get faceIdEnabled => _faceIdEnabled;

  /// Whether a recognized face may disarm the system (opt-in — see
  /// [BackendService.faceDisarmEnabledStream]).
  bool get faceDisarmEnabled => _faceDisarmEnabled;

  /// Seconds left to disarm during [AlarmState.entryDelay]. Seeded by the
  /// backend and ticked down locally between its pushes — see the
  /// subscription in the constructor for why both halves are needed.
  int get entryDelaySecondsRemaining => _entryDelaySecondsRemaining;
  /// Just the subset of [events] relevant to the Camera/face-ID tab,
  /// recomputed when the log changes rather than on every build.
  List<SecurityEvent> get faceIdEvents => _faceIdEvents;

  // Thin pass-throughs to the backend — the resulting state change is
  // observed back through the stream subscriptions wired up in the
  // constructor, not through these calls' return values.
  Future<void> arm(ArmMode mode) => _backend.arm(mode);
  Future<void> disarm() => _backend.disarm();

  /// Enrols a new approved face: creates the record, grabs the newest camera
  /// frame, and attaches it as a training photo.
  ///
  /// Both halves matter. A record on its own has no embedding to compare
  /// against, so it can never match anyone — which is what the app used to
  /// create, under a list captioned "recognized as residents". The frame is
  /// captured *first*, so a system with no camera frame available doesn't
  /// leave an inert record behind for the user to wonder about.
  Future<EnrollFaceOutcome> enrollFace(String name) async {
    final frame = await _backend.captureCameraFrame();
    if (frame == null) return EnrollFaceOutcome.noFrame;

    final id = await _backend.addFace(name);
    if (id == null) return EnrollFaceOutcome.failed;

    final accepted = await _backend.addFacePhoto(id, frame);
    return accepted ? EnrollFaceOutcome.enrolled : EnrollFaceOutcome.noFaceInFrame;
  }
  Future<void> removeFace(String id) => _backend.removeFace(id);
  Future<void> setFaceIdEnabled(bool enabled) => _backend.setFaceIdEnabled(enabled);
  Future<void> setFaceDisarmEnabled(bool enabled) => _backend.setFaceDisarmEnabled(enabled);

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
    _linkSub.cancel();
    _boardsSub.cancel();
    _errorSub.cancel();
    _facesSub.cancel();
    _cameraOnlineSub.cancel();
    _faceIdSub.cancel();
    _faceDisarmSub.cancel();
    _entryDelaySub.cancel();
    _armingTimer?.cancel();
    _entryDelayTimer?.cancel();
    super.dispose();
  }
}
