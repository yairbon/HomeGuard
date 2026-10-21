"""What the backend prints to its console (it may be filmed or shared).

Routine traffic (heartbeats, camera frames) is left out so the lines that matter
stay readable, and a session token in a log line never survives.
"""

from __future__ import annotations

import logging


def _access(path: str) -> logging.LogRecord:
    # uvicorn's access record: (client, method, path, http version, status)
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("192.168.68.123:5000", "POST", path, "1.1", 200), None,
    )


def test_routine_requests_are_left_out_of_the_console(app_module):
    quiet = app_module._QuietRoutineTraffic()
    assert not quiet.filter(_access("/esp32/heartbeat?board=door_controller"))
    assert not quiet.filter(_access("/camera/recognize"))
    assert not quiet.filter(_access("/camera/latest"))


def test_everything_that_matters_still_shows(app_module):
    quiet = app_module._QuietRoutineTraffic()
    shown = ("/arm", "/disarm", "/login", "/esp32/sensor-event?sensor=door", "/esp32/state-change")
    for path in shown:
        assert quiet.filter(_access(path)), path


def test_a_session_token_never_reaches_a_log_line(app_module):
    redact = app_module._RedactSessionToken()
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1,
        '%s - "WebSocket %s" [accepted]', ("127.0.0.1:60327", "/ws?token=SECRETSESSIONTOKEN"), None,
    )
    assert redact.filter(record)
    assert "SECRETSESSIONTOKEN" not in record.getMessage()
    assert "token=***" in record.getMessage()


async def test_events_are_echoed_to_the_console(state, caplog):
    with caplog.at_level(logging.INFO, logger="state"):
        await state.set_night_mode(True)
    assert any("Night mode enabled" in message for message in caplog.messages)
