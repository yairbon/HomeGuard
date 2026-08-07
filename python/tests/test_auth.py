"""Authentication, session tokens, and the lockout.

This is the file that would have caught the original design flaw: `/login`
verified a password and then nothing ever checked identity again, so every
route was reachable by anyone who could open a socket to the backend.
"""

from __future__ import annotations

import pytest

from backend.security import TokenStore, hash_password, verify_password

from .conftest import PASSWORD

# Every route that mutates or exposes system state. Parameterized rather than
# written out one test at a time so a newly added route is a one-line change
# here — and so nobody can add an endpoint that quietly forgets its
# `Depends(require_app)`.
PROTECTED_ROUTES = [
    ("get", "/state", None),
    ("post", "/arm", {"mode": "away"}),
    ("post", "/disarm", None),
    ("post", "/trigger-alert", {"source": "door", "reason": "x"}),
    ("post", "/logout", None),
    ("post", "/change-password", {"old_password": PASSWORD, "new_password": "x"}),
    ("get", "/faces", None),
    ("post", "/faces", {"name": "x"}),
    ("post", "/face-id-enabled", {"enabled": True}),
    ("post", "/settings/night-mode", {"enabled": True}),
    ("post", "/settings/emergency-number", {"number": "100"}),
    ("post", "/settings/police-call-delay", {"seconds": 30}),
    ("post", "/debug/simulate-face-recognition", None),
]

DEVICE_ROUTES = [
    ("/esp32/heartbeat", {"board": "ultrasonic"}, None),
    ("/esp32/sensor-event", {"sensor": "door", "reason": "forged"}, None),
    ("/esp32/state-change", None, {"armed": False}),
]


async def _call(client, method: str, path: str, body):
    """Dispatches one route from the table above.

    `json=` is only passed for methods that carry a body — httpx's `get()`
    signature rejects the keyword outright rather than ignoring it.
    """
    if method == "get":
        return await client.get(path)
    return await getattr(client, method)(path, json=body)


@pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
async def test_protected_routes_reject_anonymous_callers(client, method, path, body):
    """No session token means no access — on every single route.

    The concrete scenario: someone on the same network (or Tailnet, or a
    forwarded port) POSTs /disarm at an armed alarm. That used to return 200.
    """
    response = await _call(client, method, path, body)
    assert response.status_code == 401, f"{method.upper()} {path} was reachable anonymously"


@pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES)
async def test_protected_routes_reject_a_bogus_token(client, method, path, body):
    """A well-formed but invented token must not work either — the check has
    to be a lookup against issued tokens, not merely "is a token present"."""
    client.headers["Authorization"] = "Bearer not-a-real-token"
    response = await _call(client, method, path, body)
    assert response.status_code == 401


@pytest.mark.parametrize("path,params,body", DEVICE_ROUTES)
async def test_esp32_routes_require_the_device_key(client, path, params, body):
    """The board endpoints are the other forgeable surface: without a key,
    anything on the network could fake a break-in — or fake a
    "system disarmed" report and make the app show a false all-clear."""
    assert (await client.post(path, params=params, json=body)).status_code == 401
    wrong = {"X-Device-Key": "wrong-key"}
    assert (await client.post(path, params=params, json=body, headers=wrong)).status_code == 401


@pytest.mark.parametrize("path,params,body", DEVICE_ROUTES)
async def test_esp32_routes_accept_the_device_key(client, device_headers, path, params, body):
    response = await client.post(path, params=params, json=body, headers=device_headers)
    assert response.status_code == 200


async def test_health_is_public(client):
    """The one route that must work without credentials, so a board or a
    human can tell whether the process is even up."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_login_issues_a_working_token(client):
    response = await client.post("/login", json={"password": PASSWORD})
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["token"], str) and len(body["token"]) > 20

    client.headers["Authorization"] = f"Bearer {body['token']}"
    assert (await client.get("/state")).status_code == 200


async def test_failed_login_issues_no_token(client):
    body = (await client.post("/login", json={"password": "wrong"})).json()
    assert body["ok"] is False
    assert body["token"] is None
    assert body["attempts_remaining"] == 2


async def test_logout_invalidates_the_token_server_side(auth_client):
    """Signing out has to kill the credential on the server. A logout that
    only flips a local flag leaves a token that can still disarm the alarm
    for the rest of its 12-hour life."""
    assert (await auth_client.get("/state")).status_code == 200
    await auth_client.post("/logout")
    assert (await auth_client.get("/state")).status_code == 401


async def test_password_change_revokes_other_sessions_but_not_your_own(client, app_module):
    """Changing an alarm's password is how you lock someone out. If their
    existing session survived it, that wouldn't work."""
    first = (await client.post("/login", json={"password": PASSWORD})).json()["token"]
    second = (await client.post("/login", json={"password": PASSWORD})).json()["token"]

    client.headers["Authorization"] = f"Bearer {first}"
    response = await client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": "new-secret"}
    )
    assert response.json()["ok"] is True

    # The session that made the change keeps working...
    assert (await client.get("/state")).status_code == 200
    # ...every other one does not.
    client.headers["Authorization"] = f"Bearer {second}"
    assert (await client.get("/state")).status_code == 401


async def test_change_password_enforces_the_same_lockout_as_login(auth_client, state):
    """`/change-password` used to count no attempts at all, so the 3-attempt
    lockout guarding `/login` could be sidestepped entirely by guessing the
    old password here instead — an unthrottled password oracle."""
    for i in range(3):
        response = await auth_client.post(
            "/change-password", json={"old_password": f"guess{i}", "new_password": "x"}
        )
        assert response.json()["ok"] is False

    assert state.is_locked_out is True
    # And the correct password is now refused too, exactly as after 3 bad logins.
    assert (await auth_client.post("/login", json={"password": PASSWORD})).json()["ok"] is False


async def test_lockout_after_three_failed_logins(client, state):
    for i in range(3):
        await client.post("/login", json={"password": f"nope{i}"})

    assert state.is_locked_out is True
    body = (await client.post("/login", json={"password": PASSWORD})).json()
    assert body["ok"] is False
    assert body["locked_out"] is True
    assert body["lockout_remaining_seconds"] > 0


async def test_lockout_expires(client, state, monkeypatch):
    """Uses a shifted clock instead of a real 30-second sleep — the point
    under test is the expiry comparison, not the ability to wait."""
    from datetime import datetime, timedelta

    for i in range(3):
        await client.post("/login", json={"password": f"nope{i}"})
    assert state.is_locked_out is True

    state._locked_until = datetime.now() - timedelta(seconds=1)
    assert state.is_locked_out is False
    assert (await client.post("/login", json={"password": PASSWORD})).json()["ok"] is True


async def test_blank_new_password_is_rejected(auth_client):
    """A whitespace-only password would leave the system effectively open."""
    response = await auth_client.post(
        "/change-password", json={"old_password": PASSWORD, "new_password": "   "}
    )
    assert response.json()["ok"] is False


# ---- Password hashing ------------------------------------------------------


def test_password_is_hashed_not_stored_in_plaintext(state):
    """The stored value must not be the password. It used to be, held as a
    bare string in memory and compared with `==`."""
    assert PASSWORD not in state._password_hash
    assert state._password_hash.startswith("scrypt$")


def test_hash_and_verify_round_trip():
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored) is True
    assert verify_password("wrong horse", stored) is False


def test_hashes_are_salted():
    """Two hashes of the same password must differ, or one rainbow table
    covers every install of this project."""
    assert hash_password("same") != hash_password("same")


def test_verify_fails_closed_on_a_corrupt_hash():
    """A mangled state file must lock everyone out, not let everyone in."""
    assert verify_password("anything", "not-a-valid-hash") is False
    assert verify_password("anything", "bcrypt$aa$bb") is False


# ---- Token store -----------------------------------------------------------


def test_tokens_are_unique():
    store = TokenStore()
    assert store.issue() != store.issue()


def test_expired_tokens_are_rejected():
    from datetime import datetime, timedelta

    store = TokenStore()
    token = store.issue()
    assert store.is_valid(token) is True

    store._tokens[token] = datetime.now() - timedelta(seconds=1)
    assert store.is_valid(token) is False


def test_is_valid_handles_missing_tokens():
    store = TokenStore()
    assert store.is_valid(None) is False
    assert store.is_valid("") is False
    assert store.is_valid("never-issued") is False
