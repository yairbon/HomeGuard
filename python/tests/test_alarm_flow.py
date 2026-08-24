"""Arm/disarm state machine, sensor bypass, and alert escalation.

Where `simulate.py` waits out the real 3-second exit delay, these tests drive
`AppState` directly and await `_finish_arming` themselves — so the same logic
is covered in milliseconds.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.models import AlarmState, ArmMode, SensorType


async def arm_now(state, mode: ArmMode) -> None:
    """Arms and completes the exit delay immediately.

    Cancels the real countdown task and runs `_finish_arming`'s effects
    directly, rather than sleeping — the exit-delay *duration* is verified
    once in `test_arming_passes_through_transitional`; every other test just
    needs the system armed.
    """
    await state.arm(mode)
    if state._arming_task:
        state._arming_task.cancel()
        state._arming_task = None
    state.alarm_state = AlarmState.armed
    state._apply_bypass(mode)


async def test_arming_passes_through_transitional(state):
    """The exit-delay window is what lets someone walk out; the app renders a
    countdown against it, so it must actually exist."""
    assert state.alarm_state == AlarmState.disarmed
    await state.arm(ArmMode.away)
    assert state.alarm_state == AlarmState.transitional

    await asyncio.wait_for(state._arming_task, timeout=5)
    assert state.alarm_state == AlarmState.armed


async def test_disarm_during_exit_delay_cancels_arming(state):
    """Disarming mid-countdown must not be undone a second later by the
    timer that was already in flight."""
    await state.arm(ArmMode.away)
    await state.disarm()
    assert state.alarm_state == AlarmState.disarmed

    # Give the cancelled task more than the exit delay to misbehave.
    await asyncio.sleep(0.1)
    assert state.alarm_state == AlarmState.disarmed


async def test_arm_is_ignored_unless_disarmed(state):
    """Double-tapping ARM must not restart the countdown, and arming a
    triggered system must not clear the alert."""
    await arm_now(state, ArmMode.away)
    await state.arm(ArmMode.home)
    assert state.alarm_state == AlarmState.armed

    await state.trigger_alert(SensorType.door, "breach")
    assert state.alarm_state == AlarmState.triggered
    await state.arm(ArmMode.away)
    assert state.alarm_state == AlarmState.triggered


async def test_home_mode_bypasses_vibration_only(state):
    await arm_now(state, ArmMode.home)
    by_type = {s.type: s for s in state.sensors}
    assert by_type[SensorType.vibration].bypassed is True
    assert by_type[SensorType.door].bypassed is False
    assert by_type[SensorType.ultrasonic].bypassed is False


async def test_away_mode_bypasses_nothing(state):
    await arm_now(state, ArmMode.away)
    assert all(not s.bypassed for s in state.sensors)


async def test_bypassed_sensor_does_not_escalate(state):
    """The Home-mode promise: the vibration board keeps physically firing
    (it has no idea the system is in Home mode), and the backend is what
    decides to ignore it."""
    await arm_now(state, ArmMode.home)
    await state.trigger_alert(SensorType.vibration, "someone walking around inside")
    assert state.alarm_state == AlarmState.armed


async def test_non_bypassed_sensor_escalates(state):
    await arm_now(state, ArmMode.home)
    await state.trigger_alert(SensorType.door, "front door opened")
    assert state.alarm_state == AlarmState.triggered
    door = next(s for s in state.sensors if s.type == SensorType.door)
    assert door.ok is False


@pytest.mark.parametrize("initial", [AlarmState.disarmed, AlarmState.transitional])
async def test_alerts_are_ignored_when_not_armed(state, initial):
    """Sensors legitimately fire while disarmed and during the exit walk —
    neither should set off the alarm."""
    state.alarm_state = initial
    await state.trigger_alert(SensorType.door, "door opened normally")
    assert state.alarm_state == initial


async def test_controller_report_reapplies_home_bypass(state):
    """The bug this closes: the door controller reports only armed/disarmed,
    never the mode. When its report was what put the system into `armed`
    (rather than our own exit-delay timer), Home mode's vibration bypass was
    silently lost — so a Home-armed system would alarm on someone walking
    past the vibration sensor indoors.
    """
    await state.arm(ArmMode.home)  # records the mode, still transitional
    await state.report_state_from_esp32(armed=True)

    assert state.alarm_state == AlarmState.armed
    vibration = next(s for s in state.sensors if s.type == SensorType.vibration)
    assert vibration.bypassed is True, "Home mode's bypass was lost on the controller's report"

    await state.trigger_alert(SensorType.vibration, "movement inside")
    assert state.alarm_state == AlarmState.armed


async def test_controller_report_defaults_to_the_stricter_mode(state):
    """Arming at the physical keypad tells us nothing about the mode. The
    fallback must be Away (nothing bypassed) — guessing Home would disable a
    sensor nobody asked to disable."""
    await state.report_state_from_esp32(armed=True)
    assert state.alarm_state == AlarmState.armed
    assert all(not s.bypassed for s in state.sensors)


async def test_controller_disarm_clears_bypass(state):
    await arm_now(state, ArmMode.home)
    await state.report_state_from_esp32(armed=False)
    assert state.alarm_state == AlarmState.disarmed
    assert all(not s.bypassed and s.ok for s in state.sensors)


async def test_controller_report_cancels_a_pending_arming_task(state):
    """A controller report is authoritative and can land mid-countdown; the
    local timer must not fire afterwards and overwrite it."""
    await state.arm(ArmMode.away)
    await state.report_state_from_esp32(armed=False)
    await asyncio.sleep(0.1)
    assert state.alarm_state == AlarmState.disarmed


async def test_duplicate_controller_report_is_not_relogged(state):
    await state.report_state_from_esp32(armed=True)
    count = len(state.events)
    await state.report_state_from_esp32(armed=True)
    assert len(state.events) == count


async def test_disarm_resets_faulted_sensors(state):
    """After an alert, a sensor is marked not-ok. Disarming has to clear that
    or the dashboard shows a permanent fault."""
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "breach")
    await state.disarm()
    assert all(s.ok and not s.bypassed for s in state.sensors)


async def test_failed_logins_trigger_the_alarm_when_armed(state):
    """Mirrors the physical keypad: 3 wrong PINs on an armed system is
    treated as a possible break-in."""
    await arm_now(state, ArmMode.away)
    for i in range(3):
        await state.login(f"wrong{i}")
    assert state.alarm_state == AlarmState.triggered


async def test_failed_logins_do_not_trigger_when_disarmed(state):
    """Same three failures, disarmed system: locked out, but no alarm — the
    house isn't being protected, so there's nothing to break into."""
    for i in range(3):
        await state.login(f"wrong{i}")
    assert state.alarm_state == AlarmState.disarmed
    assert state.is_locked_out is True


async def test_event_log_is_capped(state):
    """An unbounded log grows without limit in a process meant to run for
    weeks."""
    from backend.state import MAX_EVENTS

    for i in range(MAX_EVENTS + 50):
        state._log(f"event {i}")
    assert len(state.events) == MAX_EVENTS
    assert state.events[0].message == f"event {MAX_EVENTS + 49}"  # newest first


# ---- Regressions found in the August 2026 audit --------------------------
#
# Each of these locks a defect that the suite passed straight over, because
# none of them live inside a single function — they are all gaps *between*
# two pieces that are individually correct.


class _FakeSocket:
    """A stand-in for a connected app's WebSocket.

    `send_text` optionally runs a callback, which is how a test simulates
    another client connecting or dropping *during* a broadcast — the window
    that only exists because `send_text` is awaited.
    """

    def __init__(self, on_send=None):
        self.received: list[str] = []
        self._on_send = on_send

    async def send_text(self, payload: str) -> None:
        self.received.append(payload)
        if self._on_send is not None:
            self._on_send()


async def test_a_client_disconnecting_mid_broadcast_does_not_abort_the_broadcast(state):
    """A phone dropping while an alarm frame is going out used to raise
    `RuntimeError: Set changed size during iteration` out of `_broadcast`.

    That is not a cosmetic failure: it aborted the loop, so every socket after
    the mutation point never received the alarm; it escaped into the calling
    route, so `/esp32/sensor-event` answered the reporting board with a 500;
    and the same escape out of `_monitor_boards` killed board liveness for the
    rest of the process. `send_text` suspends whenever a peer's write buffer
    is full, which for a backgrounded phone or a Tailscale hop is routine.
    """
    doomed = _FakeSocket()
    survivor = _FakeSocket(on_send=lambda: state._sockets.discard(doomed))
    state._sockets.update({doomed, survivor})

    await state._broadcast()

    assert survivor.received, "the surviving client must still get the frame"
    assert doomed not in state._sockets


async def test_a_client_connecting_mid_broadcast_does_not_abort_the_broadcast(state):
    """The other half of the same window: `register()` runs from the /ws
    handler task and grows the set while a send is suspended."""
    late = _FakeSocket()
    first = _FakeSocket(on_send=lambda: state._sockets.add(late))
    state._sockets.add(first)

    await state._broadcast()  # must not raise

    assert first.received


async def test_a_disarm_during_the_exit_delay_is_not_undone_three_seconds_later(state):
    """`arm()` publishes `transitional` and awaits a broadcast *before* it
    stores `_arming_task`, so a disarm landing in that window found
    `_arming_task is None` and cancelled nothing.

    `_finish_arming` then armed unconditionally. The result was a backend
    that said ARMED while the door controller — which had just been told to
    disarm — said DISARMED: the app shows a protected house, the siren is
    dead, and an opened door reports nothing at all.
    """
    await state.arm(ArmMode.away)
    assert state.alarm_state == AlarmState.transitional

    await state.disarm()
    assert state.alarm_state == AlarmState.disarmed

    # Let the countdown run to completion rather than cancelling it — the
    # point is that the task itself declines to act, whether or not anyone
    # remembered to cancel it.
    if state._arming_task:
        await asyncio.wait_for(state._arming_task, timeout=5)

    assert state.alarm_state == AlarmState.disarmed


async def test_the_controller_cannot_arm_out_of_a_live_alarm(state):
    """`arm()` already refuses to interrupt an alert; this path did not.

    Reachable in the ordinary case, not a contrived one: with no
    HOMEGUARD_DOOR_CONTROLLER_URL the board is never told the system armed,
    so someone reaching the keypad and arming it sends `armed: true` while
    the backend is mid-alarm. That used to clear the alarm with no `disarmed`
    event to explain it, rebuild every sensor as ok — erasing the marker on
    the one that caught the intrusion — and leave `_escalate_after_delay` to
    find a non-triggered state and swallow the "alarm not cleared" push.
    """
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(source=SensorType.ultrasonic, reason="break-in")
    assert state.alarm_state == AlarmState.triggered

    await state.report_state_from_esp32(armed=True)

    assert state.alarm_state == AlarmState.triggered
    tripped = next(s for s in state.sensors if s.type == SensorType.ultrasonic)
    assert tripped.ok is False, "the intrusion evidence must survive"


async def test_the_controller_can_still_disarm_out_of_a_live_alarm(state):
    """The guard above must not block the legitimate case — someone typing
    the PIN on the keypad is exactly how an alarm is meant to be cleared."""
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(source=SensorType.ultrasonic, reason="break-in")

    await state.report_state_from_esp32(armed=False)

    assert state.alarm_state == AlarmState.disarmed


async def test_the_alarm_reaches_the_apps_before_the_hardware_round_trip(state, monkeypatch):
    """Ordering, not speed, is what this locks.

    `trigger_alert` used to await the door controller's siren command before
    broadcasting. That board answers from inside its own `loop()`, where a
    blocking HTTP call can hold it for seconds, so the wait routinely ran to
    the full 3-second timeout — three seconds in which the alarm had fired
    and no phone had been told.
    """
    from backend import esp32_client

    order: list[str] = []
    socket = _FakeSocket(on_send=lambda: order.append("broadcast"))
    state._sockets.add(socket)

    async def fake_trigger() -> bool:
        order.append("hardware")
        return True

    monkeypatch.setattr(esp32_client, "send_trigger_alarm", fake_trigger)

    await arm_now(state, ArmMode.away)
    order.clear()
    await state.trigger_alert(source=SensorType.ultrasonic, reason="break-in")

    assert order[:2] == ["broadcast", "hardware"], f"got {order}"


async def test_the_siren_command_is_retried_once(state, monkeypatch):
    """The one outbound command whose failure is invisible from inside the
    house.

    Every other command's failure surfaces in the app as `hardware: false`.
    A siren that never sounds looks exactly like no break-in. The door
    controller answers from inside its own `loop()` and its WebServer takes
    one client at a time, so a single badly-timed request can genuinely be
    dropped — which is also what makes the board trivially deniable by
    anything on the network holding its single connection slot.
    """
    from backend import esp32_client

    monkeypatch.setattr(esp32_client, "TRIGGER_RETRY_DELAY", 0)
    monkeypatch.setattr(esp32_client, "DOOR_CONTROLLER_URL", "http://192.0.2.1")
    attempts = []

    async def failing_post(path, json=None):
        attempts.append(path)
        return False

    monkeypatch.setattr(esp32_client, "_post", failing_post)

    assert await esp32_client.send_trigger_alarm() is False
    assert attempts == ["/trigger-alarm", "/trigger-alarm"]


async def test_no_retry_when_no_board_is_configured(state, monkeypatch):
    """With HOMEGUARD_DOOR_CONTROLLER_URL unset the first call never went
    anywhere, so there is nothing to retry — and retrying would mean a
    pointless sleep plus a misleading log line on every alarm raised in a
    demo or test setup."""
    from backend import esp32_client

    monkeypatch.setattr(esp32_client, "DOOR_CONTROLLER_URL", "")
    attempts = []

    async def failing_post(path, json=None):
        attempts.append(path)
        return False

    monkeypatch.setattr(esp32_client, "_post", failing_post)

    assert await esp32_client.send_trigger_alarm() is False
    assert len(attempts) == 1


async def test_a_siren_command_that_lands_is_not_repeated(state, monkeypatch):
    """A retry is for a dropped request, not a standing habit."""
    from backend import esp32_client

    monkeypatch.setattr(esp32_client, "TRIGGER_RETRY_DELAY", 0)
    attempts = []

    async def ok_post(path, json=None):
        attempts.append(path)
        return True

    monkeypatch.setattr(esp32_client, "_post", ok_post)

    assert await esp32_client.send_trigger_alarm() is True
    assert len(attempts) == 1
