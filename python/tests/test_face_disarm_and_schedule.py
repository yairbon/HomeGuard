"""Face-disarm, the entry-delay window, and the night-mode schedule.

All three replace behaviour that was previously either absent or dishonest:
recognizing a face did nothing, the entry countdown was invisible to the app,
and the night-mode switch was stored but never acted on while the UI claimed
it auto-armed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from backend.models import AlarmState, ArmMode, EventCategory, SensorType

from .test_alarm_flow import arm_now

# ---- Face disarm -----------------------------------------------------------


async def test_face_disarm_is_off_by_default(state):
    """The safe default. Enabling it lowers disarming from "knows the PIN or
    password" to "has a photo of a resident", so it must be a deliberate
    choice rather than something a user gets without asking.
    """
    assert state.face_disarm_enabled is False

    await arm_now(state, ArmMode.away)
    await state.log_face_recognized("Guy Bar")
    assert state.alarm_state == AlarmState.armed


async def test_recognized_face_disarms_when_enabled(state):
    await state.set_face_disarm_enabled(True)
    await arm_now(state, ArmMode.away)

    disarmed = await state.log_face_recognized("Guy Bar")
    assert disarmed is True
    assert state.alarm_state == AlarmState.disarmed


async def test_recognized_face_clears_a_sounding_alarm(state):
    """Disarming matters most once the alarm is already going off — that's
    the moment someone is standing at their own door being shouted at."""
    await state.set_face_disarm_enabled(True)
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "door opened")
    assert state.alarm_state == AlarmState.triggered

    assert await state.log_face_recognized("Yair Bonfil") is True
    assert state.alarm_state == AlarmState.disarmed


async def test_recognized_face_disarms_during_entry_delay(state):
    """The normal case: you walk in, the countdown starts, the camera sees
    you before it runs out."""
    await state.set_face_disarm_enabled(True)
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(25)
    assert state.alarm_state == AlarmState.entryDelay

    assert await state.log_face_recognized("Guy Bar") is True
    assert state.alarm_state == AlarmState.disarmed


async def test_recognition_while_disarmed_is_only_logged(state):
    """Nothing to disarm, so this is purely an audit entry — and must not
    produce a spurious "Disarmed by Face ID" record."""
    await state.set_face_disarm_enabled(True)
    assert await state.log_face_recognized("Guy Bar") is False
    assert state.alarm_state == AlarmState.disarmed
    assert not any("Disarmed by Face ID" in e.message for e in state.events)


async def test_face_disarm_is_recorded_in_the_log(state):
    """Who turned the alarm off, and how, is exactly what an activity log is
    for."""
    await state.set_face_disarm_enabled(True)
    await arm_now(state, ArmMode.away)
    await state.log_face_recognized("Guy Bar")

    messages = [e.message for e in state.events]
    assert any("Disarmed by Face ID: Guy Bar" in m for m in messages)


async def test_toggling_face_disarm_is_itself_logged(state):
    """Changing what it takes to disarm the alarm is a security event."""
    await state.set_face_disarm_enabled(True)
    assert any("Face ID disarm enabled" in e.message for e in state.events)


async def test_face_disarm_setting_survives_a_restart(monkeypatch, tmp_path):
    import importlib
    from pathlib import Path

    from .conftest import DEVICE_KEY

    state_file = Path(tmp_path) / "fd-state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    await state_module.state.set_face_disarm_enabled(True)

    importlib.reload(state_module)
    assert state_module.state.face_disarm_enabled is True


# ---- Face disarm liveness ---------------------------------------------------
#
# `live` is what main.py's recognize_frame computes from
# face_recognition_service.is_live_landmark_sequence (see test_liveness.py
# for that decision in isolation) before ever calling log_face_recognized —
# these tests are about what log_face_recognized itself does with the
# result, independent of the camera/CV pipeline.


async def test_live_face_still_disarms(state):
    """Regression guard: `live` defaults to True, so every call site that
    predates the liveness check — this one included — must keep disarming
    exactly as it did before.
    """
    await state.set_face_disarm_enabled(True)
    await arm_now(state, ArmMode.away)

    disarmed = await state.log_face_recognized("Guy Bar", live=True)
    assert disarmed is True
    assert state.alarm_state == AlarmState.disarmed


async def test_a_non_live_match_does_not_disarm(state):
    """The whole point of the feature: a recognized face that fails the
    liveness check must not disarm, even with face-disarm enabled and the
    system armed — the two conditions that alone would otherwise be
    sufficient. The attempt is still recorded, distinctly from a plain
    sighting or an actual disarm.
    """
    await state.set_face_disarm_enabled(True)
    await arm_now(state, ArmMode.away)

    disarmed = await state.log_face_recognized("Guy Bar", live=False)

    assert disarmed is False
    assert state.alarm_state == AlarmState.armed
    assert not any("Disarmed by Face ID" in e.message for e in state.events)
    assert any(
        "Face recognized but liveness check failed: Guy Bar" in e.message
        for e in state.events
    )


async def test_a_non_live_match_is_logged_once_not_every_frame(state):
    """A recognized-but-not-live match is a security-relevant signal — it's
    what a spoofing attempt looks like — so it must reach the log, but the
    same per-person FACE_LOG_COOLDOWN that protects plain sightings from
    the ESP32-CAM's two-frames-a-second feed has to protect this line too,
    or a photo held up to the camera fills the log exactly the way a
    lingering real face used to (see
    test_a_lingering_face_is_logged_once_not_twice_a_second above).
    """
    await state.set_face_disarm_enabled(True)
    await arm_now(state, ArmMode.away)

    for _ in range(20):
        await state.log_face_recognized("Guy Bar", live=False)

    entries = [e for e in state.events if "liveness check failed" in e.message]
    assert len(entries) == 1, f"expected one rate-limited entry, got {len(entries)}"
    assert all(e.category == EventCategory.armDisarm for e in entries)
    assert state.alarm_state == AlarmState.armed


# ---- Entry delay -----------------------------------------------------------


async def test_entry_delay_only_starts_from_armed(state):
    """A door opening on a disarmed system is just a door opening."""
    await state.begin_entry_delay(25)
    assert state.alarm_state == AlarmState.disarmed


async def test_entry_delay_exposes_a_countdown(state):
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(25)

    assert state.alarm_state == AlarmState.entryDelay
    # Derived from a deadline, so it's within a second of the requested value.
    assert 23 <= state.entry_delay_seconds_remaining <= 25


async def test_entry_delay_countdown_is_zero_in_other_states(state):
    await arm_now(state, ArmMode.away)
    assert state.entry_delay_seconds_remaining == 0


async def test_entry_delay_escalates_to_the_alarm(state):
    """When the controller's countdown expires it sends its door alert, and
    that has to be able to escalate from `entryDelay` — not just from
    `armed`."""
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(25)
    await state.trigger_alert(SensorType.door, "not disarmed in time")
    assert state.alarm_state == AlarmState.triggered


async def test_disarming_clears_the_entry_countdown(state):
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(25)
    await state.disarm()

    assert state.alarm_state == AlarmState.disarmed
    assert state.entry_delay_seconds_remaining == 0


@pytest.fixture
def instant_grace(monkeypatch):
    """Shrinks the watchdog's grace period so its timer can be awaited
    directly instead of waited out. The window itself is passed per call
    (`begin_entry_delay(0)`), so between the two there is nothing left to
    sleep through."""
    from backend import state as state_module

    monkeypatch.setattr(state_module, "ENTRY_DELAY_GRACE", timedelta(0))


async def test_the_entry_delay_alarms_even_if_the_controller_goes_silent(
    state, instant_grace
):
    """The hole this closes is a total, silent bypass of the alarm.

    The entry countdown belongs to the door controller, and escalation used to
    depend entirely on that one board living long enough to report the
    outcome. It is inside the house the intruder just walked into: cut its
    power (or its WiFi) between opening the door and the countdown expiring
    and the system sat in `entryDelay` forever — never `triggered`, no siren
    command to the other boards, no push, and an app showing a countdown that
    never reached zero. Nobody is told anything, ever.
    """
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(0)
    assert state.alarm_state == AlarmState.entryDelay

    # The controller says nothing from here on — this is the board being dead.
    await asyncio.wait_for(state._entry_delay_task, timeout=5)

    assert state.alarm_state == AlarmState.triggered
    assert any("entry delay expired" in e.message for e in state.events)


async def test_disarming_in_time_cancels_the_entry_delay_watchdog(state, instant_grace):
    """Typing the PIN is the whole point of the window. The fallback must not
    alarm a house that was disarmed in time."""
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(0)
    await state.disarm()

    assert state._entry_delay_task is None
    await asyncio.sleep(0.05)  # more than enough for a cancelled timer to misfire
    assert state.alarm_state == AlarmState.disarmed


async def test_a_controller_disarm_cancels_the_entry_delay_watchdog(state, instant_grace):
    """And the same via the path that actually runs in the field: the keypad
    belongs to the controller, so a real disarm arrives as a state report,
    not as a call to `disarm()`."""
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(0)
    await state.report_state_from_esp32(armed=False)

    assert state._entry_delay_task is None
    assert state._entry_delay_ends_at is None
    await asyncio.sleep(0.05)
    assert state.alarm_state == AlarmState.disarmed


async def test_the_watchdog_does_not_re_alarm_an_alarm_already_raised(state, instant_grace):
    """When the controller *is* alive it reports the expiry itself, and the
    watchdog wakes up a moment later into a system that is already
    `triggered` — it must find that and stop, not log a second alert."""
    await arm_now(state, ArmMode.away)
    await state.begin_entry_delay(0)
    await state.trigger_alert(SensorType.door, "not disarmed in time")
    alerts = [e for e in state.events if e.category == EventCategory.alert]

    await asyncio.wait_for(state._entry_delay_task, timeout=5)

    assert state.alarm_state == AlarmState.triggered
    assert [e for e in state.events if e.category == EventCategory.alert] == alerts


async def test_entry_delay_route_requires_the_device_key(client):
    assert (await client.post("/esp32/entry-delay", json={"seconds": 25})).status_code == 401


async def test_entry_delay_route_works(auth_client, device_headers, state):
    await state.arm(ArmMode.away)
    state._arming_task.cancel()
    state.alarm_state = AlarmState.armed

    response = await auth_client.post(
        "/esp32/entry-delay", json={"seconds": 25}, headers=device_headers
    )
    assert response.status_code == 200

    body = (await auth_client.get("/state")).json()
    assert body["alarm_state"] == "entryDelay"
    assert body["entry_delay_seconds_remaining"] > 0


# ---- Night-mode schedule ---------------------------------------------------


async def test_night_mode_off_does_nothing(state):
    """The scheduler must be inert unless the user turned it on."""
    assert state.night_mode is False
    await state._night_mode_tick(datetime(2026, 8, 7, 23, 0))
    assert state.alarm_state == AlarmState.disarmed


async def test_night_mode_arms_at_the_configured_hour(state):
    """The behaviour the UI has always claimed and never had."""
    await state.set_night_mode(True)
    await state._night_mode_tick(datetime(2026, 8, 7, 23, 0))

    # Night mode arms Home, and Home has no exit delay: armed at once.
    assert state.alarm_state == AlarmState.armed
    assert all(s.bypassed for s in state.sensors if s.type != SensorType.door)
    assert any("Night mode — arming automatically" in e.message for e in state.events)


async def test_night_mode_disarms_at_the_configured_hour(state):
    await state.set_night_mode(True)
    await arm_now(state, ArmMode.home)
    await state._night_mode_tick(datetime(2026, 8, 8, 7, 0))
    assert state.alarm_state == AlarmState.disarmed


async def test_night_mode_never_silences_a_live_alarm(state):
    """The most important restraint here. If the alarm is going off at 06:59,
    the schedule reaching 07:00 must not turn it off — only a human decides
    that an alert is over.
    """
    await state.set_night_mode(True)
    await arm_now(state, ArmMode.home)
    await state.trigger_alert(SensorType.door, "break-in")
    assert state.alarm_state == AlarmState.triggered

    await state._night_mode_tick(datetime(2026, 8, 8, 7, 0))
    assert state.alarm_state == AlarmState.triggered


async def test_night_mode_does_not_interrupt_an_entry_delay(state):
    """Someone walking in at exactly 07:00 should get their countdown, not a
    surprise auto-disarm that hides the entry from the log."""
    await state.set_night_mode(True)
    await arm_now(state, ArmMode.home)
    await state.begin_entry_delay(25)

    await state._night_mode_tick(datetime(2026, 8, 8, 7, 0))
    assert state.alarm_state == AlarmState.entryDelay


async def test_night_mode_only_acts_on_the_hour(state):
    """Otherwise a deliberate 23:30 disarm would be undone a minute later,
    and again every minute until midnight."""
    await state.set_night_mode(True)
    await state._night_mode_tick(datetime(2026, 8, 7, 23, 30))
    assert state.alarm_state == AlarmState.disarmed


async def test_night_mode_does_not_rearm_an_armed_system(state):
    """Arming is only attempted from `disarmed`, so hitting the arm hour
    while already armed is a no-op rather than a restarted exit delay."""
    await state.set_night_mode(True)
    await arm_now(state, ArmMode.away)
    await state._night_mode_tick(datetime(2026, 8, 7, 23, 0))
    assert state.alarm_state == AlarmState.armed


async def test_night_mode_hours_are_configurable_and_clamped(state):
    await state.set_night_mode_hours(1, 9)
    assert (state.night_mode_arm_hour, state.night_mode_disarm_hour) == (1, 9)

    # Out-of-range values are clamped rather than rejected, so an older or
    # buggy client can't put the schedule into an impossible state.
    await state.set_night_mode_hours(99, -5)
    assert (state.night_mode_arm_hour, state.night_mode_disarm_hour) == (23, 0)


async def test_night_mode_uses_the_configured_hours(state):
    await state.set_night_mode(True)
    await state.set_night_mode_hours(1, 9)

    await state._night_mode_tick(datetime(2026, 8, 7, 23, 0))
    assert state.alarm_state == AlarmState.disarmed  # no longer the arm hour

    await state._night_mode_tick(datetime(2026, 8, 8, 1, 0))
    assert state.alarm_state == AlarmState.armed  # Home arms at once


# ---- Escalation ------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    """Captures pushes (see test_notifications.py for the same pattern)."""
    calls: list[dict] = []

    async def fake_send(server, topic, title, message, priority, tags):
        calls.append({"title": title, "message": message, "priority": priority})

    from backend import notifier

    monkeypatch.setattr(notifier, "_send", fake_send)
    return calls


async def test_escalation_fires_if_the_alarm_is_not_cleared(state, sent, monkeypatch):
    """`police_call_delay_seconds` is how long an alarm may stay uncleared
    before the follow-up goes out. Nothing dials a phone: the follow-up is a
    notification (here over ntfy), and the stored phone number it used to
    quote is no longer part of the message."""
    await state.set_ntfy("https://ntfy.example", "topic")
    await state.set_police_call_delay(0)  # fire immediately

    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "break-in")

    import asyncio

    await asyncio.sleep(0.05)

    escalations = [c for c in sent if "NOT CLEARED" in c["title"]]
    assert len(escalations) == 1
    assert "Nobody has cleared it" in escalations[0]["message"]
    assert "Call" not in escalations[0]["message"]
    assert escalations[0]["priority"] == "urgent"


async def test_disarming_in_time_cancels_the_escalation(state, sent):
    """Acknowledging the alarm is exactly what should stop the follow-up."""
    import asyncio

    await state.set_ntfy("https://ntfy.example", "topic")
    await state.set_police_call_delay(1)

    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "break-in")
    await state.disarm()
    await asyncio.sleep(1.2)

    assert not any("NOT CLEARED" in c["title"] for c in sent)


async def test_a_controller_disarm_also_cancels_the_escalation(state, sent):
    """`disarm()` always cancelled the escalation; `report_state_from_esp32`
    did not — and that is the path a real disarm takes, since the door
    controller owns the keypad. So the ordinary case of someone coming home
    and typing their PIN still sent the urgent "alarm still active, nobody
    has dealt with it, call your emergency contact" push a minute later.
    """
    await state.set_ntfy("https://ntfy.example", "topic")
    await state.set_police_call_delay(1)

    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "break-in")
    await state.report_state_from_esp32(armed=False)
    assert state.alarm_state == AlarmState.disarmed

    await asyncio.sleep(1.2)
    assert not any("NOT CLEARED" in c["title"] for c in sent)


async def test_a_negative_escalation_delay_is_clamped(state, sent):
    """`asyncio.sleep()` returns immediately for a negative argument, so an
    unclamped negative delay fired the "nobody has dealt with this alarm"
    escalation in the same instant as the alarm — no window to disarm in at
    all. Its sibling, set_night_mode_hours, clamped from the start.
    """
    from backend.state import MAX_POLICE_CALL_DELAY

    await state.set_police_call_delay(-10)
    assert state.police_call_delay_seconds == 0

    await state.set_police_call_delay(999_999)
    assert state.police_call_delay_seconds == MAX_POLICE_CALL_DELAY


async def test_escalation_without_a_contact_still_notifies(state, sent):
    """No number configured is not a reason to stay silent about an alarm
    nobody has dealt with."""
    import asyncio

    await state.set_ntfy("https://ntfy.example", "topic")
    await state.set_police_call_delay(0)

    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "break-in")
    await asyncio.sleep(0.05)

    escalations = [c for c in sent if "NOT CLEARED" in c["title"]]
    assert len(escalations) == 1
    assert "Call" not in escalations[0]["message"]


async def test_a_lingering_face_is_logged_once_not_twice_a_second(state):
    """The ESP32-CAM pushes two frames a second.

    Someone simply standing in front of it used to produce two log lines and
    two full-state broadcasts *per second*: the 200-entry log filled with
    "Face recognized: X" in under two minutes — burying every arm, disarm and
    alert in it — and every connected phone was sent the whole ~21 KB
    snapshot each time, roughly 2.6 MB a minute, to be told the same thing
    repeatedly.
    """
    for _ in range(20):
        await state.log_face_recognized("Guy Bar")

    entries = [e for e in state.events if "Face recognized: Guy Bar" in e.message]
    assert len(entries) == 1, f"expected one entry for a continuous sighting, got {len(entries)}"


async def test_each_person_is_rate_limited_separately(state):
    """Two people in frame are two distinct facts, and both belong in the log."""
    await state.log_face_recognized("Guy Bar")
    await state.log_face_recognized("Yair Bonfil")
    await state.log_face_recognized("Guy Bar")

    assert len([e for e in state.events if "Face recognized" in e.message]) == 2


async def test_a_later_arrival_is_logged_again(state):
    """The limit debounces one continuous sighting; it must not swallow a
    genuinely separate arrival."""
    from datetime import datetime, timedelta

    from backend.state import FACE_LOG_COOLDOWN

    await state.log_face_recognized("Guy Bar")
    state._face_last_logged["Guy Bar"] = (
        datetime.now() - FACE_LOG_COOLDOWN - timedelta(seconds=1)
    )
    await state.log_face_recognized("Guy Bar")

    assert len([e for e in state.events if "Face recognized: Guy Bar" in e.message]) == 2


async def test_a_face_disarm_is_always_logged(state):
    """Rate-limiting must never hide the alarm being switched off. A disarm
    is logged even if that same face was recognised a moment earlier."""
    from backend.models import AlarmState, ArmMode

    await state.set_face_disarm_enabled(True)
    await state.log_face_recognized("Guy Bar")  # primes the cooldown

    await arm_now(state, ArmMode.away)
    disarmed = await state.log_face_recognized("Guy Bar")

    assert disarmed is True
    assert state.alarm_state == AlarmState.disarmed
    assert any("Disarmed by Face ID: Guy Bar" in e.message for e in state.events)
