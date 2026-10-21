"""Turns the camera's recent frames into a short video for an alarm email.

The backend watches the ESP32-CAM's live stream all the time (see
`_record_camera` in main.py) and keeps the last minute of frames
(AppState.record_clip_frame). When an alarm goes out, this module makes them
into an MP4. If the stream is down, the stills the camera pushes for face
recognition (about 1-2 a second) are kept instead, so there is still something.

The frames arrive unevenly, so they are laid onto a fixed timeline (each tick
shows the newest frame at or before it). That way the clip plays at real speed:
a minute of footage is a minute-long video, however many frames arrived.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

import cv2
import numpy as np

try:  # ships its own ffmpeg program; without it the OpenCV encoder below is used
    import imageio_ffmpeg
except ImportError:  # pragma: no cover
    imageio_ffmpeg = None

logger = logging.getLogger("clip")

CLIP_SECONDS = 60.0
CLIP_FPS = 20.0
# Hard cap on the frames kept (60 s at 30 frames/s), so a camera that suddenly
# sends far faster than usual cannot grow the buffer without bound.
MAX_FRAMES = 1800
# Gmail takes 25 MB per message and base64 adds about a third, so keep the video under ~10 MB.
MAX_BYTES = 10 * 1024 * 1024

# H.264 first: every phone plays it. OpenCV can write it through Windows Media
# Foundation (the backend's normal home); elsewhere it falls back to MPEG-4.
_WRITERS = (
    ("H264", cv2.CAP_MSMF),
    ("avc1", cv2.CAP_ANY),
    ("mp4v", cv2.CAP_ANY),
)


def _paced(frames: list[tuple[float, bytes]]) -> tuple[list[bytes], float]:
    """Every frame once, oldest first, and the steady frame rate that makes the
    clip last as long as the footage did (frames / real time span).

    The camera delivers in bursts with gaps (WiFi). Laying the frames on a fixed
    grid by arrival time repeated one frame across each gap and dropped frames
    inside each burst, which looked like stutter. Playing every frame exactly
    once at an even rate shows the motion smoothly; a long dropout is shortened
    rather than frozen."""
    frames = sorted(frames, key=lambda f: f[0])
    span = frames[-1][0] - frames[0][0]
    fps = (len(frames) - 1) / span if span > 0 else CLIP_FPS
    return [f[1] for f in frames], min(30.0, max(1.0, fps))


def _write(timeline: list[bytes], fps: float, w: int, h: int) -> bytes | None:
    """One encode of `timeline` at `fps`, trying each codec in turn."""
    for fourcc, api in _WRITERS:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            writer = cv2.VideoWriter(path, api, cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
            if not writer.isOpened():
                writer.release()
                continue
            wrote, last_jpeg, img = 0, None, None
            for jpeg in timeline:
                if jpeg is not last_jpeg:  # frames repeat in a row: decode each once
                    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None and img.shape[:2] != (h, w):
                        img = cv2.resize(img, (w, h))
                    last_jpeg = jpeg
                if img is not None:
                    writer.write(img)
                    wrote += 1
            writer.release()
            if wrote and os.path.getsize(path) > 1000:
                with open(path, "rb") as f:
                    return f.read()
        finally:
            if os.path.exists(path):
                os.remove(path)
    return None


def _ffmpeg(timeline: list[bytes], fps: float, crf: int) -> bytes | None:
    """H.264 (libx264, yuv420p, faststart) at `fps` through ffmpeg, which is
    fed the JPEGs as they are. None if ffmpeg is missing or fails."""
    if imageio_ffmpeg is None:
        return None
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
            "-f", "image2pipe", "-framerate", f"{fps:.3f}", "-c:v", "mjpeg", "-i", "-",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", path,
        ]  # fmt: skip
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)  # fmt: skip
        try:
            for jpeg in timeline:
                proc.stdin.write(jpeg)
            proc.stdin.close()
        except OSError:
            proc.kill()
            return None
        if proc.wait(timeout=120) != 0 or os.path.getsize(path) < 1000:
            return None
        with open(path, "rb") as f:
            return f.read()
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if os.path.exists(path):
            os.remove(path)


def encode_clip(
    frames: list[tuple[float, bytes]] | tuple[tuple[float, bytes], ...] | None,
) -> bytes | None:
    """MP4 bytes for `frames` ((unix time, jpeg) pairs), or None if there are too
    few frames or no encoder works. Blocking and CPU-bound: call it from a worker
    thread, never from the event loop. Never raises."""
    try:
        frames = list(frames or ())
        if len(frames) < 2:
            return None
        first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
        if first is None:
            return None
        h, w = first.shape[:2]
        w, h = w - w % 2, h - h % 2  # H.264 wants even sides
        timeline, fps = _paced(frames)
        for crf in (28, 34):  # preferred route: every frame once, through ffmpeg
            data = _ffmpeg(timeline, fps, crf)
            if data is None:
                break
            if len(data) <= MAX_BYTES:
                return data
            logger.warning(
                "The alarm clip is %d bytes at CRF %d; making a lighter one.", len(data), crf
            )
        # Fallback (no ffmpeg, or still too big): OpenCV's encoder, retried with
        # every 2nd, then every 4th frame if the file is too big for an email.
        for step in (1, 2, 4):
            data = _write(timeline[::step], fps / step, w, h)
            if data is None:
                return None
            if len(data) <= MAX_BYTES:
                return data
            logger.warning("The alarm clip is %d bytes; making a lighter one.", len(data))
        return None
    except Exception:  # an alarm email must never fail because the clip could not be made
        logger.warning("Could not make the alarm clip.", exc_info=True)
        return None
