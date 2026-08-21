"""Outbound push notifications via ntfy, so an alert reaches the user's
phone even when the HomeGuard app isn't running.

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

import httpx

logger = logging.getLogger("notifier")

# Short, because this runs on the alert path. A slow or unreachable ntfy
# server must never delay the state broadcast that drives the in-app alarm UI
# — see `notify` for how that's guaranteed.
_TIMEOUT = 5.0

_client = httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False)

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


def notify(
    server: str,
    topic: str,
    title: str,
    message: str,
    priority: str = "high",
    tags: str = "rotating_light",
) -> None:
    """Fires off a notification without waiting for it.

    Deliberately **not** a coroutine to await. The caller is
    `AppState.trigger_alert`, which runs on the same event loop as every
    connected WebSocket; awaiting a network round-trip there would stall the
    real-time state push to every app instance for as long as the ntfy server
    takes to answer — up to `_TIMEOUT` seconds if it's unreachable. The alarm
    UI updating instantly matters more than knowing whether the push landed.

    A no-op when no topic is configured, so the whole feature stays opt-in.
    """
    if not topic:
        return
    # Never awaited: this is intentional fire-and-forget, and `_send` catches
    # everything so the task cannot raise into the void.
    #
    # But it *is* held, in `_pending`. asyncio keeps only a weak reference to
    # a running task, so one suspended on the ntfy round-trip with no strong
    # reference anywhere can be garbage-collected mid-flight — the push then
    # silently never leaves the box, with no exception and no log line. This
    # is the one notification path that reaches a phone with the app swiped
    # away, so a silent drop is the whole feature failing exactly when it
    # matters. `ruff` would normally flag this as RUF006, but the RUF rules
    # aren't in the select list.
    task = asyncio.create_task(_send(server, topic, title, message, priority, tags))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def aclose() -> None:
    """Closes the shared HTTP client, for a clean shutdown.

    Drains any still-in-flight pushes first — closing the client out from
    under them would abort an alert that was already on the wire. Bounded, so
    an unreachable ntfy server delays shutdown by at most `_TIMEOUT`.
    """
    if _pending:
        await asyncio.wait(set(_pending), timeout=_TIMEOUT)
    await _client.aclose()
