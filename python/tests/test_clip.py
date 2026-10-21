"""The rolling last-minute video attached to a breach alarm email."""

import time

import cv2
import numpy as np
import pytest

from backend import alerts, clip
from backend.models import AlarmState, ArmMode, SensorType

from .test_alarm_flow import arm_now


def jpeg(value: int, size=(64, 48)) -> bytes:
    img = np.full((size[1], size[0], 3), value, np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def frames(n: int, spacing: float = 1.0, end: float | None = None):
    end = time.time() if end is None else end
    return [(end - (n - 1 - i) * spacing, jpeg(20 + i * 5)) for i in range(n)]


def test_encode_makes_a_playable_clip_in_real_time():
    data = clip.encode_clip(frames(10, spacing=1.0))  # 9 s of footage at 1 frame/s
    assert data and len(data) > 1000
    assert data[4:8] == b"ftyp"  # an MP4 file


def test_every_frame_is_used_once_and_the_clip_lasts_as_long_as_the_footage():
    # 7 frames in bursts with a gap: arrival times 0, .05, .1, 3.0, 3.05, 3.1, 6.0
    stamps = [0.0, 0.05, 0.1, 3.0, 3.05, 3.1, 6.0]
    data = [(t, jpeg(i)) for i, t in enumerate(stamps)]
    timeline, fps = clip._paced(list(reversed(data)))  # order of arrival does not matter
    assert timeline == [j for _, j in data]  # none repeated, none dropped
    assert abs(len(timeline) / fps - len(timeline) / ((len(stamps) - 1) / 6.0)) < 1e-6
    assert abs(fps - 1.0) < 1e-6  # 6 frame intervals in 6 s of footage


def test_not_enough_frames_means_no_clip():
    assert clip.encode_clip(None) is None
    assert clip.encode_clip([]) is None
    assert clip.encode_clip(frames(1)) is None


def test_garbage_frames_never_raise():
    assert clip.encode_clip([(1.0, b"nope"), (2.0, b"also nope")]) is None


def test_the_buffer_keeps_only_the_last_minute(state):
    old = time.time() - 61
    state.clip_frames.append((old, jpeg(1)))
    state.record_clip_frame(jpeg(2))
    assert len(state.clip_frames) == 1  # the 61-second-old frame was dropped


def test_alert_clip_needs_two_frames(state):
    assert state._alert_clip() is None
    state.record_clip_frame(jpeg(1))
    assert state._alert_clip() is None
    state.record_clip_frame(jpeg(2))
    assert len(state._alert_clip()) == 2


def test_email_carries_the_video_when_frames_exist(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, **k):
            pass

        def login(self, *a):
            pass

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    cfg = alerts.EmailConfig(
        "smtp.example.com", 587, "u@example.com", "pw", "u@example.com", (), ()
    )
    alerts._send_email_blocking(cfg, ("a@example.com",), "alarm", "door", None, tuple(frames(10)))
    attachments = list(captured["message"].iter_attachments())
    assert [a.get_filename() for a in attachments] == ["camera-last-minute.mp4"]
    assert attachments[0].get_content_type() == "video/mp4"
    assert "video of the last minute" in captured["message"].get_body().get_content()


def test_email_still_goes_out_when_the_video_cannot_be_made(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, **k):
            pass

        def login(self, *a):
            pass

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(clip, "encode_clip", lambda f: None)
    cfg = alerts.EmailConfig(
        "smtp.example.com", 587, "u@example.com", "pw", "u@example.com", (), ()
    )
    alerts._send_email_blocking(cfg, ("a@example.com",), "alarm", "door", None, tuple(frames(5)))
    assert list(captured["message"].iter_attachments()) == []
    assert "could not be made" in captured["message"].get_body().get_content()


@pytest.mark.parametrize("sensor", [SensorType.ultrasonic, SensorType.vibration])
async def test_home_mode_breach_by_a_perimeter_sensor_sends_nothing(state, monkeypatch, sensor):
    calls = []
    monkeypatch.setattr("backend.notifier.notify", lambda *a, **k: calls.append(k))
    await arm_now(state, ArmMode.home)
    await state.trigger_alert(sensor, "walking around inside")
    assert state.alarm_state == AlarmState.armed
    assert calls == []


async def test_breach_alarm_passes_the_recorded_frames_to_the_email(state, monkeypatch):
    calls = []
    monkeypatch.setattr("backend.notifier.notify", lambda *a, **k: calls.append(k))
    state.record_clip_frame(jpeg(10))
    state.record_clip_frame(jpeg(20))
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    assert len(calls[0]["clip"]) == 2


async def test_clip_setting_is_clamped_saved_and_obeyed(state, auth_client):
    reply = await auth_client.post("/settings/alert-clip", json={"enabled": True, "seconds": 500})
    assert reply.status_code == 200
    body = (await auth_client.get("/state")).json()
    assert body["clip_enabled"] is True and body["clip_seconds"] == 60
    await state.set_alert_clip(True, 1)
    assert state.clip_seconds == 5

    # only the frames inside the chosen window go into the clip
    now = time.time()
    state.clip_frames.extend([(now - 40, jpeg(1)), (now - 3, jpeg(2)), (now - 1, jpeg(3))])
    assert len(state._alert_clip()) == 2  # 5 s window: the 40-second-old frame is left out

    await state.set_alert_clip(False, 30)
    assert state._alert_clip() is None  # switched off


async def test_clip_setting_survives_a_restart(state):
    await state.set_alert_clip(False, 20)
    state._persist()
    state.clip_enabled, state.clip_seconds = True, 60
    state._load()
    assert (state.clip_enabled, state.clip_seconds) == (False, 20)


def test_pushed_stills_are_ignored_while_the_live_stream_delivers(state):
    state.record_clip_frame(jpeg(1), stream=True)
    state.record_clip_frame(jpeg(2))  # a recognition still: skipped, the stream has it covered
    assert len(state.clip_frames) == 1
    state._stream_frame_at = time.time() - 10  # the stream went quiet
    state.record_clip_frame(jpeg(3))
    assert len(state.clip_frames) == 2


# ---- Boards found by their own requests ---------------------------------------


def test_only_private_lan_addresses_are_learned():
    from backend import esp32_client as c

    assert c.private_board_address("192.168.68.55") == "192.168.68.55"
    assert c.private_board_address("172.20.10.3") == "172.20.10.3"
    for bad in ("127.0.0.1", "8.8.8.8", "169.254.1.1", "0.0.0.0", "::1", "", None, "x"):
        assert c.private_board_address(bad) is None


def test_the_door_address_follows_the_door_boards_requests(monkeypatch):
    from backend import esp32_client as c

    monkeypatch.setattr(c, "DOOR_CONTROLLER_URL", "http://192.168.68.123")
    assert c.learn_door_address("172.20.10.5") is True
    assert c.DOOR_CONTROLLER_URL == "http://172.20.10.5"
    assert c.learn_door_address("172.20.10.5") is False  # no change
    assert c.learn_door_address("8.8.8.8") is False  # a public address never redirects commands
    assert c.DOOR_CONTROLLER_URL == "http://172.20.10.5"


def test_the_camera_address_follows_its_pushes(monkeypatch):
    from backend import main as m

    monkeypatch.setattr(m, "CAMERA_STREAM_URL", "http://192.168.68.125/stream")
    assert m.learn_camera_address("172.20.10.6") is True
    assert m.CAMERA_STREAM_URL == "http://172.20.10.6/stream"
    assert m.learn_camera_address("127.0.0.1") is False


# ---- The backend announces itself to the boards ----------------------------------


def test_announcement_text_and_address_filter():
    from backend import announce

    assert announce.message(8000) == b"HOMEGUARD 1 8000"
    got = announce.private_addresses(
        [
            "192.168.68.110",
            "172.20.10.2",
            "10.1.2.3",
            "127.0.0.1",
            "169.254.5.5",
            "8.8.8.8",
            "::1",
            "x",
            "192.168.68.110",
        ]
    )
    assert got == ["192.168.68.110", "172.20.10.2", "10.1.2.3"]
