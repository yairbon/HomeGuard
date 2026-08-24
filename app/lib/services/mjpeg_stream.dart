import 'dart:async';
import 'dart:convert';
import 'dart:ui' as ui;

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

/// Parses a raw byte stream in `multipart/x-mixed-replace` format (what an
/// MJPEG-over-HTTP camera stream looks like on the wire — a sequence of
/// `--<boundary>\r\nContent-Type: ...\r\nContent-Length: N\r\n\r\n<N bytes>`
/// parts, repeated forever) into complete JPEG frames.
///
/// Fed chunks as they arrive over the network — HTTP chunking has no
/// relationship to where frame boundaries fall, so a single chunk can
/// contain a partial frame, exactly one frame, or several. [addChunk]
/// returns however many complete frames that chunk completed, leaving any
/// trailing partial frame buffered for the next call.
class MjpegFrameParser {
  MjpegFrameParser(String boundaryToken) : _boundary = utf8.encode('--$boundaryToken');

  final List<int> _boundary;
  final List<int> _buffer = <int>[];

  /// Bounds the buffer so a stream that never produces a valid boundary
  /// (misconfigured upstream, garbage bytes) can't grow this unboundedly —
  /// this is a live security-camera feed, not a place to silently leak
  /// memory over a long-running app session.
  static const _maxBufferBytes = 8 * 1024 * 1024;

  List<Uint8List> addChunk(List<int> chunk) {
    _buffer.addAll(chunk);
    final frames = <Uint8List>[];

    while (true) {
      final boundaryStart = _indexOf(_buffer, _boundary, 0);
      if (boundaryStart == -1) {
        if (_buffer.length > _maxBufferBytes) _buffer.clear();
        break;
      }

      final headerStart = boundaryStart + _boundary.length;
      final headerEnd = _indexOfCrlfCrlf(_buffer, headerStart);
      if (headerEnd == -1) break; // headers haven't fully arrived yet

      final headerText = ascii.decode(_buffer.sublist(headerStart, headerEnd), allowInvalid: true);
      final contentLength = _parseContentLength(headerText);
      final bodyStart = headerEnd + 4; // past \r\n\r\n

      if (contentLength == null) {
        // No Content-Length header — the ESP32-CAM/backend always send one
        // (see esp32/esp32_cam/esp32_cam.ino's streamHandler), so this
        // shouldn't happen in practice. Drop the malformed part rather than
        // silently misinterpreting the stream.
        _buffer.removeRange(0, bodyStart);
        continue;
      }

      final bodyEnd = bodyStart + contentLength;
      if (_buffer.length < bodyEnd) break; // frame not fully arrived yet

      frames.add(Uint8List.fromList(_buffer.sublist(bodyStart, bodyEnd)));
      _buffer.removeRange(0, bodyEnd);
    }
    return frames;
  }

  int? _parseContentLength(String headerText) {
    for (final line in headerText.split('\r\n')) {
      final idx = line.indexOf(':');
      if (idx == -1) continue;
      if (line.substring(0, idx).trim().toLowerCase() == 'content-length') {
        return int.tryParse(line.substring(idx + 1).trim());
      }
    }
    return null;
  }

  static int _indexOf(List<int> haystack, List<int> needle, int start) {
    if (needle.isEmpty) return -1;
    final limit = haystack.length - needle.length;
    outer:
    for (var i = start; i <= limit; i++) {
      for (var j = 0; j < needle.length; j++) {
        if (haystack[i + j] != needle[j]) continue outer;
      }
      return i;
    }
    return -1;
  }

  static int _indexOfCrlfCrlf(List<int> buffer, int start) {
    for (var i = start; i <= buffer.length - 4; i++) {
      if (buffer[i] == 0x0D && buffer[i + 1] == 0x0A && buffer[i + 2] == 0x0D && buffer[i + 3] == 0x0A) {
        return i;
      }
    }
    return -1;
  }
}

/// Owns one persistent connection to an MJPEG stream URL, parses it into
/// frames via [MjpegFrameParser], and reconnects automatically on any
/// drop (connection refused, stream ended, network error) — the camera
/// screen just listens to [frames] and doesn't need to know a reconnect
/// ever happened.
class MjpegStreamClient {
  MjpegStreamClient({required this.streamUrl, required this.headers});

  final String streamUrl;
  final Map<String, String> headers;

  http.Client? _client;
  StreamSubscription<List<int>>? _sub;
  Timer? _reconnectTimer;
  final _frames = StreamController<Uint8List>.broadcast();
  bool _disposed = false;

  Stream<Uint8List> get frames => _frames.stream;

  Future<void> connect() async {
    if (_disposed) return;
    _client = http.Client();
    try {
      final request = http.Request('GET', Uri.parse(streamUrl))..headers.addAll(headers);
      final response = await _client!.send(request);

      if (response.statusCode != 200) {
        throw http.ClientException('MJPEG stream returned ${response.statusCode}');
      }
      final contentType = response.headers['content-type'] ?? '';
      final match = RegExp(r'boundary=("([^"]+)"|([^;]+))').firstMatch(contentType);
      if (match == null) {
        throw http.ClientException('No boundary in Content-Type: $contentType');
      }
      final boundaryToken = match.group(2) ?? match.group(3)!;
      final parser = MjpegFrameParser(boundaryToken);

      _sub = response.stream.listen(
        (chunk) {
          for (final frame in parser.addChunk(chunk)) {
            if (!_frames.isClosed) _frames.add(frame);
          }
        },
        onError: (Object _) => _scheduleReconnect(),
        onDone: _scheduleReconnect,
        cancelOnError: true,
      );
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    _client?.close();
    if (_disposed) return;
    // Mirrors HttpBackendService's existing WebSocket reconnect interval
    // (same 3s cadence, same reasons: a network glitch, the adb-reverse USB
    // tunnel dropping, a Tailscale hiccup — see CLAUDE.md's remote-access
    // section for why those are the real, observed failure modes here).
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 3), connect);
  }

  void dispose() {
    _disposed = true;
    _reconnectTimer?.cancel();
    _sub?.cancel();
    _client?.close();
    _frames.close();
  }
}

/// Turns the JPEG frames coming out of [MjpegStreamClient] into decoded
/// [ui.Image]s, exposed as a [ValueListenable] the camera feed can paint
/// with [RawImage].
///
/// This exists because the obvious way to show these frames — handing each
/// `Uint8List` to `Image.memory` — is a memory trap at video rates.
/// `MemoryImage`'s cache key is the byte list itself, `Uint8List` has no value
/// equality, and [MjpegFrameParser] necessarily produces a fresh list per
/// frame; so every single frame became a *distinct* entry in the global
/// `ImageCache`, which filled to its 100 MiB default in a few seconds of
/// watching and then thrashed, evicting and re-decoding for the rest of the
/// session. None of those entries could ever be hit again either — the same
/// frame never arrives twice.
///
/// Decoding by hand skips the cache entirely, and makes the lifetime of each
/// decoded image explicit, which is the part `Image.memory` gave no way to
/// control.
class MjpegImageDecoder {
  /// The most recently decoded frame, or null before the first one arrives.
  /// Never goes back to null on a decode failure: the last good frame stays
  /// up, which is what a viewer wants from a live feed that hiccuped.
  final ValueNotifier<ui.Image?> image = ValueNotifier<ui.Image?>(null);

  /// Newest frame not yet decoded. Deliberately a single slot rather than a
  /// queue: decoding is slower than a 30fps producer on a slow phone, and a
  /// backlog of stale frames is worthless on a security camera — the point
  /// is what the doorway looks like *now*.
  Uint8List? _pending;
  bool _draining = false;
  bool _disposed = false;

  /// Offers [jpeg] as the next frame to display, replacing any frame still
  /// waiting to be decoded.
  void submit(Uint8List jpeg) {
    if (_disposed) return;
    _pending = jpeg;
    if (!_draining) unawaited(_drain());
  }

  Future<void> _drain() async {
    _draining = true;
    try {
      while (!_disposed) {
        final bytes = _pending;
        if (bytes == null) break;
        _pending = null;

        final ui.Image decoded;
        try {
          // instantiateImageCodecFromBuffer takes ownership of the buffer and
          // disposes it itself, including on failure — so there is
          // deliberately no buffer.dispose() here.
          final codec = await ui.instantiateImageCodecFromBuffer(
            await ui.ImmutableBuffer.fromUint8List(bytes),
          );
          try {
            decoded = (await codec.getNextFrame()).image;
          } finally {
            codec.dispose();
          }
        } catch (_) {
          // A truncated or corrupt frame (a chunk lost mid-stream, say).
          // Keep showing the last good one and move on to the next.
          continue;
        }

        if (_disposed) {
          decoded.dispose();
          return;
        }

        final previous = image.value;
        // Publish first, dispose second, and never in the same synchronous
        // step: the RenderImage still holds `previous` until the rebuild this
        // assignment schedules actually runs, and painting an image that has
        // already been disposed throws. The microtask lands after this turn
        // and before the next frame is built, which is exactly the window
        // where nothing can be painting either image.
        image.value = decoded;
        if (previous != null) scheduleMicrotask(previous.dispose);
      }
    } finally {
      _draining = false;
    }
  }

  /// Releases the currently displayed frame and the notifier. Any decode
  /// still in flight discards its result rather than publishing to a
  /// disposed notifier.
  void dispose() {
    _disposed = true;
    _pending = null;
    image.value?.dispose();
    image.dispose();
  }
}
