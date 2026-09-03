import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

import 'backend_service.dart';

/// Outcome of one REST call, distinguishing the three things that can
/// happen: the request succeeded, the backend refused it, or the request
/// never got an answer at all.
///
/// Before this existed every call funnelled through a `catch (_) => {}`, so
/// an unreachable backend and a rejected password produced byte-identical
/// results. On an alarm panel that is a real hazard — tapping DISARM while
/// the phone has no route to the backend would leave the UI looking exactly
/// as if the system had been disarmed.
class _Result {
  /// A response that actually came back from the backend, whatever its
  /// status code.
  const _Result({required this.statusCode, required this.body}) : reachable = true;

  /// The "never reached the server" case — no status code exists, because
  /// nothing ever answered.
  const _Result.unreachable() : statusCode = 0, body = const {}, reachable = false;

  final int statusCode;
  final Map<String, dynamic> body;

  /// False when the request failed at the transport level (no route, DNS
  /// failure, connection refused, timeout) — i.e. the backend never saw it.
  final bool reachable;

  /// True only for a 2xx response that actually arrived.
  bool get ok => reachable && statusCode >= 200 && statusCode < 300;

  /// True when the backend rejected the session token — the app needs to
  /// send the user back to the login screen rather than retrying.
  bool get unauthorized => reachable && statusCode == 401;
}

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
///
/// Everything except `/health` and `/login` requires the session token
/// returned by [login]. That token is held only in memory, deliberately: it
/// is the credential that can disarm the alarm, so it should not outlive the
/// process on a device that might be lost or stolen — the user re-enters the
/// password on a cold start.
class HttpBackendService implements BackendService {
  /// [baseUrl] is the FastAPI backend's HTTP origin (REST calls use it
  /// as-is; the WebSocket URL is derived from it in [_connect] by
  /// swapping the scheme to ws/wss).
  ///
  /// Unlike the previous version, this does *not* open the WebSocket
  /// immediately — the backend now rejects an unauthenticated `/ws`
  /// handshake, so connecting before [login] would just burn reconnect
  /// attempts against a socket that can't be accepted yet. [login] opens it
  /// once there's a token to present.
  HttpBackendService({this.baseUrl = 'http://localhost:8000'});

  final String baseUrl;

  /// Session token from [login]. Null while signed out, which is also what
  /// makes [_connect] a no-op — there is nothing to authenticate with.
  String? _token;

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

  WebSocketChannel? _channel;
  StreamSubscription? _wsSub;
  Timer? _reconnectTimer;
  bool _disposed = false;

  /// Last value published on [backendLinkStream]. Starts false — nothing has
  /// been received from the backend yet, and claiming a live link before one
  /// exists is the exact lie this stream was added to stop telling.
  bool _linked = false;

  /// Set once a frame has failed to decode, cleared by the next frame that
  /// decodes cleanly. Stops a backend that keeps sending something this
  /// build can't read (a new enum value, say) from producing one SnackBar
  /// per state change for the rest of the session.
  bool _reportedBadFrame = false;

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
  Stream<List<ApprovedFace>> get facesStream => _facesController.stream;
  @override
  Stream<bool> get cameraOnlineStream => _cameraOnlineController.stream;
  @override
  Stream<bool> get faceIdEnabledStream => _faceIdEnabledController.stream;
  @override
  Stream<bool> get faceDisarmEnabledStream => _faceDisarmEnabledController.stream;
  @override
  Stream<int> get entryDelayStream => _entryDelayController.stream;
  @override
  Stream<String> get errorStream => _errorController.stream;

  @override
  Map<String, String> get cameraFrameHeaders =>
      _token == null ? const {} : {'Authorization': 'Bearer $_token'};
  @override
  String? get cameraStreamUrl => _token == null ? null : '$baseUrl/camera/stream';

  // ---- WebSocket: receiving live state -------------------------------------

  /// Opens the WebSocket connection to the backend's `/ws` endpoint and
  /// starts listening for state updates. Bails out immediately if [dispose]
  /// has already run (guards against a reconnect timer firing after
  /// disposal), or if there's no token yet — the backend closes an
  /// unauthenticated handshake with code 1008, so retrying without one would
  /// just spin.
  ///
  /// The token goes in the query string rather than a header because neither
  /// the browser WebSocket API nor `web_socket_channel` offers a portable way
  /// to set headers on the handshake; the backend reads it from there (see
  /// `websocket_endpoint` in python/backend/main.py).
  ///
  /// Both a synchronous connection failure (caught here) and an async one
  /// (`onDone`/`onError` on the subscription, e.g. the backend closing the
  /// socket or dropping off the network) fall back to [_scheduleReconnect]
  /// rather than surfacing an error, since there's no caller in a position to
  /// handle a broadcast stream failing — the UI should just keep retrying and
  /// show "offline" via [connectivityStream] instead.
  void _connect() {
    if (_disposed) return;
    final token = _token;
    if (token == null) return;

    // Only the scheme is rewritten, so a token or path containing "http"
    // can't be mangled the way a blanket replaceFirst on the whole URL could.
    final wsUrl = baseUrl.startsWith('https')
        ? baseUrl.replaceFirst('https', 'wss')
        : baseUrl.replaceFirst('http', 'ws');
    try {
      _channel = WebSocketChannel.connect(
        Uri.parse('$wsUrl/ws?token=${Uri.encodeQueryComponent(token)}'),
      );
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

  /// Publishes a change in link state on [backendLinkStream], but only when
  /// it actually changes — a state frame arrives on every backend event, and
  /// re-announcing "still connected" each time would rebuild every listener
  /// for nothing.
  void _setLinked(bool linked) {
    if (_linked == linked || _linkController.isClosed) return;
    _linked = linked;
    _linkController.add(linked);
  }

  /// Retries [_connect] after a fixed backoff. Cancels any existing timer
  /// first so overlapping failures (e.g. `onError` then `onDone` for the
  /// same socket) don't stack up multiple pending reconnect attempts.
  ///
  /// The link goes false here rather than only in [_disconnect]: this is the
  /// path a *dropped* socket takes, and it is the one that used to leave the
  /// dashboard frozen and green for as long as the reconnects kept failing.
  void _scheduleReconnect() {
    _setLinked(false);
    if (_disposed || _token == null) return;
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 3), _connect);
  }

  /// Closes the WebSocket without scheduling a reconnect. Used on logout,
  /// where the token that authenticated the socket is about to stop being
  /// valid.
  void _disconnect() {
    _setLinked(false);
    _reconnectTimer?.cancel();
    _wsSub?.cancel();
    _wsSub = null;
    _channel?.sink.close();
    _channel = null;
  }

  /// Parses one `FullState` JSON message from the WebSocket (see
  /// `python/backend/models.py`) and fans its fields out to each of this
  /// service's individual stream controllers, so the rest of the app can
  /// keep listening to narrow, typed streams instead of one big blob.
  ///
  /// Everything is decoded into locals *before* anything is published, so a
  /// message that turns out to be undecodable half way through leaves every
  /// stream on its previous value instead of applying a torn half-update.
  ///
  /// The whole body is wrapped because a throw inside an `onData` handler
  /// does not reach the subscription's `onError`: it escapes as an unhandled
  /// async error, so nothing reconnected, nothing was reported, the socket
  /// stayed open, and every subsequent frame threw in exactly the same
  /// place. Live updates stopped dead while the UI still said ONLINE. The
  /// reachable versions of that were `byName` on any enum value this build
  /// doesn't know (the backend has five `EventCategory` values and the log is
  /// where new ones get added) and the unchecked casts below.
  void _handleMessage(dynamic raw) {
    // A frame arriving at all is the proof the link works end to end, even if
    // this build can't make sense of its contents — that's a separate
    // problem, reported separately below.
    _setLinked(true);

    final AlarmState alarmState;
    final List<SensorReading> sensors;
    final List<SecurityEvent> events;
    final List<ApprovedFace> faces;
    final List<BoardStatus>? boards;
    final bool esp32Online;
    final bool cameraOnline;
    final bool faceIdEnabled;
    final bool faceDisarmEnabled;
    final int entryDelaySeconds;

    try {
      final json = jsonDecode(raw as String) as Map<String, dynamic>;

      // The one field with no safe fallback: a state this build has never
      // heard of can't be rendered as armed, disarmed or anything else, and
      // guessing on an alarm panel is worse than visibly not updating. The
      // frame is refused whole.
      final state = AlarmState.values.asNameMap()[json['alarm_state'] as String];
      if (state == null) throw FormatException('unknown alarm_state: ${json['alarm_state']}');
      alarmState = state;

      sensors = (json['sensors'] as List)
          .map((s) => _sensorFromJson(s as Map<String, dynamic>))
          // A sensor type this build doesn't know decodes to null: there is
          // no node to draw for it, but the three that are known still
          // render, which beats dropping the frame over it.
          .whereType<SensorReading>()
          .toList();

      events = (json['events'] as List)
          .map((e) => _eventFromJson(e as Map<String, dynamic>))
          .toList();

      faces = (json['faces'] as List)
          .map((f) => ApprovedFace(id: f['id'] as String, name: f['name'] as String))
          .toList();

      // Tolerated as absent so an app build can talk to a backend that
      // predates per-board tracking without throwing on every state frame.
      final rawBoards = json['boards'] as List?;
      boards = rawBoards?.map((b) => _boardFromJson(b as Map<String, dynamic>)).toList();

      esp32Online = json['esp32_online'] as bool;
      cameraOnline = json['camera_online'] as bool;
      faceIdEnabled = json['face_id_enabled'] as bool;
      // Tolerated as absent so this build still works against a backend that
      // predates face-disarm / the entry-delay countdown.
      faceDisarmEnabled = json['face_disarm_enabled'] as bool? ?? false;
      entryDelaySeconds = json['entry_delay_seconds_remaining'] as int? ?? 0;
    } catch (_) {
      if (!_reportedBadFrame) {
        _reportedBadFrame = true;
        _errorController.add(
          "Couldn't read the backend's update — the app may be out of date "
          'with it. Showing the last state received.',
        );
      }
      return;
    }
    _reportedBadFrame = false;

    _alarmStateController.add(alarmState);
    _sensorController.add(sensors);
    _eventLogController.add(events);
    _facesController.add(faces);
    if (boards != null) _boardsController.add(boards);
    _connectivityController.add(esp32Online);
    _cameraOnlineController.add(cameraOnline);
    _faceIdEnabledController.add(faceIdEnabled);
    _faceDisarmEnabledController.add(faceDisarmEnabled);
    _entryDelayController.add(entryDelaySeconds);
  }

  /// Decodes one entry of the WebSocket message's `sensors` array into a
  /// [SensorReading], or null for a sensor type this build has no node for.
  /// `bypassed` defaults to `false` for backward/forward compatibility with
  /// backend payloads that omit it.
  SensorReading? _sensorFromJson(Map<String, dynamic> j) {
    final type = SensorType.values.asNameMap()[j['type'] as String];
    if (type == null) return null;
    return SensorReading(
      type: type,
      label: j['label'] as String,
      ok: j['ok'] as bool,
      bypassed: j['bypassed'] as bool? ?? false,
    );
  }

  /// Decodes one entry of the WebSocket message's `events` array into a
  /// [SecurityEvent]. An unrecognized category falls back to
  /// [EventCategory.info] — the log is the part of the wire format most
  /// likely to grow a new category, and a badge in the wrong colour is a far
  /// better outcome than losing the whole event stream over it.
  SecurityEvent _eventFromJson(Map<String, dynamic> j) => SecurityEvent(
        timestamp: DateTime.parse(j['timestamp'] as String),
        message: j['message'] as String,
        category: EventCategory.values.asNameMap()[j['category'] as String] ?? EventCategory.info,
      );

  /// Decodes one entry of the WebSocket message's `boards` array into a
  /// [BoardStatus].
  BoardStatus _boardFromJson(Map<String, dynamic> j) => BoardStatus(
        name: j['name'] as String,
        online: j['online'] as bool,
        lastSeen: j['last_seen'] == null ? null : DateTime.parse(j['last_seen'] as String),
      );

  // ---- REST helpers ---------------------------------------------------------

  /// Headers for an authenticated request: JSON content type plus the
  /// session token, omitted while signed out so `/login` itself works.
  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (_token != null) 'Authorization': 'Bearer $_token',
      };

  /// Turns an `http.Response` into a [_Result], tolerating an empty or
  /// non-JSON body (several endpoints reply with a bare `{"ok":true}`, and
  /// error responses may not be JSON at all).
  _Result _decode(http.Response response) {
    Map<String, dynamic> body = const {};
    if (response.body.isNotEmpty) {
      try {
        final decoded = jsonDecode(response.body);
        if (decoded is Map<String, dynamic>) body = decoded;
      } catch (_) {
        // Non-JSON body (e.g. a plain-text proxy error). The status code is
        // still meaningful, so this isn't treated as unreachable.
      }
    }
    return _Result(statusCode: response.statusCode, body: body);
  }

  /// Reports a failed request on [errorStream], translating the two failure
  /// modes into messages a user can act on. Returns the result unchanged so
  /// callers can chain.
  _Result _report(_Result result, String action) {
    if (!result.reachable) {
      _errorController.add("Couldn't reach the backend — $action may not have taken effect");
    } else if (result.unauthorized) {
      _errorController.add('Session expired — sign in again');
    }
    return result;
  }

  /// POSTs [body] (default `{}`) as JSON to `$baseUrl$path`.
  Future<_Result> _post(String path, [Map<String, dynamic>? body]) async {
    try {
      return _decode(await http.post(
        Uri.parse('$baseUrl$path'),
        headers: _headers,
        body: jsonEncode(body ?? {}),
      ));
    } catch (_) {
      return const _Result.unreachable();
    }
  }

  /// GETs `$baseUrl$path`.
  Future<_Result> _get(String path) async {
    try {
      return _decode(await http.get(Uri.parse('$baseUrl$path'), headers: _headers));
    } catch (_) {
      return const _Result.unreachable();
    }
  }

  // ---- Auth -------------------------------------------------------------------

  /// Posts [password] to `/login` and translates the backend's response into
  /// a [LoginResult]. On success, stores the returned session token and opens
  /// the WebSocket — the socket can't be authenticated before this point, so
  /// login is what brings live state online.
  ///
  /// A transport failure surfaces on [errorStream] and comes back as a
  /// declined login, so the user sees "couldn't reach the backend" rather
  /// than being told their password was wrong.
  @override
  Future<LoginResult> login(String password) async {
    final res = await _post('/login', {'password': password});
    if (!res.reachable) {
      _errorController.add("Couldn't reach the backend — check the URL in Settings");
      return const LoginResult(ok: false, reachable: false);
    }

    final token = res.body['token'] as String?;
    if (token != null) {
      _token = token;
      _connect();
    }
    return LoginResult(
      ok: res.body['ok'] as bool? ?? false,
      lockedOut: res.body['locked_out'] as bool? ?? false,
      lockoutRemainingSeconds: res.body['lockout_remaining_seconds'] as int? ?? 0,
      attemptsRemaining: res.body['attempts_remaining'] as int? ?? 0,
    );
  }

  /// Revokes the session on the backend and tears down the authenticated
  /// WebSocket. The local token is cleared even if the revoke call fails —
  /// the user asked to sign out, and a token this app can no longer use is
  /// strictly better than one it keeps sending.
  @override
  Future<void> logout() async {
    if (_token != null) await _post('/logout');
    _token = null;
    _disconnect();
  }

  /// Posts the old/new value pair to `/change-password`, which now changes
  /// both this app's login and the physical keypad's PIN in one call (see
  /// [ChangePasswordResult]). Mirrors what the old, now-removed
  /// `/settings/keypad-pin` handler used to do — same three-field response
  /// shape, same reachability handling.
  @override
  Future<ChangePasswordResult> changePassword(String oldPassword, String newPassword) async {
    final res = _report(
      await _post('/change-password', {
        'old_password': oldPassword,
        'new_password': newPassword,
      }),
      'the password change',
    );
    if (!res.reachable) {
      return const ChangePasswordResult(ok: false, reachable: false);
    }
    return ChangePasswordResult(
      ok: res.body['ok'] as bool? ?? false,
      // Defaults matter here: a `false` that comes from a missing field
      // would claim the board didn't take the new code. That's the safe
      // direction to be wrong in — it tells the user to check the keypad
      // rather than assuring them of something unverified.
      hardware: res.body['hardware'] as bool? ?? false,
      secretsUpdated: res.body['secrets_updated'] as bool? ?? false,
    );
  }

  // ---- Arm / disarm -------------------------------------------------------------

  /// Tells the backend to arm in [mode]; the resulting state change is
  /// observed via [alarmStateStream]/[sensorStream] over the WebSocket,
  /// not from this call's response.
  ///
  /// The backend's reply also carries `hardware`, saying whether the door
  /// controller board actually acknowledged the command. A false there means
  /// the app's state and the physical panel may disagree, which is worth
  /// telling the user about even though the request itself succeeded.
  @override
  Future<void> arm(ArmMode mode) async {
    final res = _report(await _post('/arm', {'mode': mode.name}), 'arming');
    if (res.ok && res.body['hardware'] == false) {
      _errorController.add('Armed, but the door controller board did not respond');
    }
  }

  /// Tells the backend to disarm; see [arm] re: how the result is observed
  /// and why `hardware` is surfaced.
  @override
  Future<void> disarm() async {
    final res = _report(await _post('/disarm'), 'disarming');
    if (res.ok && res.body['hardware'] == false) {
      _errorController.add('Disarmed here, but the door controller board did not respond');
    }
  }

  /// Forwards a sensor/manual alert to the backend for it to evaluate and
  /// (if armed) escalate; see [BackendService.triggerAlert] for the
  /// no-op-when-disarmed contract, which the backend enforces here.
  @override
  Future<void> triggerAlert({SensorType? source, required String reason}) async {
    _report(await _post('/trigger-alert', {'source': source?.name, 'reason': reason}),
        'the test alert');
  }

  // ---- Faces --------------------------------------------------------------------

  /// Registers a new approved face under [name], returning the id the backend
  /// assigned it.
  @override
  Future<String?> addFace(String name) async {
    final res = _report(await _post('/faces', {'name': name}), 'adding the face');
    return res.ok ? res.body['id'] as String? : null;
  }

  /// Uploads one training photo for an existing face, as multipart/form-data
  /// under the field name `photo` — which is what the backend's
  /// `POST /faces/{id}/photo` handler expects.
  ///
  /// A 422 means "no face found in that frame", which is an ordinary outcome
  /// of pointing the camera at an empty hallway, so it is reported as `false`
  /// rather than pushed onto the error stream as a fault.
  @override
  Future<bool> addFacePhoto(String id, Uint8List jpeg) async {
    try {
      final request = http.MultipartRequest('POST', Uri.parse('$baseUrl/faces/$id/photo'))
        ..headers.addAll(_headers)
        ..files.add(http.MultipartFile.fromBytes('photo', jpeg, filename: 'face.jpg'));
      final streamed = await request.send();
      final response = await http.Response.fromStream(streamed);
      if (response.statusCode == 422) return false;
      return _report(_decode(response), 'the face photo').ok;
    } catch (_) {
      _report(const _Result.unreachable(), 'the face photo');
      return false;
    }
  }

  /// Fetches the newest frame the backend has cached from any camera.
  ///
  /// Returns raw JPEG bytes, not JSON, so it bypasses [_decode] entirely. A
  /// 404 is the ordinary "no camera has sent anything yet" answer and is not
  /// reported as a failure.
  @override
  Future<Uint8List?> captureCameraFrame() async {
    if (_token == null) return null;
    try {
      final response = await http.get(Uri.parse('$baseUrl/camera/latest'), headers: _headers);
      if (response.statusCode == 404) return null;
      if (response.statusCode != 200) {
        _report(_Result(statusCode: response.statusCode, body: const {}), 'the camera frame');
        return null;
      }
      return response.bodyBytes;
    } catch (_) {
      _report(const _Result.unreachable(), 'the camera frame');
      return null;
    }
  }

  /// Removes the approved face with the given [id]. Routed through the same
  /// error handling as every other call — it used to call `http.delete`
  /// directly, so unlike its siblings it threw on a network failure instead
  /// of reporting one.
  @override
  Future<void> removeFace(String id) async {
    _Result result;
    try {
      result = _decode(await http.delete(Uri.parse('$baseUrl/faces/$id'), headers: _headers));
    } catch (_) {
      result = const _Result.unreachable();
    }
    _report(result, 'removing the face');
  }

  /// Turns face-ID recognition on/off.
  @override
  Future<void> setFaceIdEnabled(bool enabled) async {
    _report(await _post('/face-id-enabled', {'enabled': enabled}), 'the Face ID toggle');
  }

  @override
  Future<void> setFaceDisarmEnabled(bool enabled) async {
    _report(await _post('/face-disarm-enabled', {'enabled': enabled}),
        'the Face ID disarm toggle');
  }

  /// Asks the backend's debug endpoint to simulate a face-recognition
  /// event (see [BackendService.simulateFaceRecognition]).
  @override
  Future<void> simulateFaceRecognition() async {
    _report(await _post('/debug/simulate-face-recognition'), 'the recognition test');
  }

  // ---- Settings -----------------------------------------------------------------
  // No dedicated GET endpoints for these — /state already carries them
  // (see python/backend/models.py FullState), so just read from there.

  /// Reads all four settings from a single `/state` response.
  ///
  /// This was four separate getters, and the settings screen asked for all
  /// four at once — so opening it fired four `GET /state` requests, each
  /// dragging back the whole event log (up to 200 entries) to extract one
  /// scalar. They were also the only `_post`/`_get` callers besides
  /// [logout] that skipped [_report], which is how an unreachable backend
  /// produced a fully-rendered settings screen made entirely of `??`
  /// defaults. Returning null instead is what lets the screen say so.
  @override
  Future<AppSettings?> getSettings() async {
    final res = _report(await _get('/state'), 'loading settings');
    if (!res.ok) return null;
    final body = res.body;
    return AppSettings(
      nightMode: body['night_mode'] as bool? ?? false,
      nightModeArmHour: body['night_mode_arm_hour'] as int? ?? 23,
      nightModeDisarmHour: body['night_mode_disarm_hour'] as int? ?? 7,
      emergencyNumber: body['emergency_number'] as String? ?? '',
      policeCallDelaySeconds: body['police_call_delay_seconds'] as int? ?? 60,
      ntfy: NtfyConfig(
        server: body['ntfy_server'] as String? ?? 'https://ntfy.sh',
        topic: body['ntfy_topic'] as String? ?? '',
      ),
    );
  }

  @override
  Future<void> setNightModeHours(int armHour, int disarmHour) async {
    _report(
      await _post('/settings/night-mode-hours', {
        'arm_hour': armHour,
        'disarm_hour': disarmHour,
      }),
      'the night-mode hours',
    );
  }

  @override
  Future<void> setNightMode(bool enabled) async {
    _report(await _post('/settings/night-mode', {'enabled': enabled}), 'the night-mode change');
  }

  @override
  Future<void> setEmergencyNumber(String number) async {
    _report(await _post('/settings/emergency-number', {'number': number}),
        'the emergency number change');
  }

  @override
  Future<void> setPoliceCallDelaySeconds(int seconds) async {
    _report(await _post('/settings/police-call-delay', {'seconds': seconds}),
        'the response-delay change');
  }

  @override
  Future<void> setNtfyConfig(String server, String topic) async {
    _report(await _post('/settings/ntfy', {'server': server, 'topic': topic}),
        'the notification settings');
  }

  @override
  Future<bool> sendTestNotification() async {
    final res = _report(await _post('/settings/ntfy/test'), 'the test notification');
    return res.body['ok'] as bool? ?? false;
  }

  /// Tears down the WebSocket connection and closes every stream
  /// controller. Sets [_disposed] first, before cancelling anything —
  /// [_connect]/[_scheduleReconnect] both check that flag, so this
  /// ordering guarantees a reconnect already in flight (e.g. a timer
  /// that fires in the same event-loop turn as this call) won't
  /// resurrect the connection right after we tear it down.
  @override
  void dispose() {
    _disposed = true;
    _disconnect();
    _alarmStateController.close();
    _eventLogController.close();
    _sensorController.close();
    _connectivityController.close();
    _linkController.close();
    _boardsController.close();
    _facesController.close();
    _cameraOnlineController.close();
    _faceIdEnabledController.close();
    _faceDisarmEnabledController.close();
    _entryDelayController.close();
    _errorController.close();
  }
}
