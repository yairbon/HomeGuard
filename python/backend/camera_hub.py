"""One connection to the ESP32-CAM's stream, shared by every viewer.

Why this exists
---------------
The camera's own web server serves its MJPEG stream from an endless loop, so it
can feed exactly one viewer at a time. The backend used to open a fresh
connection to the camera for every app that opened the Camera tab, so a second
viewer (the laptop and a phone at once) got nothing.

Now the backend holds a single connection to the camera and hands a copy of the
bytes to every viewer. The first viewer opens it; when the last one leaves it is
closed again, so the camera is left alone while nobody is watching.

What a viewer receives is exactly what the camera sent, from wherever the
stream happens to be when it joins. A viewer that joins mid-frame starts with
a partial frame, which the app's parser discards until it finds the next
"--boundary" line (see mjpeg_stream.dart). A viewer too slow to keep up loses
its oldest bytes instead of holding everyone else back; the parser resyncs the
same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

logger = logging.getLogger("camera_hub")

# Bytes are queued per viewer in the sizes the network delivers them (a few KB
# each, about a hundred a second at the camera's frame rate), so this is on the
# order of a couple of seconds of video before a stalled viewer starts losing data.
VIEWER_QUEUE_CHUNKS = 200

# How long a new viewer waits for a previous upstream connection to finish
# closing: the camera cannot serve a second stream until the first has ended.
CLOSE_WAIT_SECONDS = 2.0

_END = None  # what a viewer's queue receives when the stream is over

Opener = Callable[[], Awaitable[httpx.Response]]


class CameraHub:
    """Fans one upstream stream out to many viewers. See the module docstring.

    `opener` opens the camera's stream and returns the streaming response; it may
    raise (for example an HTTPException) and then no viewer is registered.
    """

    def __init__(self, opener: Opener):
        self._opener = opener
        self._viewers: set[asyncio.Queue[bytes | None]] = set()
        self._task: asyncio.Task | None = None
        self._finishing: asyncio.Task | None = None
        self._content_type = ""
        self._lock = asyncio.Lock()

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    async def subscribe(self) -> tuple[str, AsyncIterator[bytes]]:
        """Joins the stream. Returns the camera's Content-Type and an async
        iterator of its bytes. Raises whatever the opener raises."""
        async with self._lock:
            if self._task is None or self._task.done():
                if self._finishing is not None:
                    # asyncio.wait, not wait_for: it simply reports a finished
                    # (or cancelled) task, and still lets *our own* cancellation
                    # through, which swallowing CancelledError would not.
                    await asyncio.wait({self._finishing}, timeout=CLOSE_WAIT_SECONDS)
                upstream = await self._opener()
                self._content_type = upstream.headers.get(
                    "content-type", "multipart/x-mixed-replace; boundary=frame"
                )
                self._task = asyncio.create_task(self._pump(upstream))
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=VIEWER_QUEUE_CHUNKS)
            self._viewers.add(queue)
        return self._content_type, self._iterate(queue)

    async def _iterate(self, queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await queue.get()
                if chunk is _END:
                    return
                yield chunk
        finally:
            self._viewers.discard(queue)
            if not self._viewers:
                self._release_upstream()

    def _release_upstream(self) -> None:
        """The last viewer left: let the camera go."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            self._finishing = task

    async def _pump(self, upstream: httpx.Response) -> None:
        try:
            async for chunk in upstream.aiter_raw():
                for queue in tuple(self._viewers):
                    if queue.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()  # a slow viewer loses its oldest bytes
                    queue.put_nowait(chunk)
        except (httpx.StreamError, httpx.TransportError, OSError) as exc:
            logger.info("Camera stream ended: %s", type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                await upstream.aclose()
            for queue in tuple(self._viewers):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(_END)

    async def aclose(self) -> None:
        """Stops the upstream connection (shutdown)."""
        self._release_upstream()
        if self._finishing is not None:
            await asyncio.wait({self._finishing}, timeout=CLOSE_WAIT_SECONDS)
