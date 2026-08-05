import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

import 'backend_service.dart';

/// Talks to the real Python backend (python/) over REST + a WebSocket.
/// REST for anything the app initiates (login, arm, faces...); the
/// WebSocket for everything the app just needs to *know about* as it
/// happens (state/sensor/event changes — including ones caused by the
/// ESP32 boards or another device's app instance, not just this one).
///
/// The backend sends one `FullState` JSON blob per update (see
/// python/backend/models.py) — this class fans that single message out
/// into the individual streams the rest of the app expects, mirroring
/// how MockBackendService's separate StreamControllers work.
class HttpBackendService implements BackendService {
  /// [baseUrl] is the FastAPI backend's HTTP origin (REST calls use it
  /// as-is; the WebSocket URL is derived from it in [_connect] by
  /// swapping the scheme to ws/wss). Immediately kicks off the WebSocket
  /// connection so live state starts flowing as soon as this service
  /// exists, without waiting for the first widget to subscribe.
  HttpBackendService({this.baseUrl = 'http://localhost:8000'}) {
    _connect();
  }

  final String baseUrl;

  final _alarmStateController = StreamController<AlarmState>.broadcast();
  final _eventLogController = StreamController<List<SecurityEvent>>.broadcast();
  final _sensorController = StreamController<List<SensorReading>>.broadcast();
  final _connectivityController = StreamController<bool>.broadcast();
  final _facesController = StreamController<List<ApprovedFace>>.broadcast();
  final _cameraOnlineController = StreamController<bool>.broadcast();
  final _faceIdEnabledController = StreamController<bool>.broadcast();

  WebSocketChannel? _channel;
  StreamSubscription? _wsSub;
  Timer? _reconnectTimer;
  bool _disposed = false;

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

  // ---- WebSocket: receiving live state -------------------------------------

  /// Opens the WebSocket connection to the backend's `/ws` endpoint and
  /// starts listening for state updates. Bails out immediately if
  /// [dispose] has already run (guards against a reconnect timer firing
  /// after disposal — see [_scheduleReconnect]). Both a synchronous
  /// connection failure (caught here) and an async one (`onDone`/
  /// `onError` on the subscription, e.g. the backend closing the socket
  /// or dropping off the network) fall back to [_scheduleReconnect]
  /// rather than surfacing an error, since there's no caller in a
  /// position to handle a broadcast stream failing — the UI should just
  /// keep retrying and show "offline" via [connectivityStream] instead.
  void _connect() {
    if (_disposed) return;
    final wsUrl = baseUrl.replaceFirst('http', 'ws');
    try {
      _channel = WebSocketChannel.connect(Uri.parse('$wsUrl/ws'));
      _wsSub = _channel!.stream.listen(
        _handleMessage,
        onDone: _scheduleReconnect,
        onError: (_) => _scheduleReconnect(),
        cancelOnError: true,
      );
    } catch (_) {
      _scheduleReconnect();
    }
  }

  /// Retries [_connect] after a fixed backoff. Cancels any existing timer
  /// first so overlapping failures (e.g. `onError` then `onDone` for the
  /// same socket) don't stack up multiple pending reconnect attempts.
  void _scheduleReconnect() {
    if (_disposed) return;
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 3), _connect);
  }

  /// Parses one `FullState` JSON message from the WebSocket (see
  /// `python/backend/models.py`) and fans its fields out to each of this
  /// service's individual stream controllers, so the rest of the app can
  /// keep listening to narrow, typed streams instead of one big blob.
  ///
  /// Publication order is load-bearing, not cosmetic: `alarm_state` goes
  /// out **last**. These are async broadcast controllers, so listeners run
  /// in the order their events were queued — and [AlarmService]'s
  /// alarm-state listener reads its own already-received `_events` list to
  /// caption the intrusion notification. Adding the state first meant that
  /// listener ran while `_events` still held the *previous* snapshot, so a
  /// break-in notification was captioned with the event before it (e.g.
  /// "System armed (Away)") instead of the alert that just fired.
  /// [MockBackendService] already emitted in this order — that's why the
  /// bug never showed up under the widget tests.
  ///
  /// Wrapped in a try/catch because a malformed or unexpected payload
  /// (`byName` throws on an enum value this build doesn't know, e.g. after
  /// a backend adds a SensorType the app hasn't shipped yet) would
  /// otherwise throw straight out of a stream callback as an uncaught
  /// async error. Dropping one bad frame and waiting for the next
  /// broadcast is the right failure mode for a live status channel.
  void _handleMessage(dynamic raw) {
    try {
      final json = jsonDecode(raw as String) as Map<String, dynamic>;

      _sensorController.add(
        (json['sensors'] as List).map((s) => _sensorFromJson(s as Map<String, dynamic>)).toList(),
      );

      _eventLogController.add(
        (json['events'] as List).map((e) => _eventFromJson(e as Map<String, dynamic>)).toList(),
      );

      _facesController.add(
        (json['faces'] as List)
            .map((f) => ApprovedFace(id: f['id'] as String, name: f['name'] as String))
            .toList(),
      );

      _connectivityController.add(json['esp32_online'] as bool);
      _cameraOnlineController.add(json['camera_online'] as bool);
      _faceIdEnabledController.add(json['face_id_enabled'] as bool);

      // Last, so every list above is already delivered — see the doc note.
      _alarmStateController.add(AlarmState.values.byName(json['alarm_state'] as String));
    } catch (_) {
      // Intentionally silent: there is no caller in a position to handle
      // this, and the next state broadcast supersedes whatever was lost.
    }
  }

  /// Decodes one entry of the WebSocket message's `sensors` array into a
  /// [SensorReading]. `bypassed` defaults to `false` for backward/forward
  /// compatibility with backend payloads that omit it.
  SensorReading _sensorFromJson(Map<String, dynamic> j) => SensorReading(
        type: SensorType.values.byName(j['type'] as String),
        label: j['label'] as String,
        ok: j['ok'] as bool,
        bypassed: j['bypassed'] as bool? ?? false,
      );

  /// Decodes one entry of the WebSocket message's `events` array into a
  /// [SecurityEvent].
  SecurityEvent _eventFromJson(Map<String, dynamic> j) => SecurityEvent(
        timestamp: DateTime.parse(j['timestamp'] as String),
        message: j['message'] as String,
        category: EventCategory.values.byName(j['category'] as String),
      );

  // ---- REST helpers ---------------------------------------------------------
  // Networks fail — a dropped WiFi connection or an unreachable backend
  // shouldn't crash the app. Callers get an empty map back and treat
  // missing fields as their safe default (see the `as bool? ?? false`
  // style casts below) rather than an unhandled exception.

  /// POSTs [body] (default `{}`) as JSON to `$baseUrl$path` and returns
  /// the decoded JSON response, or `{}` on any failure (unreachable host,
  /// non-JSON body, empty body, etc.) — see the swallowing rationale in
  /// the section comment above. Every REST-based method on this class is
  /// built on this helper specifically so none of them need their own
  /// try/catch.
  Future<Map<String, dynamic>> _post(String path, [Map<String, dynamic>? body]) async {
    try {
      final response = await http.post(
        Uri.parse('$baseUrl$path'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body ?? {}),
      );
      if (response.body.isEmpty) return {};
      return jsonDecode(response.body) as Map<String, dynamic>;
    } catch (_) {
      return {};
    }
  }

  /// GETs `$baseUrl$path` and returns the decoded JSON response, or `{}`
  /// on any failure — same swallow-and-fall-back-to-empty contract as
  /// [_post] (an empty/invalid body throws inside `jsonDecode`, which the
  /// catch here turns into `{}` just like an unreachable host would).
  Future<Map<String, dynamic>> _get(String path) async {
    try {
      final response = await http.get(Uri.parse('$baseUrl$path'));
      return jsonDecode(response.body) as Map<String, dynamic>;
    } catch (_) {
      return {};
    }
  }

  // ---- Auth -------------------------------------------------------------------

  /// Posts [password] to `/login` and translates the backend's response
  /// into a [LoginResult]. Every field falls back to its "nothing
  /// happened" default (`ok: false`, no lockout, 0 attempts) so that a
  /// swallowed network failure (see [_post]) reads as a declined login
  /// rather than a crash — the user just sees "wrong password" instead
  /// of an error dialog, and can retry once connectivity returns.
  @override
  Future<LoginResult> login(String password) async {
    final res = await _post('/login', {'password': password});
    return LoginResult(
      ok: res['ok'] as bool? ?? false,
      lockedOut: res['locked_out'] as bool? ?? false,
      lockoutRemainingSeconds: res['lockout_remaining_seconds'] as int? ?? 0,
      attemptsRemaining: res['attempts_remaining'] as int? ?? 0,
    );
  }

  /// Posts the old/new password pair to `/change-password`. Returns
  /// `false` (rather than throwing) both when the backend rejects the
  /// change and when the request itself fails, for the same
  /// fail-safe-default reason as [login].
  @override
  Future<bool> changePassword(String oldPassword, String newPassword) async {
    final res = await _post('/change-password', {
      'old_password': oldPassword,
      'new_password': newPassword,
    });
    return res['ok'] as bool? ?? false;
  }

  // ---- Arm / disarm -------------------------------------------------------------

  /// Tells the backend to arm in [mode]; the resulting state change is
  /// observed via [alarmStateStream]/[sensorStream] over the WebSocket,
  /// not from this call's response.
  @override
  Future<void> arm(ArmMode mode) => _post('/arm', {'mode': mode.name});

  /// Tells the backend to disarm; see [arm] re: how the result is
  /// observed.
  @override
  Future<void> disarm() => _post('/disarm');

  /// Forwards a sensor/manual alert to the backend for it to evaluate and
  /// (if armed) escalate; see [BackendService.triggerAlert] for the
  /// no-op-when-disarmed contract, which the backend enforces here.
  @override
  Future<void> triggerAlert({SensorType? source, required String reason}) =>
      _post('/trigger-alert', {'source': source?.name, 'reason': reason});

  // ---- Faces --------------------------------------------------------------------

  /// Registers a new approved face under [name].
  @override
  Future<void> addFace(String name) => _post('/faces', {'name': name});

  /// Deletes the approved face with [id]. Uses `http.delete` directly
  /// since there's no `_delete` helper, but swallows failures the same way
  /// [_post]/[_get] do — this used to be the one REST call on the class
  /// that let a network error escape, which meant tapping delete with the
  /// backend unreachable threw out of a UI callback instead of quietly
  /// doing nothing like every neighbouring action does. The deletion is
  /// confirmed by the face disappearing from [facesStream], not by this
  /// call returning.
  @override
  Future<void> removeFace(String id) async {
    try {
      await http.delete(Uri.parse('$baseUrl/faces/$id'));
    } catch (_) {
      // Same swallow-and-fall-back contract as _post/_get above.
    }
  }

  /// Turns face-ID recognition on/off.
  @override
  Future<void> setFaceIdEnabled(bool enabled) => _post('/face-id-enabled', {'enabled': enabled});

  /// Asks the backend's debug endpoint to simulate a face-recognition
  /// event (see [BackendService.simulateFaceRecognition]).
  @override
  Future<void> simulateFaceRecognition() => _post('/debug/simulate-face-recognition');

  // ---- Settings -----------------------------------------------------------------
  // No dedicated GET endpoints for these — /state already carries them
  // (see python/backend/models.py FullState), so just read from there.

  @override
  Future<bool> getNightMode() async => (await _get('/state'))['night_mode'] as bool? ?? false;

  @override
  Future<void> setNightMode(bool enabled) => _post('/settings/night-mode', {'enabled': enabled});

  @override
  Future<String> getEmergencyNumber() async =>
      (await _get('/state'))['emergency_number'] as String? ?? '';

  @override
  Future<void> setEmergencyNumber(String number) =>
      _post('/settings/emergency-number', {'number': number});

  @override
  Future<int> getPoliceCallDelaySeconds() async =>
      (await _get('/state'))['police_call_delay_seconds'] as int? ?? 60;

  @override
  Future<void> setPoliceCallDelaySeconds(int seconds) =>
      _post('/settings/police-call-delay', {'seconds': seconds});

  /// Tears down the WebSocket connection and closes every stream
  /// controller. Sets [_disposed] first, before cancelling anything —
  /// [_connect]/[_scheduleReconnect] both check that flag, so this
  /// ordering guarantees a reconnect already in flight (e.g. a timer
  /// that fires in the same event-loop turn as this call) won't
  /// resurrect the connection right after we tear it down.
  @override
  void dispose() {
    _disposed = true;
    _reconnectTimer?.cancel();
    _wsSub?.cancel();
    _channel?.sink.close();
    _alarmStateController.close();
    _eventLogController.close();
    _sensorController.close();
    _connectivityController.close();
    _facesController.close();
    _cameraOnlineController.close();
    _faceIdEnabledController.close();
  }
}
