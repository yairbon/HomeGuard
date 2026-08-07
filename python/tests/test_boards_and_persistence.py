"""Per-board liveness and the persisted state file.

Both cover behavior that simply did not exist before: board status was one
latching boolean, and nothing survived a restart.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from backend.models import ArmMode

from .conftest import DEVICE_KEY, PASSWORD

# ---- Board liveness --------------------------------------------------------


async def test_all_boards_start_offline(state):
    """Nothing has reported yet, so nothing is online. The old flag started
    False too — the difference is that it could never go back."""
    assert state.esp32_online is False
    boards = state.board_statuses()
    assert len(boards) == 3
    assert all(not b.online and b.last_seen is None for b in boards)


async def test_heartbeat_marks_only_the_reporting_board_online(state):
    await state.note_board_seen("ultrasonic")
    by_name = {b.name: b for b in state.board_statuses()}

    assert by_name["ultrasonic"].online is True
    assert by_name["ultrasonic"].last_seen is not None
    # The whole point of per-board tracking: one board reporting must not
    # vouch for the other two.
    assert by_name["vibration"].online is False
    assert by_name["door_controller"].online is False


async def test_board_goes_offline_when_heartbeats_stop(state):
    """The regression this exists for: `esp32_online` used to latch True on
    the first heartbeat and stay True forever, so a board that lost power
    still showed as ONLINE in the app indefinitely.
    """
    from backend.state import BOARD_TIMEOUT

    await state.note_board_seen("vibration")
    assert state.board_statuses()[1].online is True

    # Age the heartbeat past the timeout rather than sleeping for it.
    state._board_last_seen["vibration"] = datetime.now() - BOARD_TIMEOUT - timedelta(seconds=1)

    by_name = {b.name: b for b in state.board_statuses()}
    assert by_name["vibration"].online is False
    assert by_name["vibration"].last_seen is not None, "should remember it was once seen"
    assert state.esp32_online is False


async def test_aggregate_is_online_if_any_board_is(state):
    from backend.state import BOARD_TIMEOUT

    await state.note_board_seen("ultrasonic")
    await state.note_board_seen("door_controller")
    state._board_last_seen["ultrasonic"] = datetime.now() - BOARD_TIMEOUT - timedelta(seconds=1)

    assert state.esp32_online is True  # door_controller is still reporting


async def test_state_endpoint_exposes_boards(auth_client, device_headers):
    await auth_client.post(
        "/esp32/heartbeat", params={"board": "door_controller"}, headers=device_headers
    )
    body = (await auth_client.get("/state")).json()

    assert len(body["boards"]) == 3
    by_name = {b["name"]: b for b in body["boards"]}
    assert by_name["door_controller"]["online"] is True
    assert by_name["ultrasonic"]["online"] is False


async def test_sensor_event_marks_the_board_seen(auth_client, device_headers, state):
    await auth_client.post(
        "/esp32/sensor-event",
        params={"sensor": "vibration", "reason": "knock"},
        headers=device_headers,
    )
    by_name = {b.name: b for b in state.board_statuses()}
    assert by_name["vibration"].online is True


# ---- Persistence -----------------------------------------------------------


async def test_state_file_is_written(state, app_module):
    from backend.state import STATE_FILE

    await state.set_emergency_number("100")
    assert STATE_FILE.exists()
    saved = json.loads(STATE_FILE.read_text())
    assert saved["emergency_number"] == "100"
    assert saved["password_hash"].startswith("scrypt$")


async def test_password_change_survives_a_restart(client, monkeypatch, tmp_path):
    """The concrete failure this fixes: the backend restarts, and the
    password silently reverts to the default `homeguard` while the user still
    believes theirs is in effect.
    """
    state_file = Path(tmp_path) / "restart-state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    first_boot = state_module.state

    token = await first_boot.login(PASSWORD)
    assert await first_boot.change_password(PASSWORD, "after-restart", current_token=token) is True

    # Restart: a brand-new AppState reading the same file.
    importlib.reload(state_module)
    second_boot = state_module.state

    assert await second_boot.login(PASSWORD) is None, "old password should be gone"
    assert await second_boot.login("after-restart") is not None


async def test_faces_survive_a_restart(monkeypatch, tmp_path):
    state_file = Path(tmp_path) / "faces-state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    added = await state_module.state.add_face("Restored Person")

    importlib.reload(state_module)
    names = [f.name for f in state_module.state.faces]
    assert "Restored Person" in names
    # And the id counter must resume past the restored faces, not restart at
    # 3 and hand a new person the previous holder's training-photo folder.
    new_face = await state_module.state.add_face("Someone Else")
    assert new_face.id != added.id
    assert int(new_face.id) > int(added.id)


async def test_alarm_state_is_deliberately_not_persisted(monkeypatch, tmp_path):
    """A restarted backend genuinely doesn't know whether the house is armed.
    Coming up disarmed and letting the door controller's next report
    establish the truth is honest; writing a stale `armed` back would make
    the app display a state nothing has verified.
    """
    state_file = Path(tmp_path) / "armed-state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    await state_module.state.arm(ArmMode.away)
    state_module.state._arming_task.cancel()

    saved = json.loads(state_file.read_text())
    assert "alarm_state" not in saved


async def test_corrupt_state_file_falls_back_to_defaults(monkeypatch, tmp_path):
    """An alarm that refuses to start because its config file has a typo is
    worse than one that starts with defaults and says so."""
    state_file = Path(tmp_path) / "corrupt.json"
    state_file.write_text("{ this is not json")
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    assert await state_module.state.login(PASSWORD) is not None


async def test_generated_device_key_is_stable_across_restarts(monkeypatch, tmp_path):
    """If the key changed on every boot, all three boards would need
    re-flashing every time the backend restarted."""
    state_file = Path(tmp_path) / "devkey.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.delenv("HOMEGUARD_DEVICE_KEY", raising=False)

    from backend import state as state_module

    importlib.reload(state_module)
    first = state_module.state.device_key
    assert first, "a key should be generated when none is configured"

    importlib.reload(state_module)
    assert state_module.state.device_key == first


async def test_env_device_key_wins_over_the_persisted_one(monkeypatch, tmp_path):
    state_file = Path(tmp_path) / "devkey2.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.delenv("HOMEGUARD_DEVICE_KEY", raising=False)

    from backend import state as state_module

    importlib.reload(state_module)
    generated = state_module.state.device_key

    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", "explicitly-configured")
    importlib.reload(state_module)
    assert state_module.state.device_key == "explicitly-configured"
    assert state_module.state.device_key != generated
