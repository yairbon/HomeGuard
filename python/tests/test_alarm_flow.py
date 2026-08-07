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
