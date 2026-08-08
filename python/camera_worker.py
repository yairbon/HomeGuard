"""Camera worker: grabs frames from a webcam and feeds them to the backend's
face recognition.

Why this is a separate process
------------------------------
Without it, face recognition in this project was a dead end: the recognizer
worked and was tested, but nothing ever called `/camera/recognize`, so no face
was ever actually recognized in normal operation. The app's camera tab showed
a static placeholder.

It's a separate process rather than a thread inside the backend for two
reasons. Grabbing frames is a blocking OpenCV call, and the backend is a
single-threaded event loop serving every WebSocket — a blocking read there
would stall the whole alarm UI. And the camera is often not on the same
machine as the backend, so it needs to be movable.

Usage
-----
    HOMEGUARD_DEVICE_KEY=<the backend's device key> \\
        python camera_worker.py [--url http://localhost:8000] [--camera 0]

Authenticates with the same shared device key the ESP32 boards use — it's a
headless machine client, not a person, so it can't do an interactive login.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import cv2
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger("camera_worker")

# How often a frame is sent. Face recognition is the expensive part, and a
# person walking to a door is in view for several seconds — sending every
# frame would burn CPU on near-identical images for no extra detection
# ability.
DEFAULT_INTERVAL = 1.5

# JPEG rather than PNG: roughly a tenth the size at quality the recognizer
# can't tell apart, which matters when this runs over WiFi or a VPN.
JPEG_QUALITY = 80


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HomeGuard camera worker")
    parser.add_argument(
        "--url",
        default=os.environ.get("HOMEGUARD_BASE_URL", "http://localhost:8000"),
        help="Backend base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=int(os.environ.get("HOMEGUARD_CAMERA_INDEX", "0")),
        help="OpenCV camera index (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("HOMEGUARD_CAMERA_INTERVAL", DEFAULT_INTERVAL)),
        help="Seconds between frames (default: %(default)s)",
    )
    return parser.parse_args()


def open_camera(index: int) -> cv2.VideoCapture:
    """Opens the camera, exiting with a clear message if it isn't there.

    A missing camera is by far the most common setup problem (wrong index,
    unplugged, or held open by another program), so it gets an explicit error
    rather than an endless stream of failed reads.
    """
    capture = cv2.VideoCapture(index)
    if not capture.isOpened():
        sys.exit(
            f"Could not open camera {index}. Check it's plugged in and not in use "
            f"by another program, or pass a different --camera index."
        )
    return capture


def main() -> None:
    args = parse_args()
    device_key = os.environ.get("HOMEGUARD_DEVICE_KEY", "").strip()
    if not device_key:
        sys.exit(
            "HOMEGUARD_DEVICE_KEY is not set. Use the same key the backend logs at "
            "startup (the one in the boards' secrets.h) — /camera/recognize requires it."
        )

    capture = open_camera(args.camera)
    # trust_env=False so a proxy configured in the environment doesn't swallow
    # traffic aimed at a backend on the local network.
    client = httpx.Client(
        base_url=args.url,
        timeout=10.0,
        trust_env=False,
        headers={"X-Device-Key": device_key},
    )

    logger.info("Streaming camera %d to %s every %.1fs", args.camera, args.url, args.interval)
    # Only transitions are logged, not every frame — otherwise one person
    # standing in view produces a log line every interval forever.
    last_reported: str | None = None

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                logger.warning("Dropped frame from the camera — retrying")
                time.sleep(args.interval)
                continue

            encoded, buffer = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )
            if not encoded:
                logger.warning("Could not JPEG-encode a frame — skipping")
                time.sleep(args.interval)
                continue

            try:
                response = client.post(
                    "/camera/recognize",
                    files={"photo": ("frame.jpg", buffer.tobytes(), "image/jpeg")},
                )
                if response.status_code == 401:
                    sys.exit("Rejected (401) — HOMEGUARD_DEVICE_KEY doesn't match the backend's.")
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPError as exc:
                # The backend being briefly unreachable is normal (restart,
                # flaky WiFi). Keep going rather than dying — the camera
                # worker should recover on its own.
                logger.warning("Backend unreachable: %s", exc)
                time.sleep(args.interval)
                continue

            name = body.get("name") if body.get("matched") else None
            if name != last_reported:
                logger.info("Recognized: %s", name if name else "(nobody)")
                last_reported = name

            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Stopping.")
    finally:
        capture.release()
        client.close()


if __name__ == "__main__":
    main()
