"""The camera hub: one upstream connection, many viewers (backend/camera_hub.py).

The camera can serve a single stream, so everything here is about sharing one
connection fairly and letting it go when nobody is watching. The upstream is a
fake whose chunks the test releases one at a time.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from backend import camera_hub
from backend.camera_hub import CameraHub


class FakeUpstream:
    """Quacks like the streaming httpx.Response the hub reads from."""

    def __init__(self, content_type: str = "multipart/x-mixed-replace; boundary=frame"):
        self.headers = {"content-type": content_type}
        self.closed = False
        self._chunks: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

    def feed(self, chunk: bytes) -> None:
        self._chunks.put_nowait(chunk)

    def end(self) -> None:
        self._chunks.put_nowait(None)

    def fail(self) -> None:
        self._chunks.put_nowait(httpx.ReadError("camera dropped"))

    async def aiter_raw(self):
        while True:
            item = await self._chunks.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def aclose(self) -> None:
        self.closed = True


class Opener:
    def __init__(self):
        self.upstreams: list[FakeUpstream] = []
        self.error: Exception | None = None

    async def __call__(self) -> FakeUpstream:
        if self.error is not None:
            raise self.error
        upstream = FakeUpstream()
        self.upstreams.append(upstream)
        return upstream


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def take(chunks, count: int = 1) -> list[bytes]:
    return [await asyncio.wait_for(chunks.__anext__(), timeout=2) for _ in range(count)]


async def test_two_viewers_share_one_upstream_connection():
    opener = Opener()
    hub = CameraHub(opener)

    content_type, first = await hub.subscribe()
    _, second = await hub.subscribe()
    assert len(opener.upstreams) == 1  # the camera is opened once, not per viewer
    assert content_type.startswith("multipart/x-mixed-replace")
    assert hub.viewer_count == 2

    opener.upstreams[0].feed(b"frame-1")
    opener.upstreams[0].feed(b"frame-2")
    assert await take(first, 2) == [b"frame-1", b"frame-2"]
    assert await take(second, 2) == [b"frame-1", b"frame-2"]
    await hub.aclose()


async def test_the_camera_is_released_when_the_last_viewer_leaves_and_reopened_for_the_next():
    opener = Opener()
    hub = CameraHub(opener)

    _, viewer = await hub.subscribe()
    opener.upstreams[0].feed(b"a")
    await take(viewer)
    await viewer.aclose()  # the viewer disconnects
    await settle()
    assert opener.upstreams[0].closed
    assert hub.viewer_count == 0

    _, again = await hub.subscribe()
    assert len(opener.upstreams) == 2  # a fresh connection for the next watcher
    opener.upstreams[1].feed(b"b")
    assert await take(again) == [b"b"]
    await hub.aclose()


async def test_one_viewer_leaving_does_not_cut_off_the_other():
    opener = Opener()
    hub = CameraHub(opener)
    _, staying = await hub.subscribe()
    _, leaving = await hub.subscribe()

    await leaving.aclose()
    await settle()
    assert not opener.upstreams[0].closed

    opener.upstreams[0].feed(b"still-here")
    assert await take(staying) == [b"still-here"]
    await hub.aclose()


async def test_a_slow_viewer_loses_its_oldest_bytes_and_never_holds_up_the_others(monkeypatch):
    monkeypatch.setattr(camera_hub, "VIEWER_QUEUE_CHUNKS", 3)
    opener = Opener()
    hub = CameraHub(opener)
    _, fast = await hub.subscribe()
    _, slow = await hub.subscribe()

    for index in range(8):
        opener.upstreams[0].feed(f"c{index}".encode())
        assert await take(fast) == [f"c{index}".encode()]  # the fast one keeps up

    # The slow one never read, so it holds only the newest three.
    assert await take(slow, 3) == [b"c5", b"c6", b"c7"]
    await hub.aclose()


async def test_viewers_end_cleanly_when_the_camera_stream_ends_or_breaks():
    opener = Opener()
    hub = CameraHub(opener)
    _, viewer = await hub.subscribe()
    opener.upstreams[0].feed(b"x")
    opener.upstreams[0].end()
    assert await take(viewer) == [b"x"]
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(viewer.__anext__(), timeout=2)

    _, broken = await hub.subscribe()  # the dead connection is replaced
    assert len(opener.upstreams) == 2
    opener.upstreams[1].fail()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(broken.__anext__(), timeout=2)
    assert opener.upstreams[1].closed
    await hub.aclose()


async def test_a_failure_to_reach_the_camera_reaches_the_caller_and_leaves_the_hub_usable():
    opener = Opener()
    opener.error = HTTPException(status_code=502, detail="Camera unreachable")
    hub = CameraHub(opener)

    with pytest.raises(HTTPException) as caught:
        await hub.subscribe()
    assert caught.value.status_code == 502
    assert hub.viewer_count == 0

    opener.error = None
    _, viewer = await hub.subscribe()
    opener.upstreams[0].feed(b"ok")
    assert await take(viewer) == [b"ok"]
    await hub.aclose()


async def test_the_stream_route_reports_a_missing_camera_cleanly(auth_client):
    # HOMEGUARD_CAMERA_STREAM_URL is unset under test: 503, not a hang or a crash.
    response = await auth_client.get("/camera/stream")
    assert response.status_code == 503
