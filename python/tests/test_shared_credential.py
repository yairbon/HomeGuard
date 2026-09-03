"""Changing the app's login password AND the physical keypad's disarm PIN —
now the same action, because they are now the same value.

Until this file was renamed from test_keypad_pin.py, these were two
independent endpoints guarding two independent secrets: a free-form app
password, and a separately-set 4-8 digit keypad PIN. They were merged into
one shared secret at the project owner's explicit request — see
AppState.change_password for the full account, including the security
trade-off that merge accepts (the app password — which now also gates
remote arm/disarm, the camera feed, and every setting — is bound by the
keypad's stricter, digits-only shape, not the other way around).
`/settings/keypad-pin` no longer exists; `/change-password` does both jobs
in one call.

The feature still spans three places, and it is still worth being precise
about which one holds the truth:

  * the door controller's **NVS** — the live PIN. The board reads it at boot
    and compares against it. This is the only copy that decides whether a
    keypress disarms the alarm.
  * the **backend** — `_password_hash` (what /login compares against) and
    `keypad_pin` (the same value, kept in the clear because it has to be
    relayed to hardware) — a stored copy, so the change survives a restart
    and can be re-sent to a board that was offline when it was made.
  * **secrets.h** — the compile-time default, kept in sync only so that
    reflashing a board doesn't silently hand it back an old PIN. Rewriting
    it changes nothing about a board that is already running.

These tests cover the backend's half and the file sync. The NVS half lives in
`esp32/door_keypad_controller/` and is compile-verified only — there is no
board in CI.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from backend.security import verify_password

from .conftest import DEVICE_KEY, PASSWORD

VALID_PIN = "4271"


@pytest.fixture
def secrets_header(tmp_path: Path) -> Path:
    """A stand-in secrets.h with the same shape as the real template."""
    header = tmp_path / "secrets.h"
    header.write_text(
        "#ifndef HOMEGUARD_SECRETS_H\n"
        "#define HOMEGUARD_SECRETS_H\n"
        '#define WIFI_SSID     "free-tau"\n'
        '#define WIFI_PASSWORD "not-the-real-one"\n'
        '#define BACKEND_HOST "192.168.1.10"\n'
        "#define BACKEND_PORT 8000\n"
        '#define DEVICE_KEY "some-device-key"\n'
        '#define KEYPAD_PIN "1913"  // trailing comment\n'
        "#endif\n",
        encoding="utf-8",
    )
    return header


async def test_changing_the_password_stores_and_forwards_the_new_pin(
    auth_client, state, monkeypatch
):
    """The happy path: both representations updated here, and the plaintext
    pushed to the board — proving this one call really does both jobs, not
    just the login half.
    """
    from backend import esp32_client

    sent = []

    async def fake_send(pin: str) -> bool:
        sent.append(pin)
        return True

    monkeypatch.setattr(esp32_client, "send_keypad_pin", fake_send)

    response = await auth_client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": VALID_PIN}
    )
    body = response.json()

    assert response.status_code == 200
    assert body == {"ok": True, "hardware": True, "secrets_updated": False}
    assert sent == [VALID_PIN], "the new value must reach the door controller, not just be stored"
    assert state.keypad_pin == VALID_PIN
    assert verify_password(VALID_PIN, state._password_hash) is True, (
        "the login half must accept the same value the keypad half was just given"
    )


async def test_an_unreachable_board_keeps_the_change_instead_of_losing_it(auth_client, state):
    """With no HOMEGUARD_DOOR_CONTROLLER_URL configured the push is a no-op.

    The credential change itself must still go through — on both the login
    side and the keypad side — and the app must be told plainly that the
    hardware did not take it. The alternative (treating an unreached board
    as a failed change) would leave the user believing their password/PIN
    never changed at all, when in fact the app already accepts the new
    value; only the physical keypad is still running the old one.
    """
    response = await auth_client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": VALID_PIN}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["hardware"] is False
    assert state.keypad_pin == VALID_PIN
    assert verify_password(VALID_PIN, state._password_hash) is True


async def test_hardware_and_secrets_fields_are_false_when_the_change_is_rejected(auth_client):
    """`hardware`/`secrets_updated` describe a hardware round trip that only
    happens once the credential itself is accepted. A caller must never be
    able to read `hardware: true` (or `secrets_updated: true`) out of a
    response whose `ok` is false — that would claim a board took a PIN that
    was, in fact, never sent.
    """
    response = await auth_client.post(
        "/change-password", json={"old_password": "not-the-password", "new_password": VALID_PIN}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": False, "hardware": False, "secrets_updated": False}


@pytest.mark.parametrize(
    "pin,why",
    [
        ("123", "too short"),
        ("123456789", "too long"),
        ("", "empty"),
        ("   ", "whitespace-only"),
        ("12a4", "not all digits"),
        ("12*4", "contains a keypad control key"),
        ("12 4", "contains a space"),
        ("١٢٣٤", "Unicode digits str.isdigit() accepts but the keypad can't send"),
        ('1"9\n#define WIFI_PASSWORD "x', "a C preprocessor injection attempt"),
    ],
)
async def test_invalid_new_values_are_rejected(auth_client, state, pin, why):
    """422 and no state change, for every shape the keypad could not
    produce — since that shape is now also the only one a new login
    password may take.

    Folds in what used to be test_auth.py's dedicated blank-password check
    (`""`, `"   "`): there is only one format rule now, so there is only one
    place that tests it.

    The last case is the one with teeth: this value is interpolated into a
    `#define` in a C++ header when secrets syncing is on, so 'digits only'
    is a sanitizer, not just a usability rule.
    """
    response = await auth_client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": pin}
    )

    assert response.status_code == 422, why
    assert state.keypad_pin == "", f"{why!r} must not have been stored"
    assert verify_password(PASSWORD, state._password_hash) is True, (
        f"{why!r} must not have changed the login password either"
    )


async def test_the_value_never_enters_the_event_log(auth_client, state):
    """Same rule the ntfy topic follows: the log is shown in the app,
    exported, and read by whoever has a session — a secret in it is a
    secret leaked. Neither the old value nor the new one should appear.
    """
    await auth_client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": VALID_PIN}
    )

    log = " ".join(event.message for event in state.events)
    assert VALID_PIN not in log
    assert PASSWORD not in log
    assert "Password changed" in log, "the change itself should still be auditable"


async def test_the_value_is_not_broadcast_in_the_state_payload(auth_client):
    """Deliberately absent from FullState. Putting it there would push the
    shared secret to every connected client on every state change.
    """
    await auth_client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": VALID_PIN}
    )
    body = (await auth_client.get("/state")).json()

    assert "keypad_pin" not in body
    assert "password" not in body
    assert VALID_PIN not in json.dumps(body)


async def test_the_change_survives_a_restart(monkeypatch, tmp_path):
    """Persisted, so a board that was offline can be re-sent the right PIN
    after the backend comes back — and so /login keeps accepting the new
    value — rather than either half of the change being lost.
    """
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    await state_module.state.change_password(PASSWORD, VALID_PIN)

    importlib.reload(state_module)
    assert state_module.state.keypad_pin == VALID_PIN
    assert verify_password(VALID_PIN, state_module.state._password_hash) is True


async def test_a_hand_edited_pin_in_the_state_file_is_re_validated(monkeypatch, tmp_path):
    """state.json is a plain file on disk that a user may well have opened.

    A malformed value there must not survive load and get relayed to
    hardware — `keypad_pin` is the one half of this pair that leaves the
    process. (`_password_hash` isn't exercised here: a mangled hash instead
    fails closed on the next /login — see
    test_verify_fails_closed_on_a_corrupt_hash in test_auth.py — a
    different, already-covered guarantee.)
    """
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(state_file))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)
    await state_module.state.change_password(PASSWORD, VALID_PIN)

    stored = json.loads(state_file.read_text(encoding="utf-8"))
    stored["keypad_pin"] = 'oops"; #define KEYPAD_PIN "0000'
    state_file.write_text(json.dumps(stored), encoding="utf-8")

    importlib.reload(state_module)
    assert state_module.state.keypad_pin == ""


# ---- secrets.h syncing ---------------------------------------------------
#
# secrets_file.py itself is untouched by the password/PIN merge — these are
# unchanged from test_keypad_pin.py, just relocated.


def test_secrets_syncing_is_off_unless_configured(monkeypatch):
    """Writing into a source tree only makes sense on a dev machine, so it
    must not happen just because the backend is running somewhere."""
    monkeypatch.delenv("HOMEGUARD_SECRETS_H", raising=False)

    from backend import secrets_file

    importlib.reload(secrets_file)

    assert secrets_file.is_enabled() is False
    assert secrets_file.update_keypad_pin(VALID_PIN) is False


def test_syncing_rewrites_only_the_pin_line(monkeypatch, secrets_header):
    """Everything else in that file — the WiFi password, the device key — has
    to come through byte-identical."""
    monkeypatch.setenv("HOMEGUARD_SECRETS_H", str(secrets_header))

    from backend import secrets_file

    importlib.reload(secrets_file)
    before = secrets_header.read_text(encoding="utf-8").splitlines()

    assert secrets_file.update_keypad_pin(VALID_PIN) is True

    after = secrets_header.read_text(encoding="utf-8").splitlines()
    assert f'#define KEYPAD_PIN "{VALID_PIN}"  // trailing comment' in after
    assert '#define KEYPAD_PIN "1913"' not in "\n".join(after)
    # Every other line untouched, including the two other secrets.
    assert [line for line in before if "KEYPAD_PIN" not in line] == [
        line for line in after if "KEYPAD_PIN" not in line
    ]


def test_syncing_reports_failure_rather_than_raising(monkeypatch, tmp_path):
    """A missing or shapeless header must not turn a change that already
    reached the board into an error — the caller surfaces the False instead.
    """
    from backend import secrets_file

    monkeypatch.setenv("HOMEGUARD_SECRETS_H", str(tmp_path / "does-not-exist.h"))
    importlib.reload(secrets_file)
    assert secrets_file.update_keypad_pin(VALID_PIN) is False

    without_pin = tmp_path / "no-pin.h"
    without_pin.write_text('#define WIFI_SSID "x"\n', encoding="utf-8")
    monkeypatch.setenv("HOMEGUARD_SECRETS_H", str(without_pin))
    importlib.reload(secrets_file)
    assert secrets_file.update_keypad_pin(VALID_PIN) is False
    assert without_pin.read_text(encoding="utf-8") == '#define WIFI_SSID "x"\n'


async def test_the_route_reports_whether_the_header_was_synced(
    auth_client, monkeypatch, secrets_header
):
    """The app distinguishes several outcomes, so the route has to report
    `hardware` and `secrets_updated` as their own booleans alongside `ok`,
    not fold everything into one flag."""
    monkeypatch.setenv("HOMEGUARD_SECRETS_H", str(secrets_header))

    from backend import secrets_file

    importlib.reload(secrets_file)

    body = (
        await auth_client.post(
            "/change-password", json={"old_password": PASSWORD, "new_password": VALID_PIN}
        )
    ).json()

    assert body["secrets_updated"] is True
    assert f'"{VALID_PIN}"' in secrets_header.read_text(encoding="utf-8")


async def test_changing_the_credential_requires_a_session(client):
    """Covered by the table in test_auth.py too; asserted here as well
    because this is the route that changes what opens the front door *and*
    what unlocks the app.
    """
    response = await client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": VALID_PIN}
    )
    assert response.status_code == 401

    await client.post("/login", json={"password": PASSWORD})
    # Still 401: logging in doesn't install the token on the client itself.
    assert (
        await client.post(
            "/change-password", json={"old_password": PASSWORD, "new_password": VALID_PIN}
        )
    ).status_code == 401
