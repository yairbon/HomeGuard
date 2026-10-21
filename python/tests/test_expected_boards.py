"""`HOMEGUARD_BOARDS`: which boards an install expects to hear from.

The default must stay "every board" — a board that is expected but never comes
up is what the app's SENSOR OFFLINE banner exists to show. Dropping one is an
opt-in, for the setup where the ultrasonic and vibration sensors share one
ESP32 and nothing will ever heartbeat as `vibration`.
"""

from __future__ import annotations

import logging

import pytest

EVERY_BOARD = ("ultrasonic", "vibration", "door_controller")


def _parse(raw: str) -> tuple[str, ...]:
    # Imported late: `backend.state` builds its singleton at import time, and
    # the `state` fixture is what points that at a temp directory first.
    from backend.state import expected_boards

    return expected_boards(raw)


@pytest.fixture
def one_board_install(monkeypatch):
    # Must run before `auth_client` (and so before `app_module` reloads the
    # backend): the board list is computed from the environment at import.
    monkeypatch.setenv("HOMEGUARD_BOARDS", "ultrasonic,door_controller")


def test_unset_or_blank_means_every_board(state):
    assert _parse("") == EVERY_BOARD
    assert _parse("  , ") == EVERY_BOARD


def test_a_one_board_install_drops_the_vibration_board(state):
    assert _parse("ultrasonic,door_controller") == ("ultrasonic", "door_controller")


def test_order_and_spacing_do_not_matter(state):
    assert _parse(" door_controller , ultrasonic ,, ") == ("ultrasonic", "door_controller")


def test_unknown_names_are_ignored_with_a_warning(state, caplog):
    with caplog.at_level(logging.WARNING, logger="state"):
        parsed = _parse("ultrasonic,toaster,door_controller")
    assert parsed == ("ultrasonic", "door_controller")
    assert "toaster" in caplog.text


def test_a_value_with_no_valid_name_falls_back_to_every_board(state, caplog):
    with caplog.at_level(logging.WARNING, logger="state"):
        assert _parse("toaster") == EVERY_BOARD
    assert "toaster" in caplog.text


async def test_default_install_reports_every_board(auth_client):
    response = await auth_client.get("/state")
    assert tuple(b["name"] for b in response.json()["boards"]) == EVERY_BOARD


async def test_state_lists_only_the_configured_boards(one_board_install, auth_client):
    response = await auth_client.get("/state")
    assert [b["name"] for b in response.json()["boards"]] == ["ultrasonic", "door_controller"]


async def test_a_vibration_alert_from_the_combined_board_marks_it_online(
    one_board_install, auth_client, device_headers
):
    await auth_client.post(
        "/esp32/sensor-event",
        params={"sensor": "vibration", "board": "ultrasonic", "reason": "knock"},
        headers=device_headers,
    )
    boards = {b["name"]: b for b in (await auth_client.get("/state")).json()["boards"]}
    assert boards["ultrasonic"]["online"] is True
    assert "vibration" not in boards


async def test_a_heartbeat_from_an_unexpected_board_is_ignored(
    one_board_install, auth_client, device_headers
):
    response = await auth_client.post(
        "/esp32/heartbeat", params={"board": "vibration"}, headers=device_headers
    )
    assert response.status_code == 200
    boards = {b["name"]: b for b in (await auth_client.get("/state")).json()["boards"]}
    assert "vibration" not in boards
