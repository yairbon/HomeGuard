"""Tests for ip_check.py — the startup warning for a board's secrets.h
BACKEND_HOST drifting out of step with this machine's current LAN IP (the
project's most repeated hardware-bringup problem; see the module docstring).

`check_board_secrets` and `log_stale_board_ips` never touch the real
esp32/ directory here: each test builds its own throwaway one under
`tmp_path`, the same isolation pattern the rest of this suite uses for
state.json.
"""

from __future__ import annotations

import ipaddress
import logging

import pytest

from backend.ip_check import BOARD_DIRS, check_board_secrets, get_lan_ip, log_stale_board_ips


def _write_secrets(esp32_dir, board: str, backend_host: str | None) -> None:
    board_dir = esp32_dir / board
    board_dir.mkdir(parents=True, exist_ok=True)
    if backend_host is None:
        (board_dir / "secrets.h").write_text('#define WIFI_SSID "x"\n', encoding="utf-8")
    else:
        (board_dir / "secrets.h").write_text(
            f'#define WIFI_SSID "x"\n'
            f'#define BACKEND_HOST "{backend_host}"\n'
            f"#define BACKEND_PORT 8000\n",
            encoding="utf-8",
        )


def test_get_lan_ip_returns_a_plausible_address_or_none():
    """Can't control the real network from a test, so this only checks the
    function never raises and, when it does return something, that it's a
    real dotted-quad — not that it's any *particular* address.
    """
    ip = get_lan_ip()
    if ip is not None:
        ipaddress.IPv4Address(ip)  # raises ValueError if this isn't one


def test_matching_host_is_reported_as_matching(tmp_path):
    _write_secrets(tmp_path, "ultrasonic_sensor", "10.0.0.5")

    results = check_board_secrets(tmp_path, current_ip="10.0.0.5")

    assert len(results) == 1
    assert results[0].board == "ultrasonic_sensor"
    assert results[0].configured_host == "10.0.0.5"
    assert results[0].matches_current_ip is True


def test_stale_host_is_reported_as_not_matching(tmp_path):
    _write_secrets(tmp_path, "esp32_cam", "10.0.0.5")

    results = check_board_secrets(tmp_path, current_ip="10.0.0.99")

    assert len(results) == 1
    assert results[0].matches_current_ip is False


def test_a_board_with_no_secrets_h_is_silently_skipped(tmp_path):
    """A fresh checkout (or CI) never creates the gitignored secrets.h files
    at all — that must not look like every board is stale."""
    (tmp_path / "door_keypad_controller").mkdir()

    results = check_board_secrets(tmp_path, current_ip="10.0.0.5")

    assert results == []


def test_a_secrets_h_with_no_backend_host_line_is_skipped(tmp_path):
    _write_secrets(tmp_path, "vibration_sensor", backend_host=None)

    results = check_board_secrets(tmp_path, current_ip="10.0.0.5")

    assert results == []


def test_checks_every_known_board_independently(tmp_path):
    _write_secrets(tmp_path, "ultrasonic_sensor", "10.0.0.5")
    _write_secrets(tmp_path, "vibration_sensor", "10.0.0.99")
    # door_keypad_controller and esp32_cam left unwritten — missing entirely.

    results = check_board_secrets(tmp_path, current_ip="10.0.0.5")

    by_board = {r.board: r.matches_current_ip for r in results}
    assert by_board == {"ultrasonic_sensor": True, "vibration_sensor": False}
    assert set(BOARD_DIRS) == {
        "ultrasonic_sensor",
        "vibration_sensor",
        "door_keypad_controller",
        "esp32_cam",
    }


def test_logs_nothing_above_info_when_every_board_matches(tmp_path, caplog):
    _write_secrets(tmp_path, "ultrasonic_sensor", "10.0.0.5")
    _write_secrets(tmp_path, "esp32_cam", "10.0.0.5")

    with caplog.at_level(logging.INFO, logger="ip_check"):
        log_stale_board_ips(esp32_dir=tmp_path, current_ip="10.0.0.5")

    assert all(record.levelno < logging.WARNING for record in caplog.records)
    assert any("matches" in record.message for record in caplog.records)


def test_warns_once_per_stale_board(tmp_path, caplog):
    _write_secrets(tmp_path, "ultrasonic_sensor", "10.0.0.5")  # fresh
    _write_secrets(tmp_path, "door_keypad_controller", "10.0.0.77")  # stale

    with caplog.at_level(logging.WARNING, logger="ip_check"):
        log_stale_board_ips(esp32_dir=tmp_path, current_ip="10.0.0.5")

    warnings = "\n".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
    assert "door_keypad_controller" in warnings
    assert "10.0.0.77" in warnings
    assert "10.0.0.5" in warnings
    assert "ultrasonic_sensor" not in warnings


def test_logs_nothing_when_no_board_has_a_secrets_file(tmp_path, caplog):
    """Nothing to compare against isn't the same as everything being stale —
    a fresh checkout must stay quiet, not fire 4 false warnings."""
    with caplog.at_level(logging.INFO, logger="ip_check"):
        log_stale_board_ips(esp32_dir=tmp_path, current_ip="10.0.0.5")

    assert caplog.records == []


def test_warns_but_does_not_raise_when_the_current_ip_cant_be_determined(
    tmp_path, caplog, monkeypatch
):
    import backend.ip_check as ip_check_module

    monkeypatch.setattr(ip_check_module, "get_lan_ip", lambda: None)
    _write_secrets(tmp_path, "ultrasonic_sensor", "10.0.0.5")

    with caplog.at_level(logging.WARNING, logger="ip_check"):
        log_stale_board_ips(esp32_dir=tmp_path)  # current_ip omitted -> auto-detect

    assert any("Could not determine" in r.message for r in caplog.records)


async def test_backend_startup_runs_the_check(app_module, monkeypatch):
    """The whole point is that this runs on every real startup without
    anyone remembering to invoke it separately — assert `lifespan` actually
    calls it, the same way test_shutdown_closes_every_outbound_http_client
    pins the client-cleanup side of the same function.
    """
    calls = []
    monkeypatch.setattr(app_module, "log_stale_board_ips", lambda: calls.append(True))

    async with app_module.lifespan(app_module.app):
        pass

    assert calls == [True]


@pytest.mark.parametrize("board", BOARD_DIRS)
def test_backend_host_regex_matches_the_real_secrets_h_style(tmp_path, board):
    """Uses the exact formatting esp32/secrets.h.example ships (extra
    indentation-driven spacing around the value), not just a minimal case."""
    _write_secrets(tmp_path, board, "192.168.1.42")

    results = check_board_secrets(tmp_path, current_ip="192.168.1.42")

    assert results[0].matches_current_ip is True
