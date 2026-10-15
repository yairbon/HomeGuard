"""Opt-in extra CORS origins, for serving the Flutter web build on its own port.

The default must stay exactly as tight as it was: only the backend's own
origin. `HOMEGUARD_CORS_ORIGINS` adds *exact* origins on top and never `*`.
"""

from __future__ import annotations

import logging

import pytest

WEB_ORIGIN = "http://localhost:5000"
OWN_ORIGIN = "http://localhost:8000"


def _preflight(client, origin: str):
    return client.options(
        "/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


@pytest.fixture
def web_origin_allowed(monkeypatch):
    # Must run before `client` (and so before `app_module` reloads the backend):
    # the origins are read from the environment at import time.
    monkeypatch.setenv("HOMEGUARD_CORS_ORIGINS", WEB_ORIGIN)


def test_parser_splits_trims_and_drops_blanks(app_module):
    parsed = app_module.extra_cors_origins(" http://a:5000/ ,, http://b:5000 ")
    assert parsed == ["http://a:5000", "http://b:5000"]


def test_parser_refuses_a_wildcard(app_module, caplog):
    with caplog.at_level(logging.WARNING, logger="cors"):
        parsed = app_module.extra_cors_origins("*,http://a:5000")
    assert parsed == ["http://a:5000"]
    assert "ignoring '*'" in caplog.text


async def test_an_unlisted_origin_is_refused_by_default(client):
    response = await _preflight(client, WEB_ORIGIN)
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_the_backends_own_origin_is_still_allowed(client):
    response = await _preflight(client, OWN_ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == OWN_ORIGIN


async def test_a_listed_origin_is_allowed(web_origin_allowed, client):
    response = await _preflight(client, WEB_ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == WEB_ORIGIN


async def test_listing_one_origin_does_not_open_others(web_origin_allowed, client):
    response = await _preflight(client, "http://evil.example")
    assert response.status_code == 400
