"""Communication with the door/keypad controller board.

There are three ESP32 boards in this project (see esp32/), each with its
own sketch. Two of them (ultrasonic, vibration) are pure sensor-push
clients — they call this backend's `/esp32/sensor-event` and
`/esp32/heartbeat` endpoints directly and never receive anything back, so
they need no entry here.

The door/keypad controller is different: it's the arm/disarm authority
(it owns the keypad), so the app needs to be able to command it remotely,
not just receive reports from it. That board runs a small WebServer with
`/arm` and `/disarm` routes — see esp32/door_keypad_controller/. This
module's `send_arm`/`send_disarm` call those routes and are believed to
match the real firmware (built together with it), but re-check against
the actual sketch if you change either side.
"""

from __future__ import annotations

import logging
import os

import httpx

from .models import ArmMode
from .state import state

logger = logging.getLogger("esp32_client")

# The door/keypad controller board's IP, e.g. "http://192.168.1.50".
# Find it by opening the Arduino IDE's Serial Monitor after that board
# boots — it prints its assigned IP on connect.
DOOR_CONTROLLER_URL = os.environ.get("HOMEGUARD_DOOR_CONTROLLER_URL", "")

_client = httpx.AsyncClient(timeout=3.0)


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


async def send_arm(mode: ArmMode) -> bool:
    """Tell the door controller to arm. It runs its own exit-delay
    countdown before actually going ARMED (see the sketch's EXITING
    state) and reports back via /esp32/state-change once it does.
    """
    return await _post("/arm", json={"mode": mode.value})


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
    """
    return await _post("/trigger-alarm")
