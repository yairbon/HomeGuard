"""Serving the built Flutter web app from the backend (one origin, one port).

`mount_web_app` is exercised on a throwaway FastAPI app rather than the real
one: whether the real app has a build to serve depends on what is on this
machine's disk, and a test should not.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
def build_dir(tmp_path):
    (tmp_path / "index.html").write_text("<html>homeguard web</html>")
    (tmp_path / "main.dart.js").write_text("// compiled app")
    return tmp_path


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _app_with_api() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


def test_nothing_is_mounted_without_a_build(app_module, tmp_path):
    app = _app_with_api()
    before = len(app.routes)
    assert app_module.mount_web_app(app, tmp_path) is False
    assert len(app.routes) == before


def test_nothing_is_mounted_for_a_missing_directory(app_module, tmp_path):
    assert app_module.mount_web_app(_app_with_api(), tmp_path / "nope") is False


async def test_the_build_is_served_from_the_root(app_module, build_dir):
    app = _app_with_api()
    assert app_module.mount_web_app(app, build_dir) is True
    async with _client(app) as client:
        index = await client.get("/")
        asset = await client.get("/main.dart.js")
    assert index.status_code == 200
    assert "homeguard web" in index.text
    assert asset.status_code == 200
    assert "compiled app" in asset.text


async def test_api_routes_registered_first_still_win(app_module, build_dir):
    app = _app_with_api()
    app_module.mount_web_app(app, build_dir)
    async with _client(app) as client:
        response = await client.get("/health")
    assert response.json() == {"ok": True}


async def test_unknown_paths_are_a_404_not_the_index_page(app_module, build_dir):
    app = _app_with_api()
    app_module.mount_web_app(app, build_dir)
    async with _client(app) as client:
        response = await client.get("/not-a-route")
    assert response.status_code == 404
    assert "homeguard web" not in response.text


async def test_the_app_is_always_revalidated_so_a_rebuild_shows_up(app_module, build_dir):
    app = _app_with_api()
    app_module.mount_web_app(app, build_dir)
    async with _client(app) as client:
        for path in ("/", "/main.dart.js"):
            response = await client.get(path)
            assert response.headers["cache-control"] == "no-cache", path


# ---- A second backend must never share the real photo folder ---------------------


def test_the_real_install_keeps_its_photos_in_python_faces(app_module, tmp_path, monkeypatch):
    monkeypatch.delenv("HOMEGUARD_FACES_DIR", raising=False)
    base = tmp_path / "python"
    assert app_module.default_faces_dir(base / "state.json", base) == base / "faces"


def test_a_backend_with_its_own_state_gets_its_own_photo_folder(app_module, tmp_path, monkeypatch):
    """The regression: a simulation with a temporary state file deleted the real
    enrolled face because both used python/faces."""
    monkeypatch.delenv("HOMEGUARD_FACES_DIR", raising=False)
    base = tmp_path / "python"
    throwaway = tmp_path / "sim" / "state.json"
    chosen = app_module.default_faces_dir(throwaway, base)
    assert chosen == tmp_path / "sim" / "faces"
    assert chosen != base / "faces"


def test_the_photo_folder_can_be_set_explicitly(app_module, tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEGUARD_FACES_DIR", str(tmp_path / "elsewhere"))
    assert app_module.default_faces_dir(tmp_path / "state.json", tmp_path / "python") == (
        tmp_path / "elsewhere"
    )
