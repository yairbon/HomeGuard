import 'dart:typed_data';

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

/// Away arms every sensor. Home arms only the door sensor: it bypasses both
/// perimeter sensors (ultrasonic and vibration), so people can move around inside.
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

  /// False when the login request never reached the backend at all.
  ///
  /// Without this axis a failed *request* was indistinguishable from a
  /// rejected *password*: the unreachable case came back as
  /// `LoginResult(ok: false)`, [attemptsRemaining] defaulted to 0, and the
  /// login screen told the user "Wrong password (0 attempt(s) left)" — an
  /// accusation about their password and a fabricated lockout warning, when
  /// the actual problem was a backend URL that nothing was listening on.
  /// Same reason [ChangePasswordResult] carries one.
  final bool reachable;

  const LoginResult({
    required this.ok,
    this.lockedOut = false,
    this.lockoutRemainingSeconds = 0,
    this.attemptsRemaining = 0,
    this.reachable = true,
  });
}

/// Outcome of changing the shared password/keypad-PIN credential.
///
/// Three distinguishable results, because they need three different things
/// said to the user:
///
/// * [ok] false — the new value was rejected (not 4-8 digits), the current
///   value didn't match, or the backend couldn't be reached. Nothing
///   changed anywhere.
/// * [ok] true, [hardware] false — saved here (this app can already log in
///   with the new value), but the door controller didn't answer. The
///   keypad is **still accepting the old code**; this is the case the user
///   most needs told plainly.
/// * [ok] true, [hardware] true — the board stored it in NVS and is using
///   it now, so both the app and the physical keypad are on the new code.
///
/// [secretsUpdated] is a separate axis and only relevant on a development
/// machine: it says whether `esp32/.../secrets.h` was rewritten to match, so
/// that reflashing the board later doesn't silently restore an old code. It
/// is false on any normal install, where the backend has no source tree to
/// write to, and that is not a failure.
class ChangePasswordResult {
  final bool ok;
  final bool hardware;
  final bool secretsUpdated;
  final bool reachable;

  const ChangePasswordResult({
    required this.ok,
    this.hardware = false,
    this.secretsUpdated = false,
    this.reachable = true,
  });
}

/// Outcome of enrolling a face — creating the record *and* giving it a photo
/// to actually recognise with.
///
/// Split out because the two halves fail differently and the user needs to
/// know which happened. Previously the app only ever did the first half:
/// `POST /faces` creates a record with no training photos, and nothing in
/// `app/` ever called `/faces/{id}/photo`. So every face added from the phone
/// appeared in the list under a caption promising it would be "recognized as
/// a resident", and could never match anything at all.
enum EnrollFaceOutcome {
  /// Record created and a photo attached — the face can now be recognised.
  enrolled,

  /// No camera frame was available. The record is deliberately not created,
  /// so there is no inert leftover to clean up.
  noFrame,

  /// A frame arrived but the backend found no face in it. The record *was*
  /// created; the user should retry the photo rather than re-adding the name.
  noFaceInFrame,

  /// The backend couldn't be reached, or refused the request.
  failed,
}

/// What the Settings screen's test-alert button gets back: whether every
/// configured channel delivered, and a sentence to show as is (it never holds
/// a password or key).
class AlertTestResult {
  const AlertTestResult({required this.ok, required this.summary});

  final bool ok;
  final String summary;
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

/// Every backend-stored setting the settings screen edits, read in one go.
///
/// This used to be four independent getters ([BackendService.getSettings]'s
/// predecessors), each of which fetched the whole of `/state` — four
/// requests, each carrying the entire event log, to read four scalars. Worse,
/// each swallowed transport failures and substituted its own default, so an
/// unreachable backend rendered as a *loaded* settings screen showing a blank
/// emergency number and a blank ntfy topic. One tap on save then wrote those
/// blanks back, silently switching off the only alert channel that works with
/// the app closed. Returning this as a single nullable value makes "we never
/// heard back" impossible to mistake for "the answers are all empty".
class AppSettings {
  const AppSettings({
    required this.nightMode,
    required this.nightModeArmHour,
    required this.nightModeDisarmHour,
    required this.alertsEnabled,
    this.clipEnabled = true,
    this.clipSeconds = 60,
    required this.alertEmails,
    required this.escalationEmails,
    required this.emergencyNumber,
    required this.policeCallDelaySeconds,
    required this.ntfy,
  });

  /// Whether night mode (auto-arm overnight) is enabled.
  final bool nightMode;

  /// Whole hours (0-23) at which night mode arms and disarms.
  ///
  /// The backend has carried these in every state frame since night mode
  /// gained a real scheduler, and shipped a `/settings/night-mode-hours`
  /// route to change them — but nothing in the app read either, so the window
  /// was frozen at the server defaults of 23:00-07:00 and the toggle said
  /// only "automatically arm overnight" without ever naming a time. A shift
  /// worker enabling it got the house armed around them at 23:00 with no way
  /// to move it.
  final int nightModeArmHour;
  final int nightModeDisarmHour;

  /// Master switch for messages that leave the house (email, WhatsApp). Off
  /// means an alarm still sounds and shows in the app, it just sends no
  /// message — for testing without flooding an inbox.
  final bool alertsEnabled;

  /// Whether the breach email carries a video of the last [clipSeconds] seconds
  /// from the camera (5-60).
  final bool clipEnabled;
  final int clipSeconds;

  /// Who is emailed when an alarm triggers, and who is added if it is still not
  /// cleared after the delay (the emergency contacts). The sending account and
  /// its password live on the laptop, never here.
  final List<String> alertEmails;
  final List<String> escalationEmails;

  /// The configured emergency contact number (empty string if unset).
  final String emergencyNumber;

  /// How long after a triggered alert the backend escalates if nobody has
  /// cleared it.
  final int policeCallDelaySeconds;

  /// Where the backend pushes alerts so they arrive with this app closed.
  final NtfyConfig ntfy;
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

  /// Changes the shared password/keypad-PIN credential from [oldPassword]
  /// to [newPassword] — both this app's own login *and* the code typed on
  /// the physical keypad to disarm, in one call. The new value travels:
  /// backend -> hashed for login -> door controller -> the board's NVS,
  /// which is the only copy the keypad actually compares against. See
  /// [ChangePasswordResult] for why the outcome isn't a plain bool.
  ///
  /// Must be 4-8 digits — the keypad's other two keys are `*` (arm) and `#`
  /// (submit), so anything else produces a code that can't be typed there.
  /// The backend and the firmware both enforce that independently; the app
  /// checks too, only so the user gets told before a round trip.
  Future<ChangePasswordResult> changePassword(String oldPassword, String newPassword);

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
  ///
  /// Note what this is *not*: it is the backend's opinion of the boards, so
  /// it can only ever arrive over a working connection. See
  /// [backendLinkStream] for whether this app can hear the backend at all.
  Stream<bool> get connectivityStream;

  /// Emits whether this app currently has a live link to the backend — i.e.
  /// whether anything on the dashboard is still being updated.
  ///
  /// Deliberately separate from [connectivityStream]: that one relays the
  /// backend's `esp32_online` ("the boards are talking to the backend"),
  /// which says nothing about whether *we* are talking to the backend.
  /// Nothing used to report that, so when the WebSocket dropped, the
  /// dashboard froze on its last values — sensors OK, system disarmed —
  /// behind a green ONLINE dot, indefinitely and with no reconnect visible
  /// to the user. On an alarm panel a frozen readout that still claims to be
  /// live is the worst of both worlds: it looks exactly like a quiet house.
  ///
  /// Implementations emit false as soon as the link drops (and while
  /// reconnecting) and true again on the first frame received after a
  /// successful connect — a frame arriving is the only real proof the link
  /// works end to end.
  Stream<bool> get backendLinkStream;

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
  /// Registers a new approved face under [name], returning its backend id —
  /// or null if the call failed.
  ///
  /// The id matters because a face record on its own is inert: recognition
  /// matches against training photos, and a record with none can never match
  /// anything. Prefer [AlarmService.enrollFace] over calling this directly,
  /// unless you genuinely want a nameplate with no face behind it.
  Future<String?> addFace(String name);

  /// Attaches a training photo to an existing approved face. Returns whether
  /// the backend accepted it — it rejects a frame with no detectable face
  /// (422), which is a normal outcome worth showing the user, not an error.
  Future<bool> addFacePhoto(String id, Uint8List jpeg);

  /// The most recent frame any camera uploaded, or null if none has arrived
  /// or the request failed.
  ///
  /// Read from the backend's cache rather than the live MJPEG stream, so
  /// enrolling a face doesn't require the camera screen to be open.
  Future<Uint8List?> captureCameraFrame();

  /// Removes the approved face with the given [id].
  Future<void> removeFace(String id);

  // Camera / face-ID module
  /// Emits whether the camera module is currently reachable — tracked
  /// separately from [connectivityStream] since the camera and the
  /// sensor ESP32 boards are independent pieces of hardware that can go
  /// offline independently of each other.
  Stream<bool> get cameraOnlineStream;

  /// Headers a request to [cameraStreamUrl] needs to authenticate.
  Map<String, String> get cameraFrameHeaders;

  /// The backend's `/camera/stream` URL — a continuous MJPEG multipart
  /// stream proxied from the ESP32-CAM's own /stream endpoint — or null when
  /// there's no way to authenticate a request right now (not logged in yet)
  /// or no real backend to ask at all ([MockBackendService]).
  ///
  /// This replaced a `cameraFrameUrl` pointing at `/camera/latest`, which
  /// the screen polled a couple of times a second. That getter outlived its
  /// last call site and was carried on all three of these classes for
  /// nothing; it's gone now. [cameraFrameHeaders] kept its name because it
  /// is still what authenticates a request to this URL.
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

  // Settings — loaded once by SettingsScreen, no need to stream these
  /// Reads every backend-stored setting in a single request, or null if the
  /// backend couldn't be reached (see [AppSettings] for why that distinction
  /// is load-bearing rather than a nicety).
  Future<AppSettings?> getSettings();

  /// Enables/disables night mode.
  Future<void> setNightMode(bool enabled);

  /// Switches outgoing alert messages on or off (see [AppSettings.alertsEnabled]).
  Future<void> setAlertsEnabled(bool enabled);

  /// Turns the video clip in the breach email on or off and sets its length
  /// (see [AppSettings.clipSeconds]).
  Future<void> setAlertClip(bool enabled, int seconds);

  /// Sets who is emailed (see [AppSettings.alertEmails]). Returns null when
  /// saved, or a sentence to show the user when it was refused (for instance a
  /// malformed address) or could not be sent.
  Future<String?> setAlertEmails(List<String> alertTo, List<String> escalationTo);

  /// Sets the hours night mode arms and disarms at. Both are whole hours in
  /// 0-23; the backend clamps rather than rejecting.
  Future<void> setNightModeHours(int armHour, int disarmHour);

  /// Sets the emergency contact [number].
  Future<void> setEmergencyNumber(String number);

  /// Sets the police-call delay, in [seconds].
  Future<void> setPoliceCallDelaySeconds(int seconds);

  /// Sets the push target — the ntfy server URL and topic name (see
  /// `python/backend/notifier.py`). An empty [topic] disables pushing.
  ///
  /// Pushing exists because the app's own notifications only fire while its
  /// process is alive: swipe it away and a break-in produces nothing.
  Future<void> setNtfyConfig(String server, String topic);

  /// Sends a test alert over every channel set up on the backend (email,
  /// WhatsApp, ntfy) and reports what happened, so the user can confirm
  /// alerts really arrive before relying on them. See [AlertTestResult].
  Future<AlertTestResult> sendTestAlert();

  /// Releases any resources (stream controllers, sockets, timers) held by
  /// the implementation. Must be called when the service is no longer
  /// needed to avoid leaking open StreamControllers/sockets.
  void dispose();
}
