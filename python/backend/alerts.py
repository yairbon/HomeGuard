"""Alert channels that need no extra app on the phone: email, and optionally a
WhatsApp message to the owner's own number.

Why this exists
---------------
The system's own notifications only fire while the app is open. This is what
reaches a person with the app closed, on any phone, including the iPhone web
app (which cannot receive push at all). The channels, and what each needs:

- **Email** (SMTP, e.g. a Gmail account with an app password): free, reaches
  anyone, so it is also how the emergency contact is told that an alarm has
  not been cleared. This replaces the "police call" that never dialed anyone.
- **WhatsApp via CallMeBot** (optional): free, but a hobby gateway run by one
  person, for personal use only and only to the owner's own number. It is an
  extra for the owner's phone, never the only channel.

Where the settings live
-----------------------
In one local file that is never committed (`alerts.local.json`, see
`alerts.example.json`), because the credentials in it — the SMTP app password
and the CallMeBot key — must not be typed into the app or stored in
`state.json`, and the app has no reason to ever read them back. It is read on
every send, so editing it takes effect without a restart. Nothing here ever
logs a password, a key, or a phone number.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
import ssl
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import httpx

from .http_clients import build_client

logger = logging.getLogger("alerts")

CONFIG_FILE = Path(
    os.environ.get("HOMEGUARD_ALERTS_FILE")
    or Path(__file__).resolve().parent.parent / "alerts.local.json"
)

# SMTP and the CallMeBot call both run on the alert path (see `notify` in
# notifier.py for why that must never block), so neither may hang long.
_EMAIL_TIMEOUT = 15.0
_WHATSAPP_TIMEOUT = 15.0
_WHATSAPP_URL = "https://api.callmebot.com/whatsapp.php"

_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    alert_to: tuple[str, ...]
    escalation_to: tuple[str, ...]


@dataclass(frozen=True)
class WhatsAppTarget:
    phone: str
    apikey: str


@dataclass(frozen=True)
class AlertsConfig:
    email: EmailConfig | None = None
    whatsapp: tuple[WhatsAppTarget, ...] = ()

    def recipients(self, *, escalation: bool) -> tuple[str, ...]:
        """Who gets the email: the owner for a fresh alarm; the owner plus the
        emergency contacts once it has gone uncleared."""
        if self.email is None:
            return ()
        merged = self.email.alert_to + (self.email.escalation_to if escalation else ())
        return tuple(dict.fromkeys(merged))  # de-duplicated, order kept

    def with_recipients(
        self, alert_to: Iterable[str], escalation_to: Iterable[str]
    ) -> AlertsConfig:
        """The same config with the email recipients replaced.

        The recipients are edited from the app and live in `state.json`; the
        local file only has to hold the sending account. Without an email
        section there is nothing to address, so the config comes back as is."""
        if self.email is None:
            return self
        return replace(
            self,
            email=replace(
                self.email, alert_to=tuple(alert_to), escalation_to=tuple(escalation_to)
            ),
        )

    def any_channel(self, *, escalation: bool = False) -> bool:
        return bool(self.recipients(escalation=escalation) or self.whatsapp)

    def describe(self) -> str:
        """A one-line summary that is safe to show or log: counts only."""
        parts = []
        if self.email and not self.recipients(escalation=True):
            parts.append("email account set up, but no recipients yet (add them in Settings)")
        if self.email and self.recipients(escalation=False):
            parts.append(f"email to {len(self.recipients(escalation=False))} address(es)")
        if self.email and self.email.escalation_to:
            parts.append(f"escalation email to {len(self.email.escalation_to)} contact(s)")
        if self.whatsapp:
            parts.append(f"WhatsApp to {len(self.whatsapp)} number(s)")
        return ", ".join(parts) or "no alert channel configured"


def parse_addresses(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Splits a list, or a comma/semicolon-separated string, into
    ``(valid, invalid)`` email addresses. Duplicates are dropped, order is kept,
    and blanks are ignored."""
    if isinstance(value, str):
        value = re.split(r"[,;]", value)
    if not isinstance(value, list):
        return (), ()
    cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    valid = tuple(dict.fromkeys(a for a in cleaned if _EMAIL.match(a)))
    invalid = tuple(dict.fromkeys(a for a in cleaned if not _EMAIL.match(a)))
    return valid, invalid


def _addresses(value: object) -> tuple[str, ...]:
    """Just the valid addresses of `parse_addresses`."""
    return parse_addresses(value)[0]


def load_config() -> AlertsConfig:
    """Reads the local alerts file. Missing, unreadable or malformed means "no
    channel" — never an exception, because this runs on the alarm path. A
    malformed file is worth a warning (without its contents)."""
    try:
        # utf-8-sig: Notepad and PowerShell can both write a byte-order mark,
        # which plain utf-8 hands to the JSON parser as a stray character.
        raw = CONFIG_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return AlertsConfig()
    except OSError as exc:
        logger.warning("Could not read %s: %s", CONFIG_FILE.name, exc)
        return AlertsConfig()
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("%s is not valid JSON — no alerts will be sent.", CONFIG_FILE.name)
        return AlertsConfig()
    if not isinstance(data, dict):
        return AlertsConfig()

    email = None
    section = data.get("email")
    if isinstance(section, dict):
        username = str(section.get("username", "")).strip()
        password = str(section.get("app_password", "")).replace(" ", "")
        alert_to = _addresses(section.get("alert_to"))
        escalation_to = _addresses(section.get("escalation_to"))
        if username and password:
            try:
                port = int(section.get("smtp_port", 587))
            except (TypeError, ValueError):
                port = 587
            email = EmailConfig(
                host=str(section.get("smtp_host", "smtp.gmail.com")).strip() or "smtp.gmail.com",
                port=port,
                username=username,
                password=password,
                sender=str(section.get("from", "")).strip() or username,
                alert_to=alert_to,
                escalation_to=escalation_to,
            )
        elif username or password:
            logger.warning(
                "The email section of %s is incomplete (needs both username and "
                "app_password) — email is off.",
                CONFIG_FILE.name,
            )

    targets = []
    for item in data.get("whatsapp") or []:
        if isinstance(item, dict):
            phone = str(item.get("phone", "")).strip()
            key = str(item.get("apikey", "")).strip()
            if phone and key:
                targets.append(WhatsAppTarget(phone=phone, apikey=key))
    return AlertsConfig(email=email, whatsapp=tuple(targets))


# ---- Email ------------------------------------------------------------------


def _send_email_blocking(
    cfg: EmailConfig,
    recipients: tuple[str, ...],
    subject: str,
    body: str,
    photo: bytes | None = None,
):
    """One SMTP conversation. Blocking; always called through `asyncio.to_thread`."""
    message = EmailMessage()
    message["From"] = cfg.sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    # The time matters: a phone can show an email long after it was sent.
    note = "\nA picture from the camera is attached." if photo else ""
    message.set_content(
        f"{body}{note}\n\nTime: {datetime.now():%A %d %B %Y, %H:%M:%S}\nHomeGuard"
    )
    if photo:
        message.add_attachment(photo, maintype="image", subtype="jpeg", filename="camera.jpg")
    context = ssl.create_default_context()
    if cfg.port == 465:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=_EMAIL_TIMEOUT, context=context) as smtp:
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=_EMAIL_TIMEOUT) as smtp:
            smtp.starttls(context=context)
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(message)


async def send_email(
    cfg: AlertsConfig,
    subject: str,
    body: str,
    *,
    escalation: bool,
    photo: bytes | None = None,
) -> str:
    """Emails the right people. Returns "" on success, else a short reason that
    is safe to show (no secrets)."""
    recipients = cfg.recipients(escalation=escalation)
    if cfg.email is None or not recipients:
        return "email is not set up"
    try:
        await asyncio.to_thread(
            _send_email_blocking, cfg.email, recipients, subject, body, photo
        )
    except smtplib.SMTPAuthenticationError:
        logger.warning("Email alert failed: the SMTP login was refused (wrong app password?).")
        return "the email login was refused - check the app password"
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("Email alert failed: %s", type(exc).__name__)
        return f"could not send email ({type(exc).__name__})"
    logger.info("Email alert sent to %d address(es).", len(recipients))
    return ""


# ---- WhatsApp (CallMeBot) ------------------------------------------------------


class _RedactCallMeBot(logging.Filter):
    """httpx logs every request URL at INFO, and CallMeBot takes the key and the
    phone number in the query string. Scrub both before they reach a log."""

    _pattern = re.compile(r"(apikey|phone)=[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if "callmebot" in str(record.args) or "callmebot" in str(record.msg):
            record.msg = self._pattern.sub(r"\1=***", str(record.msg))
            if record.args:
                record.args = tuple(
                    self._pattern.sub(r"\1=***", str(a)) if "callmebot" in str(a) else a
                    for a in (record.args if isinstance(record.args, tuple) else (record.args,))
                )
        return True


logging.getLogger("httpx").addFilter(_RedactCallMeBot())

_whatsapp_client = build_client(timeout=_WHATSAPP_TIMEOUT)


async def send_whatsapp(target: WhatsAppTarget, text: str) -> str:
    """Sends one WhatsApp text through CallMeBot. "" on success, else a reason."""
    try:
        response = await _whatsapp_client.get(
            _WHATSAPP_URL,
            params={"phone": target.phone, "text": text, "apikey": target.apikey},
        )
    except httpx.HTTPError as exc:
        logger.warning("WhatsApp alert failed: %s", type(exc).__name__)
        return f"could not reach CallMeBot ({type(exc).__name__})"
    if response.status_code >= 400:
        logger.warning("WhatsApp alert refused: HTTP %d", response.status_code)
        return f"CallMeBot answered HTTP {response.status_code} - check the key and number"
    logger.info("WhatsApp alert sent.")
    return ""


# ---- Fan-out ------------------------------------------------------------------


async def deliver(
    cfg: AlertsConfig,
    title: str,
    message: str,
    *,
    escalation: bool = False,
    photo: bytes | None = None,
) -> dict[str, str]:
    """Sends one alert over every configured channel, concurrently. Returns a
    result per channel ("" = delivered). Never raises."""
    jobs: dict[str, asyncio.Future] = {}
    if cfg.recipients(escalation=escalation):
        jobs["email"] = asyncio.ensure_future(
            send_email(cfg, title, message, escalation=escalation, photo=photo)
        )
    for index, target in enumerate(cfg.whatsapp):
        label = "whatsapp" if len(cfg.whatsapp) == 1 else f"whatsapp {index + 1}"
        jobs[label] = asyncio.ensure_future(send_whatsapp(target, f"{title}: {message}"))
    if not jobs:
        return {}
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    return {
        name: ("" if result == "" else (result if isinstance(result, str) else "unexpected error"))
        for name, result in zip(jobs, results, strict=True)
    }
