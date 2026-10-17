"""Outbound notifications, so an alert reaches the user even when the HomeGuard
app isn't running: ntfy (opt-in, needs the ntfy app) and, through
`alerts.py`, email and WhatsApp (set up in the local alerts file). `notify`
below fans one alert out to whichever of them are configured.

Why this exists
---------------
The app's own notifications (`flutter_local_notifications`) only fire while
its process is alive. Swipe the app away, or let Android kill it in the
background, and a break-in produces nothing at all — which defeats most of
the point of a remote alarm.

Why ntfy rather than Firebase Cloud Messaging
---------------------------------------------
FCM needs a Firebase project and a `google-services.json` that only the
project owner can create, and adding the Gradle plugin without that file
breaks the Android build outright (see ../CLAUDE.md). ntfy needs neither: the
backend POSTs to a topic URL, and the user installs the ntfy Android app and
subscribes to the same topic. No account, no new Flutter dependency, no
change to the app's build at all — and the topic can be served by ntfy.sh or
by a self-hosted instance.

Security note
-------------
A topic on the public ntfy.sh is readable by anyone who knows its name, and
notification bodies here describe alarm activity. So the topic name must be
treated as a secret: use a long random string, or self-host. The app's
settings screen says this next to the field.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import httpx

from . import alerts
from .http_clients import build_client

logger = logging.getLogger("notifier")

# Short, because this runs on the alert path. A slow or unreachable ntfy
# server must never delay the state broadcast that drives the in-app alarm UI
# — see `notify` for how that's guaranteed.
_TIMEOUT = 5.0

# The one client here that genuinely uses TLS — and the reason the trust store
# is built once in http_clients.py and shared, rather than three times.
_client = build_client(timeout=_TIMEOUT)

# Strong references to in-flight `notify()` tasks. See `notify` for why the
# alternative — letting the loop's weak reference be the only one — can drop
# an alarm push mid-flight. Entries remove themselves on completion.
_pending: set[asyncio.Task] = set()


async def _send(server: str, topic: str, title: str, message: str, priority: str, tags: str):
    """Performs one POST to `<server>/<topic>`.

    ntfy takes the body as the message text and everything else as headers,
    which is why there's no JSON payload here. Every failure is swallowed and
    logged: a notification that didn't go out is worth a log line, but it must
    not turn into a 500 on the request that raised the alarm.
    """
    url = f"{server.rstrip('/')}/{topic}"
    try:
        response = await _client.post(
            url,
            content=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
            },
        )
        response.raise_for_status()
        logger.info("Pushed notification to %s", url)
    except httpx.HTTPError as exc:
        logger.warning("Could not push notification to %s: %s", url, exc)


def _spawn(coroutine) -> None:
    """Runs `coroutine` in the background, holding a strong reference to it.

    asyncio keeps only a weak reference to a running task, so one suspended on
    a network round-trip with no strong reference anywhere can be garbage
    collected mid-flight — the alert then silently never leaves the box, with
    no exception and no log line. This is the path that reaches a phone with
    the app swiped away, so a silent drop is the whole feature failing exactly
    when it matters. `ruff` would normally flag this as RUF006, but the RUF
    rules aren't in the select list.
    """
    task = asyncio.create_task(coroutine)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def notify(
    server: str,
    topic: str,
    title: str,
    message: str,
    priority: str = "high",
    tags: str = "rotating_light",
    *,
    escalation: bool = False,
    alert_to: Iterable[str] | None = None,
    escalation_to: Iterable[str] | None = None,
    photo: bytes | None = None,
) -> None:
    """Fires off an alert on every configured channel without waiting for it.

    Deliberately **not** a coroutine to await. The caller is
    `AppState.trigger_alert`, which runs on the same event loop as every
    connected WebSocket; awaiting a network round-trip there would stall the
    real-time state push to every app instance for as long as a server takes
    to answer — up to the channel's timeout if it's unreachable. The alarm UI
    updating instantly matters more than knowing whether a message landed.

    Channels: ntfy when a topic is set, and email / WhatsApp when the local
    alerts file sets them up (see `alerts.py`). `escalation=True` is the
    "alarm still not cleared" follow-up, which additionally emails the
    emergency contacts. A no-op when nothing is configured, so every channel
    stays opt-in.
    """
    config = _with_app_recipients(alerts.load_config(), alert_to, escalation_to)
    if topic:
        _spawn(_send(server, topic, title, message, priority, tags))
    if config.any_channel(escalation=escalation):
        _spawn(alerts.deliver(config, title, message, escalation=escalation, photo=photo))


def _with_app_recipients(
    config: alerts.AlertsConfig,
    alert_to: Iterable[str] | None,
    escalation_to: Iterable[str] | None,
) -> alerts.AlertsConfig:
    """The recipients set in the app replace the ones in the local file."""
    if alert_to is None and escalation_to is None:
        return config
    return config.with_recipients(alert_to or (), escalation_to or ())


async def send_test(
    server: str,
    topic: str,
    alert_to: Iterable[str] | None = None,
    escalation_to: Iterable[str] | None = None,
    photo: bytes | None = None,
) -> dict[str, str]:
    """Sends a harmless test over every configured channel and *waits* for the
    outcome, unlike `notify`: this is the user asking "does it work?", so the
    answer has to be real. One entry per channel, "" meaning delivered.

    ntfy is fire-and-forget by nature (`_send` swallows its own errors), so it
    is reported as handed off rather than confirmed.
    """
    config = _with_app_recipients(alerts.load_config(), alert_to, escalation_to)
    results = await alerts.deliver(
        config,
        "HomeGuard test",
        "Test alert - this is how a real alarm will reach you.",
        escalation=True,
        photo=photo,
    )
    if topic:
        _spawn(
            _send(
                server,
                topic,
                "HomeGuard test",
                "Test notification — your alerts will arrive here.",
                "default",
                "white_check_mark",
            )
        )
        results["ntfy"] = ""
    return results


async def aclose() -> None:
    """Closes the shared HTTP client, for a clean shutdown.

    Drains any still-in-flight pushes first — closing the client out from
    under them would abort an alert that was already on the wire. Bounded, so
    an unreachable ntfy server delays shutdown by at most `_TIMEOUT`.
    """
    if _pending:
        await asyncio.wait(set(_pending), timeout=_TIMEOUT)
    await _client.aclose()
