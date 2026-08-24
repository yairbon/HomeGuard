"""Authentication, session tokens, and the lockout.

This is the file that would have caught the original design flaw: `/login`
verified a password and then nothing ever checked identity again, so every
route was reachable by anyone who could open a socket to the backend.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

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
    ("delete", "/faces/1", None),
    ("post", "/faces/1/photo", None),
    ("get", "/camera/latest", None),
    ("get", "/camera/stream", None),
    ("post", "/face-id-enabled", {"enabled": True}),
    ("post", "/face-disarm-enabled", {"enabled": True}),
    ("post", "/settings/night-mode", {"enabled": True}),
    ("post", "/settings/night-mode-hours", {"arm_hour": 23, "disarm_hour": 7}),
    ("post", "/settings/emergency-number", {"number": "100"}),
    ("post", "/settings/police-call-delay", {"seconds": 30}),
    ("post", "/settings/keypad-pin", {"pin": "1913"}),
    ("post", "/settings/ntfy", {"server": "https://ntfy.sh", "topic": "x"}),
    ("post", "/settings/ntfy/test", None),
    ("post", "/debug/simulate-face-recognition", None),
]

DEVICE_ROUTES = [
    ("/esp32/heartbeat", {"board": "ultrasonic"}, None),
    ("/esp32/sensor-event", {"sensor": "door", "reason": "forged"}, None),
    ("/esp32/state-change", None, {"armed": False}),
    ("/esp32/entry-delay", None, {"seconds": 25}),
]

# Reachable with *either* credential, so it belongs to neither table above —
# but it still must reject a caller holding neither.
EITHER_CREDENTIAL_ROUTES = ["/camera/recognize"]

# Open by design. Anything not in here and not in the tables above is a route
# somebody forgot to cover; see test_every_route_is_covered_by_a_table.
#
# The FastAPI docs routes are Starlette `Route`s rather than `APIRoute`s, so
# they never reach that test — worth knowing that `/openapi.json`, `/docs` and
# `/redoc` are also anonymous, which means the full API schema is public even
# though three READMEs claim `/health` is the only open endpoint.
PUBLIC_ROUTES = {"/health", "/login"}


async def _call(client, method: str, path: str, body):
    """Dispatches one route from the table above.

    `json=` is only passed for methods that carry a body — httpx's `get()`
    and `delete()` signatures reject the keyword outright rather than
    ignoring it.
    """
    if method in ("get", "delete"):
        return await getattr(client, method)(path)
    return await getattr(client, method)(path, json=body)


def _table_paths() -> set[str]:
    """Every path the tables in this module claim to cover, with the sample
    ids swapped back for their templates so they match the route objects."""
    covered = {path for _, path, _ in PROTECTED_ROUTES}
    covered |= {path for path, _, _ in DEVICE_ROUTES}
    covered |= set(EITHER_CREDENTIAL_ROUTES)
    covered |= PUBLIC_ROUTES
    return {
        path.replace("/faces/1/photo", "/faces/{face_id}/photo").replace(
            "/faces/1", "/faces/{face_id}"
        )
        for path in covered
    }


def test_every_route_is_covered_by_a_table():
    """The drift guard on the tables above.

    Those tables are the project's stated defence against shipping an
    endpoint that forgot its `Depends(require_app)` (CLAUDE.md says as much),
    but they are hand-maintained — and by the time this test was written they
    had silently fallen eight routes behind, including `/face-disarm-enabled`
    (which controls whether a photograph can disarm the alarm) and
    `/esp32/entry-delay` (which can push the system into its disarm
    countdown). Nothing was actually unguarded, but nothing would have said
    so if it had been.

    So: enumerate the app's own routes and require each to appear somewhere.
    Adding a route now forces a deliberate choice — cover it, or declare it
    public — instead of relying on someone remembering.
    """
    from backend.main import app

    actual = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        # /ws authenticates inline (closes with 1008 before accept()) because
        # WebSockets can't carry an Authorization header; it isn't an
        # APIRoute anyway, but be explicit.
        and route.path != "/ws"
    }
    missing = actual - _table_paths()
    assert not missing, (
        f"These routes are in the app but in no table: {sorted(missing)}. "
        "Add each to PROTECTED_ROUTES / DEVICE_ROUTES / EITHER_CREDENTIAL_ROUTES, "
        "or to PUBLIC_ROUTES if it is genuinely meant to be open."
    )


@pytest.mark.parametrize("path", EITHER_CREDENTIAL_ROUTES)
async def test_either_credential_routes_reject_no_credential(client, path):
    """`require_app_or_device` accepts two kinds of caller — and must still
    refuse a third kind that holds neither."""
    response = await client.post(path, files={"photo": ("f.jpg", b"x", "image/jpeg")})
    assert response.status_code == 401


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


async def test_one_typo_after_a_lockout_expires_does_not_re_lock_and_re_alarm(
    client, state, monkeypatch
):
    """The counter has to be cleared when the lockout is, not only on success.

    `_failed_attempts` used to stay latched at MAX_LOGIN_ATTEMPTS once the 30
    seconds elapsed, so the user's next single mistyped character hit
    `>= MAX` immediately: re-locked for another 30 seconds *and* — because
    `_register_failed_attempt` escalates — `trigger_alert` fired. On an armed
    house that means the siren, the buzzer command to the door controller,
    and an urgent push, all from one typo, in a loop the user could not
    escape at better than one mistake per cycle.
    """
    from datetime import datetime, timedelta

    from backend.models import AlarmState

    for i in range(3):
        await client.post("/login", json={"password": f"nope{i}"})
    assert state.is_locked_out is True

    state._locked_until = datetime.now() - timedelta(seconds=1)

    body = (await client.post("/login", json={"password": "one-typo"})).json()

    assert body["locked_out"] is False, "a single wrong password must not re-lock"
    assert body["attempts_remaining"] == 2, "the user gets a full fresh allowance"
    assert state.alarm_state != AlarmState.triggered


async def test_attempts_remaining_is_coherent_once_a_lockout_has_expired(client, state):
    """`attempts_remaining: 0` alongside `locked_out: false` is a contradiction
    the app rendered as "0 attempts left" on a system that was in fact
    accepting logins."""
    from datetime import datetime, timedelta

    for i in range(3):
        await client.post("/login", json={"password": f"nope{i}"})

    state._locked_until = datetime.now() - timedelta(seconds=1)
    state._clear_expired_lockout()

    assert state.is_locked_out is False
    assert state.attempts_remaining == 3


async def test_an_expired_lockout_cannot_be_cleared_early(state):
    """The reset is a no-op while the lockout is still running — otherwise it
    would be a way to serve none of it."""
    from datetime import datetime, timedelta

    for i in range(3):
        await state.login(f"nope{i}")
    state._locked_until = datetime.now() + timedelta(seconds=30)

    state._clear_expired_lockout()

    assert state.is_locked_out is True
    assert state.attempts_remaining == 0


# ---- Keeping the event loop free -------------------------------------------


async def _count_loop_ticks_during(coro):
    """Runs `coro` while a second task ticks every 10ms, and returns how many
    ticks got through.

    The whole backend is one event loop — it also holds every app's WebSocket
    and answers all four boards — so "did this run off the loop?" is a
    behavioural question, not a stylistic one, and this is what asks it.
    """
    import asyncio

    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        result = await coro
    finally:
        ticker.cancel()
    return result, ticks


async def test_login_does_not_block_the_event_loop(state, monkeypatch):
    """scrypt is deliberately expensive — ~50ms per verification, and
    `change_password` pays it twice (~150ms). Run on the event loop, that is
    150ms in which no alarm state reaches any connected phone, on a path that
    by design allows three attempts before any lockout applies.
    """
    import time

    from backend import state as state_module

    def slow_verify(password, stored):
        time.sleep(0.2)  # stands in for scrypt, exaggerated so it's decidable
        return True

    monkeypatch.setattr(state_module, "verify_password", slow_verify)

    token, ticks = await _count_loop_ticks_during(state.login("homeguard"))

    assert token is not None
    assert ticks >= 5, f"the loop stalled for the whole verification ({ticks} ticks)"


async def test_changing_the_password_does_not_block_the_event_loop(state, monkeypatch):
    import time

    from backend import state as state_module

    def slow_verify(password, stored):
        time.sleep(0.1)
        return True

    def slow_hash(password):
        time.sleep(0.1)
        return "scrypt$00$00"

    monkeypatch.setattr(state_module, "verify_password", slow_verify)
    monkeypatch.setattr(state_module, "hash_password", slow_hash)

    ok, ticks = await _count_loop_ticks_during(state.change_password(PASSWORD, "new-one"))

    assert ok is True
    assert ticks >= 5, f"the loop stalled for the whole change ({ticks} ticks)"
