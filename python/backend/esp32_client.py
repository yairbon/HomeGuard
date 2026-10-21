"""Communication with the door/keypad controller board.

There are four ESP32 boards in this project (see esp32/), each with its
own sketch. Three of them (ultrasonic, vibration, ESP32-CAM) are pure push
clients — they call this backend's `/esp32/sensor-event`,
`/esp32/heartbeat` and `/camera/recognize` endpoints directly and never
receive anything back, so they need no entry here. (The camera board is
also *pulled* from, by the stream proxy in main.py, but that has its own
client — a stream connection needs a completely different timeout profile
from a 3-second command POST.)

The door/keypad controller is different: it's the arm/disarm authority
(it owns the keypad), so the app needs to be able to command it remotely,
not just receive reports from it. That board runs a small WebServer with
`/arm` and `/disarm` routes — see esp32/door_keypad_controller/. This
module's `send_arm`/`send_disarm` call those routes and are believed to
match the real firmware (built together with it), but re-check against
the actual sketch if you change either side.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os

import httpx

from .http_clients import build_client
from .models import ArmMode
from .state import state

logger = logging.getLogger("esp32_client")

# The door/keypad controller board's IP, e.g. "http://192.168.1.50".
# Find it by opening the Arduino IDE's Serial Monitor after that board
# boots — it prints its assigned IP on connect.
DOOR_CONTROLLER_URL = os.environ.get("HOMEGUARD_DOOR_CONTROLLER_URL", "")


def private_board_address(host: str | None) -> str | None:
    """`host` if it is an ordinary private IPv4 address a board on the local
    network could have, else None. Loopback, link-local, multicast, unspecified
    and public addresses are refused, so a request that did not come from the
    local network never changes where commands are sent."""
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return None
    if ip.version != 4 or not ip.is_private:
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return None
    return str(ip)


def learn_door_address(host: str | None) -> bool:
    """Points door commands at the address the door board's own request came
    from. The configured value (HOMEGUARD_DOOR_CONTROLLER_URL) is only the
    starting point: a router or a phone hotspot hands out different addresses,
    and a stale one made every arm and disarm silently miss the board. Returns
    whether the address changed. Authentication is untouched: the caller has
    already proved it holds the device key."""
    global DOOR_CONTROLLER_URL
    address = private_board_address(host)
    if address is None:
        return False
    new = f"http://{address}"
    if new == DOOR_CONTROLLER_URL:
        return False
    logger.info("Door controller address: %s -> %s", DOOR_CONTROLLER_URL or "(none)", new)
    DOOR_CONTROLLER_URL = new
    return True

# `build_client` is what supplies `trust_env=False` here, and that matters
# more on this client than on any other: the target is a bare LAN IP, and a
# stray HTTP_PROXY in the environment would route every arm/disarm command
# through a proxy with no route to 192.168.x.x. That doesn't fail fast — it
# burns the full timeout and comes back as `hardware: false`, i.e. the app
# reporting a board unreachable while the board sits there fine.
_client = build_client(timeout=3.0)

# Pause before the one retry in `send_trigger_alarm`. Short: the point is to
# step over a board that was busy for a moment, not to wait out one that is
# gone.
TRIGGER_RETRY_DELAY = 0.5


async def _post(path: str, json: dict | None = None) -> bool:
    """Shared helper behind send_arm/send_disarm: POSTs to the door
    controller and reports success as a bool rather than raising, since
    callers (arm/disarm route handlers) treat "couldn't reach the board"
    as a soft failure, not an error to surface as a 500.

    If DOOR_CONTROLLER_URL isn't configured (e.g. running the backend
    without real hardware attached), this is a no-op that just logs and
    returns False — lets the rest of the app/demo work without a board.

    Also doubles as a lightweight connectivity signal: a successful request
    marks the door controller as recently seen, on the same per-board
    timestamp the heartbeats feed. A *failure* deliberately doesn't mark it
    offline — board liveness is derived from "how long since we last heard
    from it" (see AppState.board_statuses), so one failed outbound request
    doesn't need to, and shouldn't, override that. It'll age out on its own
    if the board really is gone.

    Authenticates with the shared device key so the board can tell a genuine
    backend command from anything else on the network that found its IP —
    this is the channel that can disarm an armed alarm remotely.
    """
    if not DOOR_CONTROLLER_URL:
        logger.info(
            "HOMEGUARD_DOOR_CONTROLLER_URL not set — skipping outbound command to %s", path
        )
        return False
    try:
        response = await _client.post(
            f"{DOOR_CONTROLLER_URL}{path}",
            json=json,
            headers={"X-Device-Key": state.device_key},
        )
        response.raise_for_status()
        await state.note_board_seen("door_controller")
        return True
    except httpx.HTTPError as exc:
        logger.warning("Door controller request to %s failed: %s", path, exc)
        return False


async def send_arm(mode: ArmMode) -> str:
    """Tell the door controller to arm. It runs its own exit-delay
    countdown before actually going ARMED (see the sketch's EXITING
    state) and reports back via /esp32/state-change once it does.

    Returns what happened, because "the board said no" and "the board did not
    answer" call for opposite reactions:

    - ``"ok"``: the board accepted.
    - ``"door_open"``: the board answered 409 — it refuses to arm with its door
      open (arming then would start the entry countdown the instant it
      finished). The caller must not arm.
    - ``"no_ack"``: no answer, no URL configured, or any other refusal. The
      caller arms its own state anyway and says the hardware did not respond.
    """
    if not DOOR_CONTROLLER_URL:
        logger.info("HOMEGUARD_DOOR_CONTROLLER_URL not set — skipping outbound command to /arm")
        return "no_ack"
    try:
        response = await _client.post(
            f"{DOOR_CONTROLLER_URL}/arm",
            json={"mode": mode.value},
            headers={"X-Device-Key": state.device_key},
        )
    except httpx.HTTPError as exc:
        logger.warning("Door controller request to /arm failed: %s", exc)
        return "no_ack"
    if response.status_code == 409:
        await state.note_board_seen("door_controller")  # it answered, so it is there
        return "door_open"
    if response.is_success:
        await state.note_board_seen("door_controller")
        return "ok"
    logger.warning("Door controller answered /arm with HTTP %d", response.status_code)
    return "no_ack"


async def send_disarm() -> bool:
    """Tell the door controller to disarm immediately."""
    return await _post("/disarm")


async def send_keypad_pin(pin: str) -> bool:
    """Push a new disarm PIN to the door controller's NVS.

    The board persists it in flash and uses it from that moment on, so this
    is what actually changes the PIN — `secrets.h` only supplies the default
    for a board that has never been told one (see backend/secrets_file.py).

    Callers must have validated `pin` first (`is_valid_keypad_pin`); the
    board validates again on its side, and rejects with 400 rather than
    storing something the keypad could not type.

    Returns False when the board is unreachable. The PIN stays stored on this
    side either way, so the app can retry once the board is back rather than
    losing the change.
    """
    return await _post("/keypad-pin", json={"pin": pin})


async def send_trigger_alarm() -> bool:
    """Tell the door controller to sound its siren immediately.

    Its buzzers otherwise only react to its own local door sensor + keypad
    state machine — a breach reported by a *different* board (ultrasonic,
    vibration) reaches the app fine but never reaches the door controller's
    hardware. This is how state.trigger_alert() closes that gap.

    The one command here that gets a retry, because it is the one whose
    failure is silent in the physical world: every other command's failure
    surfaces in the app as `hardware: false`, but a siren that never sounds
    looks, from inside the house, exactly like no break-in. The board answers
    from inside its own `loop()` and its WebServer serves one client at a
    time, so a single badly-timed request genuinely can be dropped — while
    one of its own reports is in flight, or while that single slot is held.

    Safe to repeat: `handleRemoteTriggerAlarm` only sets ALARM, so a second
    delivery of a command that already arrived changes nothing. Cheap, too —
    `trigger_alert` broadcasts to the apps *before* calling this, so the retry
    delays no one.
    """
    if await _post("/trigger-alarm"):
        return True

    # Nothing to retry against on an install with no board configured — the
    # first call didn't fail, it never went anywhere. Retrying would mean a
    # pointless half-second sleep and a misleading log line on every alarm
    # raised in a demo or test setup.
    if not DOOR_CONTROLLER_URL:
        return False

    await asyncio.sleep(TRIGGER_RETRY_DELAY)
    logger.warning("Retrying the siren command — the first attempt did not land")
    return await _post("/trigger-alarm")


async def aclose() -> None:
    """Closes the shared HTTP client, for a clean shutdown — the same job
    `notifier.aclose()` does, called from the same lifespan teardown.

    Without it, shutdown left this client's connection pool (and its
    transport's sockets) to be closed by the garbage collector, which on a
    reload-per-test suite like this one means a stream of "unclosed client"
    warnings and, in a real deployment, a half-open TCP connection to the
    door controller that nothing ever tore down.
    """
    await _client.aclose()
