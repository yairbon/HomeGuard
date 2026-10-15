import 'dart:async';
import 'dart:ui' as ui;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../services/alarm_service.dart';
import '../services/backend_service.dart';
import '../services/mjpeg_stream.dart';
import '../theme/app_theme.dart';
import '../widgets/event_tile.dart';
import '../widgets/homeguard_icons.dart';

/// Camera tab: shows the front-entrance feed, current Face ID and
/// camera-online status, and the most recent face-identification events.
///
/// The "feed" is [_FeedPlaceholder], which holds a persistent connection to
/// `GET /camera/stream` (see [BackendService.cameraStreamUrl]) — a
/// multipart/x-mixed-replace MJPEG stream the backend proxies from the
/// ESP32-CAM's own /stream endpoint — parsed frame-by-frame via
/// [MjpegStreamClient]. An earlier version of this screen polled a single-
/// JPEG snapshot endpoint every couple hundred milliseconds instead; this
/// replaced that once there was an actual reason to want more than ~2fps.
/// Falls back to a static icon when there's no camera source to connect to
/// (offline, or [MockBackendService], which has no real backend at all).
class CameraScreen extends StatelessWidget {
  const CameraScreen({super.key});

  /// Builds the camera screen: feed placeholder, status cards, and the
  /// recent-identifications list.
  ///
  /// Only the 5 most recent face-ID events are shown here (see
  /// [recentIds]) so this tab stays a quick-glance summary; the full
  /// history is available in the event log screen.
  @override
  Widget build(BuildContext context) {
    final alarm = context.watch<AlarmService>();
    final recentIds = alarm.faceIdEvents.take(5).toList();

    return Scaffold(
      appBar: AppBar(title: const Text('CAMERA')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _FeedPlaceholder(isOnline: alarm.isCameraOnline),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: _StatusCard(
                  icon: HGIcon.faces,
                  label: 'FACE ID',
                  value: alarm.faceIdEnabled ? 'ENABLED' : 'DISABLED',
                  color: alarm.faceIdEnabled ? AppColors.safe : AppColors.textMuted,
                  // Face ID is a user-controlled setting, so this card is
                  // tappable and toggles it directly.
                  onTap: () => context.read<AlarmService>().setFaceIdEnabled(!alarm.faceIdEnabled),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _StatusCard(
                  icon: HGIcon.camera,
                  label: 'CAMERA',
                  value: alarm.isCameraOnline ? 'STREAMING' : 'OFFLINE',
                  color: alarm.isCameraOnline ? AppColors.safe : AppColors.alarm,
                  // No onTap: unlike Face ID, camera online/offline reflects
                  // actual hardware connectivity and isn't user-toggleable.
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          _FaceDisarmToggle(
            enabled: alarm.faceDisarmEnabled,
            onChanged: (value) => context.read<AlarmService>().setFaceDisarmEnabled(value),
          ),
          const SizedBox(height: 24),
          Text(
            'RECENT IDENTIFICATIONS',
            style: AppTheme.displayEyebrow.copyWith(color: AppColors.textMuted),
          ),
          const SizedBox(height: 8),
          if (recentIds.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 16),
              child: Text('No identifications yet', style: TextStyle(color: AppColors.textMuted)),
            )
          else
            ...recentIds.map((e) => EventTileCompact(event: e)),
        ],
      ),
    );
  }
}

/// Opt-in switch for letting a recognized face disarm the system.
///
/// Given a full card with an explicit warning rather than a plain row,
/// because this is the one setting in the app that *lowers* what it takes to
/// turn the alarm off. The recognizer compares face embeddings; the backend
/// adds two liveness checks (landmark motion and an anti-spoofing model), but
/// a good replay on a screen may still get through — that makes this
/// convenience, not security, and the UI has to say so rather than let
/// someone enable it assuming otherwise.
class _FaceDisarmToggle extends StatelessWidget {
  const _FaceDisarmToggle({required this.enabled, required this.onChanged});

  final bool enabled;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: AppColors.surfaceRaised,
      borderRadius: BorderRadius.circular(10),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 6, 14, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              value: enabled,
              activeThumbColor: AppColors.system,
              onChanged: onChanged,
              title: const Text(
                'Disarm with Face ID',
                style: TextStyle(color: AppColors.textPrimary, fontSize: 14),
              ),
              subtitle: Text(
                'A recognized face turns the alarm off',
                style: TextStyle(color: AppColors.textMuted, fontSize: 12),
              ),
            ),
            Text(
              'Includes a liveness check (face motion and an anti-spoofing '
              'model), but treat this as convenience, not security.',
              style: TextStyle(color: AppColors.transitional, fontSize: 11),
            ),
          ],
        ),
      ),
    );
  }
}

/// The camera feed box: a bordered container showing the live MJPEG stream
/// (or a camera icon when there's nothing to show), a LIVE/OFFLINE status
/// badge, and a caption naming the camera and its connection state.
class _FeedPlaceholder extends StatefulWidget {
  const _FeedPlaceholder({required this.isOnline});

  final bool isOnline;

  @override
  State<_FeedPlaceholder> createState() => _FeedPlaceholderState();
}

class _FeedPlaceholderState extends State<_FeedPlaceholder> with WidgetsBindingObserver {
  MjpegStreamClient? _stream;
  StreamSubscription<Uint8List>? _frameSub;

  // Decoded frames live in a ValueNotifier rather than State so a new frame
  // repaints only the image itself, not the badge/caption/border around it —
  // setState here would rebuild the whole card 30 times a second.
  final _decoder = MjpegImageDecoder();

  /// Retries [_start] while the tab is visible but there is still no stream
  /// URL to connect to. Without it, a feed that found `cameraStreamUrl` null
  /// once stayed dead for the rest of the session.
  Timer? _retryTimer;

  /// Visibility of the tab this widget sits in, from the [TickerMode] that
  /// `MainShell` wraps each [IndexedStack] child in.
  ValueListenable<TickerModeData>? _tabVisibility;

  bool _appResumed = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // getValuesNotifier deliberately establishes no dependency, so this is
    // safe in initState and won't re-fire didChangeDependencies. This widget
    // never moves in the tree, so subscribing once here is enough.
    _tabVisibility = TickerMode.getValuesNotifier(context)..addListener(_syncStreaming);
    _syncStreaming();
  }

  /// The stream is only worth having open while someone can actually see it.
  ///
  /// `MainShell`'s IndexedStack mounts all five tabs at login, so this used to
  /// connect the moment the user signed in and keep decoding video for the
  /// whole session on tabs where not a single frame was ever painted. Over
  /// Tailscale on cellular that is a continuous video download nobody
  /// watches. Backgrounding the app had the same problem.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _appResumed = state == AppLifecycleState.resumed;
    _syncStreaming();
  }

  bool get _shouldStream => _appResumed && (_tabVisibility?.value.enabled ?? true);

  void _syncStreaming() {
    if (_shouldStream) {
      _start();
    } else {
      _stop();
    }
  }

  void _start() {
    if (!mounted || _stream != null) return;
    final backend = context.read<BackendService>();
    final url = backend.cameraStreamUrl;
    if (url == null) {
      // Null means "no way to authenticate a request yet" — e.g. the token
      // hasn't landed. Poll for it rather than giving up; same 3s cadence as
      // every other reconnect in the app.
      _retryTimer ??= Timer.periodic(const Duration(seconds: 3), (_) {
        if (_shouldStream) _start();
      });
      return;
    }
    _retryTimer?.cancel();
    _retryTimer = null;
    final stream = MjpegStreamClient(
      streamUrl: url,
      headers: backend.cameraFrameHeaders,
      // The still-image fallback (see MjpegStreamClient.snapshotUrl).
      snapshotUrl: url.replaceFirst('/camera/stream', '/camera/latest'),
    );
    _stream = stream;
    _frameSub = stream.frames.listen(_decoder.submit);
    stream.connect();
  }

  /// Closes the connection but keeps the last decoded frame, so returning to
  /// the tab shows the last thing the camera saw instead of a black box while
  /// the first new frame arrives.
  void _stop() {
    _retryTimer?.cancel();
    _retryTimer = null;
    _frameSub?.cancel();
    _frameSub = null;
    _stream?.dispose();
    _stream = null;
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _tabVisibility?.removeListener(_syncStreaming);
    _stop();
    _decoder.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AspectRatio(
      aspectRatio: 4 / 3,
      child: Container(
        decoration: BoxDecoration(
          color: AppColors.surface,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: AppColors.surfaceRaised),
        ),
        child: ClipRRect(
          borderRadius: BorderRadius.circular(12),
          child: Stack(
            fit: StackFit.expand,
            children: [
              RepaintBoundary(
                child: ValueListenableBuilder<ui.Image?>(
                  valueListenable: _decoder.image,
                  builder: (context, frame, _) {
                    if (frame == null) return _cameraIcon();
                    // RawImage, not Image.memory: the frames are already
                    // decoded (see MjpegImageDecoder for why they have to
                    // be), and this notifier only ever holds a fully decoded
                    // one, so there is no gap to paper over with
                    // gaplessPlayback either.
                    return RawImage(image: frame, fit: BoxFit.cover);
                  },
                ),
              ),
              Positioned(
                top: 10,
                right: 10,
                child: _Badge(
                  text: widget.isOnline ? 'LIVE' : 'OFFLINE',
                  color: widget.isOnline ? AppColors.alarm : AppColors.textMuted,
                  dot: widget.isOnline,
                ),
              ),
              Positioned(
                bottom: 10,
                left: 10,
                right: 10,
                child: Text(
                  widget.isOnline
                      ? 'ESP32-CAM · front entrance'
                      : 'ESP32-CAM · waiting for connection',
                  style: AppTheme.monoDetail.copyWith(color: AppColors.textMuted),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _cameraIcon() {
    return Center(
      child: HomeGuardIcon(HGIcon.camera, size: 48, color: AppColors.textMuted.withValues(alpha: 0.4)),
    );
  }
}

/// Small pill-shaped badge (e.g. "LIVE" / "OFFLINE") overlaid on the feed
/// placeholder, with an optional colored dot when [dot] is true.
class _Badge extends StatelessWidget {
  const _Badge({required this.text, required this.color, this.dot = false});

  final String text;
  final Color color;
  final bool dot;

  /// Renders [text] in [color], preceded by a colored dot when [dot] is
  /// true.
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: AppColors.background.withValues(alpha: 0.8),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (dot) ...[
            Container(
              width: 6,
              height: 6,
              decoration: BoxDecoration(shape: BoxShape.circle, color: color),
            ),
            const SizedBox(width: 5),
          ],
          Text(text, style: AppTheme.monoBadge.copyWith(color: color)),
        ],
      ),
    );
  }
}

/// Reusable status tile showing an icon, a label (e.g. "FACE ID"), and a
/// value (e.g. "ENABLED") in [color]. [onTap] is optional because not
/// every status shown via this widget is user-toggleable (compare the
/// Face ID and Camera cards in [CameraScreen]).
class _StatusCard extends StatelessWidget {
  const _StatusCard({
    required this.icon,
    required this.label,
    required this.value,
    required this.color,
    this.onTap,
  });

  final HGIcon icon;
  final String label;
  final String value;
  final Color color;
  final VoidCallback? onTap;

  /// Renders the icon/label/value row; tappable only if [onTap] is
  /// provided.
  @override
  Widget build(BuildContext context) {
    return Material(
      color: AppColors.surfaceRaised,
      borderRadius: BorderRadius.circular(10),
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Row(
            children: [
              HomeGuardIcon(icon, color: color, size: 20),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(label, style: TextStyle(fontSize: 11, color: AppColors.textMuted)),
                    Text(
                      value,
                      style: AppTheme.monoValue.copyWith(color: color),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
