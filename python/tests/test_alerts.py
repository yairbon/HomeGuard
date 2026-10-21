"""Email and WhatsApp alerts (backend/alerts.py), and how notify() fans out to them.

Nothing here touches a network: the SMTP conversation and the CallMeBot request
are replaced with recorders, so what is asserted is *who is told what, and when*,
and that no secret ever reaches a log or a response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib

import httpx
import pytest

from backend import alerts, notifier
from backend.models import ArmMode, SensorType

from .test_alarm_flow import arm_now

SECRET_PASSWORD = "abcdefghijklmnop"
SECRET_KEY = "4815162342"
PHONE = "+972501234567"

FULL = {
    "email": {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "username": "homeguard.sender@example.com",
        "app_password": "abcd efgh ijkl mnop",
        "alert_to": ["owner@example.com"],
        "escalation_to": ["contact@example.com", "owner@example.com"],
    },
    "whatsapp": [{"phone": PHONE, "apikey": SECRET_KEY}],
}


# What the owner types into Settings: the recipients live in the app now, the file
# only holds the sending account.
APP_RECIPIENTS = (["owner@example.com"], ["contact@example.com", "owner@example.com"])


def write_config(tmp_path, monkeypatch, data) -> None:
    path = tmp_path / "alerts.local.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    monkeypatch.setattr(alerts, "CONFIG_FILE", path)


@pytest.fixture
def emails(monkeypatch):
    """Records every email instead of speaking SMTP."""
    sent: list[dict] = []

    def fake(cfg, recipients, subject, body, photo=None, clip_frames=None):
        sent.append(
            {
                "to": recipients,
                "subject": subject,
                "body": body,
                "login": cfg.username,
                "photo": photo,
            }
        )

    monkeypatch.setattr(alerts, "_send_email_blocking", fake)
    return sent


class _FakeWhatsApp:
    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[dict] = []

    async def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        return httpx.Response(self.status)


@pytest.fixture
def whatsapp(monkeypatch):
    fake = _FakeWhatsApp()
    monkeypatch.setattr(alerts, "_whatsapp_client", fake)
    return fake


async def settle():
    """Lets the fire-and-forget delivery tasks (and their worker thread) finish."""
    for _ in range(20):
        await asyncio.sleep(0.01)


# ---- Reading the local file ----------------------------------------------------


def test_no_file_means_no_channel():
    config = alerts.load_config()
    assert not config.any_channel()
    assert config.describe() == "no alert channel configured"


def test_a_full_file_is_understood(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, FULL)
    config = alerts.load_config()
    assert config.any_channel()
    assert config.email.password == SECRET_PASSWORD  # the spaces Google shows are dropped
    assert config.recipients(escalation=False) == ("owner@example.com",)
    # The contacts are added on escalation, and nobody is mailed twice.
    assert config.recipients(escalation=True) == ("owner@example.com", "contact@example.com")
    assert config.whatsapp[0].phone == PHONE
    assert "owner@example.com" not in config.describe()  # counts only, no addresses
    assert SECRET_KEY not in config.describe()


def test_a_file_saved_with_a_byte_order_mark_still_loads(tmp_path, monkeypatch):
    path = tmp_path / "alerts.local.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(FULL).encode("utf-8"))
    monkeypatch.setattr(alerts, "CONFIG_FILE", path)
    assert alerts.load_config().any_channel()


def test_addresses_may_be_a_comma_separated_string(tmp_path, monkeypatch):
    data = json.loads(json.dumps(FULL))
    data["email"]["alert_to"] = "a@example.com, not-an-address; b@example.com"
    write_config(tmp_path, monkeypatch, data)
    assert alerts.load_config().recipients(escalation=False) == ("a@example.com", "b@example.com")


def test_an_incomplete_email_section_turns_email_off(tmp_path, monkeypatch, caplog):
    data = {"email": {"username": "x@example.com", "alert_to": ["owner@example.com"]}}
    write_config(tmp_path, monkeypatch, data)
    with caplog.at_level(logging.WARNING, logger="alerts"):
        config = alerts.load_config()
    assert config.email is None
    assert "incomplete" in caplog.text


def test_a_malformed_file_is_survived_and_its_contents_are_not_logged(
    tmp_path, monkeypatch, caplog
):
    write_config(tmp_path, monkeypatch, '{"email": {"app_password": "' + SECRET_PASSWORD)
    with caplog.at_level(logging.WARNING, logger="alerts"):
        assert not alerts.load_config().any_channel()
    assert SECRET_PASSWORD not in caplog.text


# ---- Who is told what, and when -----------------------------------------------


async def test_nothing_is_sent_without_a_channel(emails, whatsapp):
    notifier.notify("https://ntfy.sh", "", "t", "m")
    await settle()
    assert emails == [] and whatsapp.calls == []


async def test_a_fresh_alarm_emails_only_the_owner_and_messages_whatsapp(
    tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    notifier.notify("https://ntfy.sh", "", "HomeGuard alarm", "front door forced")
    await settle()

    assert [e["to"] for e in emails] == [("owner@example.com",)]
    assert emails[0]["subject"] == "HomeGuard alarm"
    assert "front door forced" in emails[0]["body"]
    assert len(whatsapp.calls) == 1
    assert whatsapp.calls[0]["params"]["phone"] == PHONE
    assert "front door forced" in whatsapp.calls[0]["params"]["text"]


async def test_escalation_also_emails_the_emergency_contacts(
    tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    notifier.notify("https://ntfy.sh", "", "t", "m", escalation=True)
    await settle()
    assert [e["to"] for e in emails] == [("owner@example.com", "contact@example.com")]


async def test_an_escalation_contact_alone_is_still_a_channel(
    tmp_path, monkeypatch, emails, whatsapp
):
    data = json.loads(json.dumps(FULL))
    data["email"]["alert_to"] = []
    data.pop("whatsapp")
    write_config(tmp_path, monkeypatch, data)

    notifier.notify("https://ntfy.sh", "", "t", "m")  # a fresh alarm: nobody to tell yet
    await settle()
    assert emails == []

    notifier.notify("https://ntfy.sh", "", "t", "m", escalation=True)
    await settle()
    assert [e["to"] for e in emails] == [("contact@example.com", "owner@example.com")]


async def test_a_real_alarm_goes_through_trigger_alert(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    await state.set_alert_emails(*APP_RECIPIENTS)
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()
    assert len(emails) == 1 and len(whatsapp.calls) == 1


async def test_the_escalation_reaches_the_contacts_and_is_logged_as_sent(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    await state.set_alert_emails(*APP_RECIPIENTS)
    state.police_call_delay_seconds = 0
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()

    assert any("contact@example.com" in e["to"] for e in emails)
    messages = [e.message for e in state.events]
    assert any("escalation notification sent" in m for m in messages)


# ---- Failure is survived and never leaks a secret ------------------------------


async def test_a_refused_smtp_login_is_reported_without_the_password(
    tmp_path, monkeypatch, caplog
):
    write_config(tmp_path, monkeypatch, FULL)

    def refuse(*args):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(alerts, "_send_email_blocking", refuse)
    with caplog.at_level(logging.INFO):
        results = await alerts.deliver(alerts.load_config(), "t", "m")
    assert "login was refused" in results["email"]
    assert SECRET_PASSWORD not in results["email"]
    assert SECRET_PASSWORD not in caplog.text


async def test_a_failing_channel_never_raises_into_the_alarm_path(
    state, tmp_path, monkeypatch, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)

    def boom(*args):
        raise OSError("network is down")

    monkeypatch.setattr(alerts, "_send_email_blocking", boom)
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")  # must not raise
    await settle()
    assert state.alarm_state.value == "triggered"


async def test_whatsapp_refusal_names_the_status_not_the_key(tmp_path, monkeypatch, whatsapp):
    write_config(tmp_path, monkeypatch, FULL)
    whatsapp.status = 400
    results = await alerts.deliver(alerts.load_config(), "t", "m")
    assert "HTTP 400" in results["whatsapp"]
    assert SECRET_KEY not in results["whatsapp"]


def test_request_logs_never_show_the_whatsapp_key_or_number(caplog):
    url = f"https://api.callmebot.com/whatsapp.php?phone=%2B972501234567&text=hi&apikey={SECRET_KEY}"
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: GET %s "HTTP/1.1 200 OK"', url)
    assert SECRET_KEY not in caplog.text
    assert "972501234567" not in caplog.text
    assert "apikey=***" in caplog.text


# ---- The SEND TEST button ---------------------------------------------------------


async def test_the_test_route_requires_a_channel(auth_client):
    body = (await auth_client.post("/settings/alerts/test")).json()
    assert body["ok"] is False
    assert "No alert channel" in body["summary"]


async def test_the_test_route_reports_success_per_channel(
    auth_client, state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    await state.set_alert_emails(*APP_RECIPIENTS)
    body = (await auth_client.post("/settings/alerts/test")).json()
    assert body["ok"] is True
    assert set(body["results"]) == {"email", "whatsapp"}
    assert body["summary"].startswith("Test sent via")
    assert SECRET_PASSWORD not in json.dumps(body) and SECRET_KEY not in json.dumps(body)


async def test_the_test_route_says_which_channel_failed(
    auth_client, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    whatsapp.status = 403
    body = (await auth_client.post("/settings/alerts/test")).json()
    assert body["ok"] is False
    assert "whatsapp" in body["summary"] and "403" in body["summary"]


# ---- The master switch ---------------------------------------------------------


async def test_alerts_are_on_by_default_and_the_flag_is_in_the_state(auth_client):
    assert (await auth_client.get("/state")).json()["alerts_enabled"] is True


async def test_switching_alerts_off_stops_the_messages_but_not_the_alarm(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    await state.set_alert_emails(*APP_RECIPIENTS)
    await state.set_alerts_enabled(False)
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()

    assert state.alarm_state.value == "triggered"  # the alarm itself is untouched
    assert emails == [] and whatsapp.calls == []

    await state.set_alerts_enabled(True)
    await state.disarm()
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()
    assert len(emails) == 1


async def test_the_escalation_is_not_sent_either_when_alerts_are_off(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    state.police_call_delay_seconds = 0
    await state.set_alerts_enabled(False)
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()

    assert emails == []
    messages = [e.message for e in state.events]
    assert any("alerts are switched off" in m for m in messages)
    assert not any("escalation notification sent" in m for m in messages)


async def test_the_switch_is_logged_and_survives_a_restart(monkeypatch, tmp_path):
    import importlib
    from pathlib import Path

    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(Path(tmp_path) / "switch-state.json"))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", "test-device-key")
    from backend import state as state_module

    importlib.reload(state_module)
    await state_module.state.set_alerts_enabled(False)
    assert any("switched OFF" in e.message for e in state_module.state.events)

    importlib.reload(state_module)  # a restart
    assert state_module.state.alerts_enabled is False


async def test_the_test_button_still_sends_but_warns_when_alerts_are_off(
    auth_client, state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    await state.set_alert_emails(*APP_RECIPIENTS)
    await state.set_alerts_enabled(False)
    body = (await auth_client.post("/settings/alerts/test")).json()
    assert body["ok"] is True and len(emails) == 1
    assert "switched off" in body["summary"]


async def test_the_switch_route_works(auth_client):
    # (That it needs a login is covered by the route table in test_auth.py.)
    reply = await auth_client.post("/settings/alerts-enabled", json={"enabled": False})
    assert reply.json() == {"ok": True}
    assert (await auth_client.get("/state")).json()["alerts_enabled"] is False


# ---- Recipients are edited from the app ------------------------------------------


async def test_the_app_recipients_replace_the_ones_in_the_file(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)  # the file says owner@ / contact@
    await state.set_alert_emails(["someone.else@example.com"], [])
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()
    assert [e["to"] for e in emails] == [("someone.else@example.com",)]


async def test_clearing_the_recipients_means_nobody_is_emailed(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    data = json.loads(json.dumps(FULL))
    data.pop("whatsapp")
    write_config(tmp_path, monkeypatch, data)
    await state.set_alert_emails([], [])
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()
    assert emails == []


async def test_the_recipients_route_saves_and_shows_them_in_the_state(auth_client):
    reply = await auth_client.post(
        "/settings/alert-emails",
        json={"alert_to": ["a@example.com", "b@example.com"], "escalation_to": ["c@example.com"]},
    )
    assert reply.json() == {"ok": True}
    body = (await auth_client.get("/state")).json()
    assert body["alert_emails"] == ["a@example.com", "b@example.com"]
    assert body["escalation_emails"] == ["c@example.com"]


async def test_a_malformed_address_is_refused_by_name(auth_client):
    reply = await auth_client.post(
        "/settings/alert-emails",
        json={"alert_to": ["good@example.com", "not-an-address"], "escalation_to": []},
    )
    assert reply.status_code == 422
    assert "not-an-address" in reply.json()["detail"]
    assert (await auth_client.get("/state")).json()["alert_emails"] == []  # nothing was saved


async def test_the_addresses_are_not_written_to_the_event_log(state):
    await state.set_alert_emails(["private.person@example.com"], ["contact@example.com"])
    assert all("private.person" not in e.message for e in state.events)
    assert any("Alert recipients updated" in e.message for e in state.events)


async def test_the_first_start_carries_the_file_recipients_over_once(
    tmp_path, monkeypatch
):
    import importlib
    from pathlib import Path

    write_config(tmp_path, monkeypatch, FULL)
    monkeypatch.setenv("HOMEGUARD_STATE_FILE", str(Path(tmp_path) / "seed-state.json"))
    monkeypatch.setenv("HOMEGUARD_DEVICE_KEY", "test-device-key")
    from backend import state as state_module

    importlib.reload(state_module)
    assert state_module.state.alert_emails == ["owner@example.com"]

    # The app now owns them: a later edit of the file does not change them.
    await state_module.state.set_alert_emails(["changed@example.com"], [])
    other = json.loads(json.dumps(FULL))
    other["email"]["alert_to"] = ["from.the.file@example.com"]
    write_config(tmp_path, monkeypatch, other)
    importlib.reload(state_module)
    assert state_module.state.alert_emails == ["changed@example.com"]


async def test_email_works_with_no_recipients_in_the_file(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    data = json.loads(json.dumps(FULL))
    data["email"]["alert_to"] = []
    data["email"]["escalation_to"] = []
    write_config(tmp_path, monkeypatch, data)
    assert alerts.load_config().email is not None  # the account alone is enough
    await state.set_alert_emails(["owner@example.com"], [])
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()
    assert [e["to"] for e in emails] == [("owner@example.com",)]


# ---- A picture from the camera goes with the alarm ------------------------------


JPEG = b"\xff\xd8\xff\xe0fake-camera-frame\xff\xd9"


async def test_the_alarm_email_carries_the_latest_camera_picture(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    write_config(tmp_path, monkeypatch, FULL)
    await state.set_alert_emails(*APP_RECIPIENTS)
    await state.note_camera_frame()  # the camera is delivering
    state.camera_frame_source = lambda: JPEG
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()
    assert emails[0]["photo"] == JPEG


async def test_no_picture_is_attached_when_the_camera_is_not_delivering(
    state, tmp_path, monkeypatch, emails, whatsapp
):
    """The cache keeps the last frame forever; an old picture next to a fresh
    alarm would mislead, so a silent camera means no attachment."""
    write_config(tmp_path, monkeypatch, FULL)
    await state.set_alert_emails(*APP_RECIPIENTS)
    state.camera_frame_source = lambda: JPEG  # a frame exists, but none has arrived lately
    assert not state.camera_online
    await arm_now(state, ArmMode.away)
    await state.trigger_alert(SensorType.door, "front door forced")
    await settle()
    assert emails[0]["photo"] is None


def test_the_email_message_really_has_the_picture_attached(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, **kwargs):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    cfg = alerts.EmailConfig(
        "smtp.example.com", 587, "u@example.com", "pw", "u@example.com", (), ()
    )
    alerts._send_email_blocking(cfg, ("a@example.com",), "HomeGuard alarm", "door", JPEG)

    attachments = list(captured["message"].iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "image/jpeg"
    assert attachments[0].get_filename() == "camera.jpg"
    assert attachments[0].get_content() == JPEG
    assert "picture from the camera is attached" in captured["message"].get_body().get_content()
