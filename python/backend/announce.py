"""Tells the boards where the backend is, so they need no fixed address.

Every few seconds the backend sends a small UDP broadcast ("HOMEGUARD 1 <port>") out of each of this
computer's private network addresses. A board that has lost the backend (see
esp32/*/net_discovery.h)
hears it, takes the sender's address, and checks it with GET /health before using it. Nothing is
sent to
addresses that are not on this computer's own networks, and nothing here carries a secret.

On a network that isolates clients or filters broadcasts the packets never arrive; the boards
then fall
back to their other ways of finding the backend. Set HOMEGUARD_ANNOUNCE=0 to switch it off.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

logger = logging.getLogger("announce")

UDP_PORT = 47808
INTERVAL_SECONDS = 3.0


def message(http_port: int) -> bytes:
    return f"HOMEGUARD 1 {http_port}".encode()


def private_addresses(candidates: list[str]) -> list[str]:
    """The ordinary private IPv4 addresses among `candidates` (no loopback, link-local or public
    ones)."""
    out = []
    for text in candidates:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not (ip.is_loopback or ip.is_link_local):
            if text not in out:
                out.append(text)
    return out


def local_addresses() -> list[str]:
    found: list[str] = []
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    try:  # the address used for the default route (connecting a UDP socket sends nothing)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    return private_addresses(found)


def _broadcast_once(http_port: int) -> int:
    sent = 0
    for address in local_addresses():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((address, 0))  # leaves through this network, whichever one it is
                s.sendto(message(http_port), ("255.255.255.255", UDP_PORT))
                sent += 1
        except OSError:
            continue
    return sent


async def announce_forever(http_port: int = 8000) -> None:
    logger.info(
        "Announcing the backend by UDP broadcast on port %d every %.0f s.",
        UDP_PORT,
        INTERVAL_SECONDS,
    )
    while True:
        await asyncio.to_thread(_broadcast_once, http_port)
        await asyncio.sleep(INTERVAL_SECONDS)
