"""Per-board liveness and the persisted state file.

Both cover behavior that simply did not exist before: board status was one
latching boolean, and nothing survived a restart.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from backend.models import AlarmState, ArmMode, SensorType

from .conftest import DEVICE_KEY, PASSWORD
from .test_alarm_flow import arm_now

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


async def test_a_door_event_marks_the_door_controller_seen(auth_client, device_headers, state):
    """The door reed switch hangs off the *controller*, so an unnamed door
    event means that board is alive. The fallback used to be the sensor's own
    name, which recorded liveness under "door" — a key no board is called and
    `board_statuses` never reads, so the fallback marked nothing online at
    all. It is also the board it mattered most for: the arm/disarm authority.
    """
    await auth_client.post(
        "/esp32/sensor-event",
        params={"sensor": "door", "reason": "forced"},
        headers=device_headers,
    )
    by_name = {b.name: b for b in state.board_statuses()}
    assert by_name["door_controller"].online is True


async def test_unknown_board_names_are_not_recorded(state):
    """`board` is a query string off the network and `_board_last_seen` is
    never pruned, so accepting arbitrary keys made it a dict a caller could
    grow without bound — and no such key could ever be read back, since
    `board_statuses` only looks up BOARD_NAMES."""
    await state.note_board_seen("ultrasonic")
    await state.note_board_seen("not-a-board")
    await state.note_board_seen("")

    assert set(state._board_last_seen) == {"ultrasonic"}
    assert len(state.board_statuses()) == 3


async def test_a_heartbeat_from_an_unknown_board_is_still_a_200(auth_client, device_headers):
    """Ignored, not rejected: a board with a mistyped BOARD_NAME retrying a
    failing heartbeat forever is worse than one whose heartbeat is quietly
    dropped, and the sketches have no way to report a 4xx to anyone."""
    response = await auth_client.post(
        "/esp32/heartbeat", params={"board": "typo"}, headers=device_headers
    )
    assert response.status_code == 200


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


async def test_a_state_file_that_is_not_an_object_falls_back_to_defaults(monkeypatch, tmp_path):
    """`_load` promises in its own docstring to degrade to defaults, but only
    handled files that failed to *parse*. Valid JSON that isn't an object —
    `[]`, `null`, a bare string — went straight into `data.get(...)` and
    raised AttributeError out of `__init__`, i.e. the backend did not start at
    all. Trivial to produce: an editor saving an empty array, or a truncated
    write leaving `null` behind.
    """
    for contents in ("[]", "null", '"hello"'):
        state_file = Path(tmp_path) / f"non-object-{len(contents)}.json"
        state_file.write_text(contents)
        monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
        monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

        from backend import state as state_module

        importlib.reload(state_module)  # must not raise
        assert await state_module.state.login(PASSWORD) is not None, contents


async def test_a_null_scalar_falls_back_to_its_default(monkeypatch, tmp_path):
    """The failure this had was displaced in both time and place. A present
    key holding `null` loaded fine — `data.get(k, default)` only defends
    against a *missing* key — and then `snapshot()` raised a Pydantic
    ValidationError on the first `/state` call and on every WebSocket connect,
    so the app could show nothing at all, with a traceback pointing at the
    model rather than at the file that caused it.
    """
    state_file = Path(tmp_path) / "null-scalars.json"
    state_file.write_text(
        json.dumps(
            {
                "emergency_number": None,
                "ntfy_topic": None,
                "night_mode": None,
                "police_call_delay_seconds": None,
            }
        )
    )
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    loaded = state_module.state

    assert loaded.emergency_number == ""
    assert loaded.ntfy_topic == ""
    assert loaded.night_mode is False
    assert loaded.police_call_delay_seconds == 60
    loaded.snapshot()  # the call that used to raise


async def test_a_negative_persisted_escalation_delay_is_clamped(monkeypatch, tmp_path):
    """A hand-edited file reaches `_escalate_after_delay` by exactly the same
    route the setter does, so the setter's clamp has to be applied on load
    too — otherwise the instant-escalation bug is one text editor away."""
    state_file = Path(tmp_path) / "negative-delay.json"
    state_file.write_text(json.dumps({"police_call_delay_seconds": -10}))
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    assert state_module.state.police_call_delay_seconds == 0


async def test_a_persisted_password_is_not_re_hashed_at_startup(monkeypatch, tmp_path):
    """scrypt is deliberately slow — ~50ms per hash (see security.py). Hashing
    the default password in `__init__` spent that on every single process
    start to produce a string `_load` discarded one line later, on every
    install that had ever been run.

    Patched on `backend.security` rather than on `backend.state`, because
    reloading `backend.state` re-executes its `from .security import ...` and
    would wipe a patch applied to the state module itself.
    """
    state_file = Path(tmp_path) / "lazy-hash.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import security
    from backend import state as state_module

    importlib.reload(state_module)  # first boot: nothing on disk yet

    hashed: list[str] = []
    real_hash = security.hash_password

    def counting_hash(password: str) -> str:
        hashed.append(password)
        return real_hash(password)

    monkeypatch.setattr(security, "hash_password", counting_hash)
    importlib.reload(state_module)  # second boot: the hash is already there

    assert hashed == [], "startup must not hash a password it is about to discard"
    # And the persisted hash is still the one that works.
    assert await state_module.state.login(PASSWORD) is not None


async def test_the_state_file_is_written_compactly(state):
    """`indent=2` cost time and a quarter more bytes on a file rewritten on
    every settings change and on every alert — `_persist` runs on the alarm
    path. `_load` parses either form identically, which is what this also
    checks by reading the events back."""
    from backend.state import STATE_FILE

    for i in range(20):
        state._log(f"event {i}")
    await state.set_emergency_number("100")

    raw = STATE_FILE.read_text()
    assert "\n" not in raw, "the state file should be a single compact line"

    saved = json.loads(raw)
    assert len(saved["events"]) == 20
    # `model_dump(mode="json")` has to produce the same wire form the old
    # `json.loads(model_dump_json())` did — a datetime as an ISO string and an
    # enum as its value, not a `datetime` object json.dumps would choke on.
    assert isinstance(saved["events"][0]["timestamp"], str)
    assert saved["events"][0]["category"] == "info"


async def test_events_survive_a_restart(monkeypatch, tmp_path):
    """An alert is exactly the event worth surviving a restart — `_persist`
    is called from `trigger_alert` for that reason."""
    state_file = Path(tmp_path) / "events-state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    await arm_now(state_module.state, ArmMode.away)
    await state_module.state.trigger_alert(SensorType.door, "front door forced")

    importlib.reload(state_module)
    restored = state_module.state.events

    assert any("front door forced" in e.message for e in restored)
    alert = next(e for e in restored if "front door forced" in e.message)
    assert alert.category.value == "alert"
    assert isinstance(alert.timestamp, datetime)


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


# ---- Background tasks and shutdown -----------------------------------------


async def test_the_board_monitor_survives_an_exception(state, monkeypatch):
    """One raise inside the loop used to end the task for good, and
    `start_monitor` only ever creates one when `_monitor_task is None` — a
    *dead* task is not None, so nothing restarts it. Board liveness then
    freezes for the rest of the process: a board that lost power keeps
    showing ONLINE in the app, and there isn't even a log line at the moment
    it stopped working.
    """
    from backend import state as state_module

    monkeypatch.setattr(state_module, "BOARD_MONITOR_INTERVAL", 0.01)

    polls = 0
    real_statuses = state.board_statuses

    def exploding_statuses():
        nonlocal polls
        polls += 1
        if polls == 2:
            raise RuntimeError("transient failure inside the loop")
        return real_statuses()

    monkeypatch.setattr(state, "board_statuses", exploding_statuses)

    state.start_monitor()
    try:
        await asyncio.sleep(0.15)
        assert not state._monitor_task.done(), "the loop must not die on one raise"
        assert polls > 2, "and it must keep polling afterwards"
    finally:
        await state.stop_monitor()


async def test_shutdown_cancels_every_pending_timer(state):
    """`stop_monitor` cancelled only the two long-running loops. The one-shot
    timers outlived it, and shutdown lands mid-countdown often enough —
    restarting the backend while the house is arming is the normal way to
    deploy a change. A pending timer waking on a closing loop is "Task was
    destroyed but it is pending!" at best, and at worst a `trigger_alert`
    broadcasting into torn-down WebSockets.
    """
    await state.arm(ArmMode.away)  # _arming_task
    state.alarm_state = AlarmState.armed
    await state.begin_entry_delay(30)  # _entry_delay_task
    await state.trigger_alert(SensorType.door, "break-in")  # _escalation_task

    pending = [state._arming_task, state._entry_delay_task, state._escalation_task]
    assert all(t is not None and not t.done() for t in pending)

    await state.stop_monitor()

    assert all(t.done() for t in pending)
    assert state._arming_task is None
    assert state._entry_delay_task is None
    assert state._escalation_task is None


async def test_shutdown_closes_every_outbound_http_client(app_module, monkeypatch):
    """Three clients talk out of this process; only one of them was being
    closed. The other two left their connection pools — sockets to the door
    controller and to the camera — for the garbage collector.

    Spies rather than the real clients, because the real ones are
    module-level singletons and closing them here would leave later tests in
    this session holding a closed client.
    """

    class _ClientSpy:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    from backend import esp32_client, notifier

    spies = {
        "ntfy": _ClientSpy(),
        "door controller": _ClientSpy(),
        "camera stream": _ClientSpy(),
    }
    monkeypatch.setattr(notifier, "_client", spies["ntfy"])
    monkeypatch.setattr(esp32_client, "_client", spies["door controller"])
    monkeypatch.setattr(app_module, "_cam_stream_client", spies["camera stream"])

    async with app_module.lifespan(app_module.app):
        pass

    assert all(spy.closed for spy in spies.values()), {
        name: spy.closed for name, spy in spies.items()
    }


async def test_the_camera_goes_offline_once_frames_stop(state, monkeypatch):
    """`camera_online` is derived from the last frame's timestamp, not stored.

    It used to be a plain flag that only `/camera/recognize` ever wrote, and
    only ever wrote `True` — nothing anywhere passed `False`. So a camera that
    lost power left the app showing a live badge over a frozen last frame, for
    the life of the process. That is the same latching-flag defect the
    per-board `esp32_online` boolean had, reintroduced for the one board that
    cannot be caught by heartbeats: the ESP32-CAM deliberately sends none, so
    frames are its only liveness signal.
    """
    from datetime import datetime, timedelta

    from backend.state import BOARD_TIMEOUT

    assert state.camera_online is False, "no frame has arrived yet"

    await state.note_camera_frame()
    assert state.camera_online is True

    # Shift the clock rather than sleeping out the real timeout.
    state._camera_last_frame_at = datetime.now() - BOARD_TIMEOUT - timedelta(seconds=1)
    assert state.camera_online is False


async def test_a_camera_frame_only_broadcasts_on_the_transition(state):
    """The ESP32-CAM sends two frames a second. A full-state push per frame
    would be megabytes a minute to every connected phone to say nothing new,
    so only the offline->online edge broadcasts."""

    class _Socket:
        def __init__(self):
            self.received = []

        async def send_text(self, payload):
            self.received.append(payload)

    socket = _Socket()
    state._sockets.add(socket)

    await state.note_camera_frame()
    assert len(socket.received) == 1, "the first frame flips the badge, so it must push"

    await state.note_camera_frame()
    await state.note_camera_frame()
    assert len(socket.received) == 1, "subsequent frames change nothing observable"
