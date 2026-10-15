"""What a brand-new install looks like, and the defaults that used to be demo-grade.

- The first password is the door keypad's own PIN when the backend can read it
  from the board's secrets.h, so app password and keypad PIN are one secret
  from the first minute (they already were after the first password change).
- A fresh install has no approved faces (it used to ship two hardcoded names
  with no photos).
- The interactive API docs are off unless asked for; they have no credential.
- A missing notification channel is logged as such, not as a sent notification.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from backend.models import ArmMode, SensorType

from .test_alarm_flow import arm_now

HEADER = '#define WIFI_SSID "x"\n#define KEYPAD_PIN "{pin}"\n#define DEVICE_KEY "k"\n'


@pytest.fixture
def keypad_header(tmp_path, monkeypatch):
    """Points the backend at a door-controller secrets.h with a known PIN.

    Must be requested before `client`/`state`: the first password is chosen
    when the reloaded AppState starts, which is after this runs.
    """
    from backend import secrets_file

    header = tmp_path / "secrets.h"
    header.write_text(HEADER.format(pin="4821"), encoding="utf-8")
    monkeypatch.setattr(secrets_file, "SECRETS_FILE", str(header))
    return header


async def test_first_password_is_the_keypad_pin(keypad_header, client, state):
    assert (await client.post("/login", json={"password": "4821"})).status_code == 200
    # ...and the well-known factory default no longer works on this install.
    assert (await client.post("/login", json={"password": "homeguard"})).json()["token"] is None
    # The PIN the board already has is what the backend remembers for it.
    assert state.keypad_pin == "4821"


@pytest.fixture
def unusable_header(tmp_path, monkeypatch):
    """A secrets.h whose KEYPAD_PIN is not 4-8 digits."""
    from backend import secrets_file

    header = tmp_path / "secrets.h"
    header.write_text(HEADER.format(pin="not-digits"), encoding="utf-8")
    monkeypatch.setattr(secrets_file, "SECRETS_FILE", str(header))
    return header


async def test_an_unusable_header_pin_falls_back_to_the_factory_default(
    unusable_header, client, state
):
    assert (await client.post("/login", json={"password": "homeguard"})).json()["token"]
    assert state.keypad_pin == ""


def test_reading_the_pin_without_a_configured_file_is_empty(monkeypatch):
    from backend import secrets_file

    monkeypatch.setattr(secrets_file, "SECRETS_FILE", "")
    assert secrets_file.read_keypad_pin() == ""


def test_reading_the_pin_from_a_missing_file_is_empty(tmp_path, monkeypatch):
    from backend import secrets_file

    monkeypatch.setattr(secrets_file, "SECRETS_FILE", str(tmp_path / "nope.h"))
    assert secrets_file.read_keypad_pin() == ""


async def test_the_saved_password_wins_over_the_header_on_later_starts(
    keypad_header, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", "test-device-key")
    from backend import state as state_module

    importlib.reload(state_module)
    assert await state_module.state.login("4821") is not None
    changed = await state_module.state.change_password("4821", "9999")
    assert changed is not None

    importlib.reload(state_module)  # a restart; the header still says 4821
    assert await state_module.state.login("9999") is not None
    assert await state_module.state.login("4821") is None


async def test_a_fresh_install_has_no_approved_faces(auth_client):
    assert (await auth_client.get("/faces")).json() == []


async def test_the_api_docs_are_off_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404, path


@pytest.fixture
def docs_on(monkeypatch):
    monkeypatch.setenv("HOMEGUARD_DOCS", "1")


async def test_the_api_docs_can_be_switched_on(docs_on, client):
    assert (await client.get("/openapi.json")).status_code == 200


async def test_escalation_is_not_logged_as_sent_without_a_channel(state):
    state.police_call_delay_seconds = 0
    assert state.ntfy_topic == ""
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await asyncio.sleep(0.05)  # lets the zero-delay escalation task run

    messages = [e.message for e in state.events]
    assert not any("escalation notification sent" in m for m in messages)
    assert any("escalation NOT sent" in m for m in messages)
