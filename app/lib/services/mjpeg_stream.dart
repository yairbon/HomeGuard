import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

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
