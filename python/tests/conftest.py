"""Shared fixtures.

The backend keeps its state in a module-level singleton (`state` in
`backend.state`) and its recognizer in another (`faces` in `backend.main`),
both built at import time. That's a reasonable shape for a single-household
service, but it means tests must actively reset that singleton between cases
or they leak into each other — a password changed in one test would otherwise
break every later login.

Every fixture here exists to make each test start from a known-clean install:
its own temp state file, its own faces directory, default password, no
tokens.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

DEVICE_KEY = "test-device-key"
PASSWORD = "homeguard"


@pytest.fixture(autouse=True)
def no_real_alerts(tmp_path, monkeypatch):
    """Every test starts with no alert channel, whatever `alerts.local.json`
    says on this machine: a test that raises an alarm would otherwise send a
    real email or WhatsApp message to the owner."""
    from backend import alerts

    monkeypatch.setattr(alerts, "CONFIG_FILE", Path(tmp_path) / "no-alerts.local.json")


@pytest.fixture
def app_module(tmp_path: pytest.TempPathFactory, monkeypatch):
    """Reimports the backend with its persistence and device key pointed at a
    temp directory, and returns the reloaded `backend.main` module.

    A reload (rather than just mutating the existing singleton) is what makes
    each test a genuinely fresh install: `AppState.__init__` is where the
    default password is hashed, the seed faces are created and the state file
    is read, so re-running it is the only way to exercise that startup path
    and to guarantee nothing carries over.

    The env vars are set before the reload because both are read at import
    time — `HOMEGUARD_STATE_FILE` when `backend.state` computes STATE_FILE,
    and `HOMEGUARD_DEVICE_KEY` when the reloaded AppState resolves its key.
    """
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(Path(tmp_path) / "state.json"))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", DEVICE_KEY)

    from backend import state as state_module

    importlib.reload(state_module)

    from backend import esp32_client
    from backend import main as main_module

    # esp32_client caches `from .state import state`, so it has to be reloaded
    # too or it keeps commanding the previous test's state object.
    importlib.reload(esp32_client)
    importlib.reload(main_module)

    # Point the recognizer's training photos at the temp dir as well, so a
    # test that enrolls a face doesn't write into the developer's real
    # python/faces/ directory.
    from backend.face_recognition_service import FaceRecognitionService

    main_module.faces = FaceRecognitionService(
        faces_dir=Path(tmp_path) / "faces",
        models_dir=Path(__file__).resolve().parent.parent / "models",
    )
    return main_module


@pytest.fixture
def state(app_module):
    """The freshly-reloaded AppState singleton under test."""
    return app_module.state


@pytest.fixture
async def client(app_module):
    """An httpx client wired straight to the ASGI app — no uvicorn, no socket,
    no port.

    This is what makes the suite fast and hermetic where `simulate.py` is
    slow: requests are dispatched in-process, so there's nothing to start up,
    nothing to wait for, and no chance of colliding with a real backend the
    developer happens to have running on port 8000.
    """
    import httpx

    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", trust_env=False
    ) as c:
        yield c


@pytest.fixture
async def auth_client(client):
    """A client that has already logged in, with the session token installed
    on its default headers — the starting point for any test about behavior
    rather than about authentication itself.
    """
    response = await client.post("/login", json={"password": PASSWORD})
    token = response.json()["token"]
    assert token, "login should succeed with the default password on a fresh install"
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
def device_headers():
    """Headers presenting the shared ESP32 device key."""
    return {"X-Device-Key": DEVICE_KEY}
