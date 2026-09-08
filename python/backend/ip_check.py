"""Warns at startup when a board's secrets.h has a stale BACKEND_HOST.

This project's single most recurring hardware-bringup problem: the dev
machine's WiFi network reassigns its IP by DHCP, often between sessions, and
every ESP32 board's `secrets.h` has that IP compiled in as a literal. When it
drifts, a board doesn't error -- it just can't reach the backend, and shows
up in the app as "offline" indistinguishable from a board that lost power or
WiFi. Catching the mismatch here, once, at the moment the backend starts, is
cheaper than re-diagnosing it by hand every time (as happened repeatedly
before this existed).

Read-only and best-effort by design: this never raises and never blocks
startup. A missing secrets.h (a fresh checkout, or CI, where these
gitignored files were never created) is silently skipped rather than
reported as stale -- there is nothing wrong to warn about in that case.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("ip_check")

# Mirrors the board list in esp32/README.md. Each of these folders has its
# own secrets.h (gitignored; esp32/secrets.h.example is the committed
# template) with an identical `#define BACKEND_HOST "…"` line.
BOARD_DIRS = ("ultrasonic_sensor", "vibration_sensor", "door_keypad_controller", "esp32_cam")

_BACKEND_HOST_LINE = re.compile(r'^\s*#\s*define\s+BACKEND_HOST\s+"([^"\n]*)"', re.MULTILINE)


@dataclass(frozen=True)
class BoardIpStatus:
    board: str
    secrets_path: Path
    configured_host: str
    matches_current_ip: bool


def get_lan_ip() -> str | None:
    """Best-effort discovery of this machine's LAN-facing IPv4 address --
    the one a board's BACKEND_HOST needs to match to reach this process.

    Opens a UDP socket toward a public address and reads back which local
    address the OS routed it through. Nothing is actually sent: UDP has no
    handshake, so `connect()` only asks the routing table to pick an
    interface, the same one WiFi-connected boards on this network would
    reach this machine through. Returns None instead of raising if there is
    no route at all (e.g. WiFi off) -- a diagnostic helper should never be
    able to take startup down with it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def check_board_secrets(esp32_dir: Path, current_ip: str) -> list[BoardIpStatus]:
    """Reads BACKEND_HOST out of each board's secrets.h under `esp32_dir`
    and reports whether it matches `current_ip`.

    Boards whose secrets.h is missing, unreadable, or has no BACKEND_HOST
    line are left out of the result entirely rather than reported as a
    mismatch -- there is no configured value to compare in that case, so
    "stale" would be the wrong word for it.
    """
    results = []
    for board in BOARD_DIRS:
        path = esp32_dir / board / "secrets.h"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = _BACKEND_HOST_LINE.search(text)
        if not match:
            continue
        configured_host = match.group(1)
        results.append(
            BoardIpStatus(
                board=board,
                secrets_path=path,
                configured_host=configured_host,
                matches_current_ip=configured_host == current_ip,
            )
        )
    return results


def log_stale_board_ips(esp32_dir: Path | None = None, current_ip: str | None = None) -> None:
    """Logs a loud warning for every board whose secrets.h BACKEND_HOST
    doesn't match this machine's current LAN IP. Meant to be called once
    from `main.py`'s `lifespan`, so it runs every time the backend starts --
    exactly the moment this project has learned the hard way to distrust
    the IP still baked into the boards' firmware.
    """
    if esp32_dir is None:
        # This file lives at python/backend/ip_check.py, so three levels up
        # is the repo root, alongside esp32/.
        esp32_dir = Path(__file__).resolve().parent.parent.parent / "esp32"
    if current_ip is None:
        current_ip = get_lan_ip()

    if current_ip is None:
        logger.warning(
            "Could not determine this machine's LAN IP — skipping the "
            "esp32/*/secrets.h freshness check."
        )
        return

    results = check_board_secrets(esp32_dir, current_ip)
    stale = [r for r in results if not r.matches_current_ip]

    if not stale:
        if results:
            logger.info(
                "esp32/*/secrets.h BACKEND_HOST matches this machine's current IP "
                "(%s) on all %d checked board(s).",
                current_ip,
                len(results),
            )
        return

    logger.warning("=" * 72)
    logger.warning(
        "STALE BACKEND_HOST: this machine's current IP is %s, but:", current_ip
    )
    for r in stale:
        logger.warning(
            "  %-22s secrets.h has %-15s (%s)", r.board, r.configured_host, r.secrets_path
        )
    logger.warning(
        "Boards still flashed with the old IP cannot reach this backend — they'll "
        "show as offline with no other clue why."
    )
    logger.warning(
        "Fix: update BACKEND_HOST in the file(s) above, then recompile and reflash."
    )
    logger.warning("=" * 72)
