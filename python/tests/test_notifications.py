"""Push notifications via ntfy.

The behaviour under test is mostly about *when* a push happens and what it
must not do — never block the alert path, never fire for routine events, and
never fire when the feature is switched off.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.models import ArmMode, SensorType

from .test_alarm_flow import arm_now


@pytest.fixture
def sent(monkeypatch):
    """Captures every outbound push instead of performing it.

    Patches `_send` rather than `notify`, so the real `notify` — including
    its "no topic means no-op" guard and its fire-and-forget task creation —
    is still what's being exercised.
    """
    calls: list[dict] = []

    async def fake_send(server, topic, title, message, priority, tags):
        calls.append(
            {
                "server": server,
                "topic": topic,
                "title": title,
                "message": message,
                "priority": priority,
                "tags": tags,
            }
        )

    from backend import notifier

    monkeypatch.setattr(notifier, "_send", fake_send)
    return calls


async def drain():
    """Lets the fire-and-forget notification task actually run.

    `notify` schedules the send and returns immediately, so without yielding
    to the event loop the task would still be pending when the assertion runs.
    """
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_no_push_when_no_topic_is_configured(state, sent):
    """Off by default: the topic is the only access control on a public ntfy
    server, so nothing is pushed until the user picks one."""
    assert state.ntfy_topic == ""
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await drain()
    assert sent == []


async def test_triggered_alarm_pushes(state, sent):
    await state.set_ntfy("https://ntfy.example", "my-secret-topic")
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await drain()

    assert len(sent) == 1
    assert sent[0]["server"] == "https://ntfy.example"
    assert sent[0]["topic"] == "my-secret-topic"
    assert "front door forced" in sent[0]["message"]
    # High priority so it breaks through Do Not Disturb — the entire point is
    # that this arrives at 3am.
    assert sent[0]["priority"] == "high"


async def test_routine_events_do_not_push(state, sent):
    """Arming, disarming and logins must not notify. A channel that fires on
    everything gets muted, and then it's not there for the one that matters.
    """
    await state.set_ntfy("https://ntfy.example", "topic")
    await state.arm(ArmMode.away)
    await state.disarm()
    await state.login("homeguard")
    await drain()
    assert sent == []


async def test_ignored_alert_does_not_push(state, sent):
    """A sensor firing while disarmed is not an alarm, so it must not wake
    anyone up."""
    await state.set_ntfy("https://ntfy.example", "topic")
    await state.trigger_alert(SensorType.door, "door opened while disarmed")
    await drain()
    assert sent == []


async def test_bypassed_sensor_does_not_push(state, sent):
    """Home mode's whole promise is that indoor movement is ignored — it must
    not produce a phone notification either."""
    await state.set_ntfy("https://ntfy.example", "topic")
    await arm_now(state, ArmMode.home)
    await state.trigger_alert(SensorType.vibration, "movement inside")
    await drain()
    assert sent == []


async def test_push_failure_does_not_break_the_alarm(state, monkeypatch):
    """An unreachable ntfy server must not stop the system from raising the
    alarm locally — the in-app state is what matters most."""
    from backend import notifier

    async def exploding_send(*args, **kwargs):
        raise RuntimeError("ntfy is down")

    monkeypatch.setattr(notifier, "_send", exploding_send)

    await state.set_ntfy("https://ntfy.example", "topic")
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "breach")
    await drain()

    from backend.models import AlarmState

    assert state.alarm_state == AlarmState.triggered


async def test_notify_never_blocks(state, monkeypatch):
    """The alert path must not wait on the network.

    `trigger_alert` runs on the same event loop as every connected WebSocket,
    so awaiting a slow push would stall the real-time state update to every
    app instance. This pins that: a push that takes a long time must not
    delay `trigger_alert` returning.
    """
    from backend import notifier

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(notifier, "_send", slow_send)

    await state.set_ntfy("https://ntfy.example", "topic")
    await arm_now(state, ArmMode.away)

    await asyncio.wait_for(state.trigger_alert(SensorType.door, "breach"), timeout=2)


async def test_settings_round_trip(auth_client):
    response = await auth_client.post(
        "/settings/ntfy", json={"server": "https://push.example", "topic": "abc123"}
    )
    assert response.json()["ok"] is True

    body = (await auth_client.get("/state")).json()
    assert body["ntfy_server"] == "https://push.example"
    assert body["ntfy_topic"] == "abc123"


async def test_blank_server_falls_back_to_the_public_default(state):
    """So a user can supply only a topic and have it work."""
    await state.set_ntfy("   ", "topic")
    assert state.ntfy_server == "https://ntfy.sh"


async def test_topic_is_not_written_to_the_event_log(state):
    """The topic is effectively a password on a public ntfy server, and the
    app displays the event log."""
    await state.set_ntfy("https://ntfy.example", "super-secret-topic-name")
    assert all("super-secret-topic-name" not in e.message for e in state.events)


async def test_test_notification_reports_whether_it_is_configured(state, sent):
    assert await state.send_test_notification() is False

    await state.set_ntfy("https://ntfy.example", "topic")
    assert await state.send_test_notification() is True
    await drain()
    assert len(sent) == 1
    # Not an emergency — must not use the alarm's priority or icon.
    assert sent[0]["priority"] == "default"


async def test_ntfy_settings_survive_a_restart(monkeypatch, tmp_path):
    import importlib
    from pathlib import Path

    from .conftest import DEVICE_KEY

    state_file = Path(tmp_path) / "ntfy-state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    await state_module.state.set_ntfy("https://push.example", "persisted-topic")

    importlib.reload(state_module)
    assert state_module.state.ntfy_topic == "persisted-topic"
    assert state_module.state.ntfy_server == "https://push.example"


async def test_an_in_flight_push_is_held_by_a_strong_reference(state, monkeypatch):
    """asyncio keeps only a *weak* reference to a running task.

    `notify()` schedules and returns — correct, and locked by
    `test_notify_never_blocks` above — but for a while nothing else held the
    resulting task either, so one suspended on the ntfy round-trip could be
    collected mid-flight. The push then never left the box: no exception, no
    log line, nothing. That is the one notification path that reaches a phone
    with the app swiped away, so a silent drop is the whole feature failing
    at exactly the moment it exists for.

    Asserting on the registry rather than trying to provoke a real collection
    — `gc` timing is not something a test should depend on.
    """
    from backend import notifier

    started = asyncio.Event()

    async def slow_send(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(notifier, "_send", slow_send)

    notifier.notify("https://ntfy.example", "topic", "title", "message")
    await asyncio.wait_for(started.wait(), timeout=2)

    assert notifier._pending, "an in-flight push must be referenced somewhere"

    for task in list(notifier._pending):
        task.cancel()


async def test_a_finished_push_is_released(state, monkeypatch):
    """The registry must not become a leak — entries remove themselves."""
    from backend import notifier

    async def instant_send(*args, **kwargs):
        return None

    monkeypatch.setattr(notifier, "_send", instant_send)

    notifier.notify("https://ntfy.example", "topic", "title", "message")
    await asyncio.sleep(0)  # let the task run to completion
    await asyncio.sleep(0)  # and let the done-callback fire

    assert not notifier._pending
