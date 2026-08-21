"""One shared TLS trust store for every outbound HTTP client in the backend.

Three clients talk *out* of this process: push notifications (`notifier`),
door-controller commands (`esp32_client`) and the ESP32-CAM stream proxy
(`main`). Each of them used to be a bare `httpx.AsyncClient()`, and each of
those builds its own `ssl.SSLContext` — which means locating, reading and
parsing the system CA bundle. Measured at ~200ms per client on this machine,
so roughly 650ms of every backend start was spent building three identical
trust stores for two clients that only ever speak plain HTTP to a
`192.168.x.x` board and one that speaks to a single fixed ntfy host.

The context is built once here and handed to all three.

`trust_env=False` is the other half of this, and it is about correctness
rather than speed. With `trust_env` left on, httpx reads `HTTP_PROXY` /
`ALL_PROXY` from the environment — so a proxy variable set for something
entirely unrelated (a corporate VPN client, a stray line in a shell profile)
silently routes arm/disarm commands and camera frames through a proxy that
has no route to the LAN. They don't fail fast either: they hang until the
timeout, which the app then reports as "hardware didn't acknowledge" on a
board that was sitting there reachable the whole time.

Note that the *context* is still built with httpx's default `trust_env=True`,
so `SSL_CERT_FILE` / `SSL_CERT_DIR` are still honoured for anyone who needs a
custom CA — the two flags are independent knobs and only the proxy half is
being turned off.
"""

from __future__ import annotations

import httpx

# Built at import, deliberately: all three clients below are themselves
# module-level, so there is no later moment at which this could be built
# lazily and save anything.
_SSL_CONTEXT = httpx.create_ssl_context()


def build_client(**kwargs) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` that shares this module's trust store and
    ignores proxy environment variables.

    `kwargs` are passed straight through, so each caller still picks its own
    timeout/limits profile — those differ a lot here (3 seconds for a board
    command, no read timeout at all for an MJPEG stream).
    """
    return httpx.AsyncClient(verify=_SSL_CONTEXT, trust_env=False, **kwargs)
