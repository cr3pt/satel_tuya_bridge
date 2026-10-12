# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

import aiohttp

logger = logging.getLogger("bridge.notifier")

CHANNELS = ("ntfy", "email", "pushover", "bark", "pushcut")
PRIORITIES = ("default", "high", "urgent")
SECURITY_MODES = ("starttls", "ssl", "none")
_TIMEOUT = aiohttp.ClientTimeout(total=10)
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_PUSHOVER_RE = re.compile(r"^[A-Za-z0-9]{30}$")
_BARK_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")
_PUSHCUT_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,60}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
_URL_RE = re.compile(r"^https?://[^\s/]+(/\S*)?$")


def _channel_cfg(cfg: dict, name: str) -> dict | None:
    section = cfg.get(name)
    if not isinstance(section, dict) or not section.get("enabled", True):
        return None
    return section


def _email_recipients(section: dict) -> list[str]:
    raw = section.get("to") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;\s]+", raw)
    return [r.strip() for r in raw if r and r.strip()]


def _configured(cfg: dict, name: str) -> bool:
    if name == "ntfy":
        return bool(cfg.get("ntfy_topic_url"))
    section = _channel_cfg(cfg, name)
    if section is None:
        return False
    if name == "email":
        return bool(section.get("host") and section.get("from") and _email_recipients(section))
    if name == "pushover":
        return bool(section.get("user_key") and section.get("app_token"))
    if name == "bark":
        return bool(section.get("device_key"))
    return bool(section.get("api_key") and section.get("notification_name"))


async def _send_ntfy(cfg: dict, title: str, message: str, priority: str) -> None:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        resp = await session.post(
            cfg["ntfy_topic_url"], data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
        )
        resp.raise_for_status()


async def _send_pushover(cfg: dict, title: str, message: str, priority: str) -> None:
    section = cfg["pushover"]
    data = {"token": section["app_token"], "user": section["user_key"], "title": title, "message": message,
            "priority": {"default": 0, "high": 1, "urgent": 2}[priority]}
    if priority == "urgent":
        data.update({"retry": 60, "expire": 3600})
    if section.get("device"):
        data["device"] = section["device"]
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        resp = await session.post("https://api.pushover.net/1/messages.json", data=data)
        resp.raise_for_status()


async def _send_bark(cfg: dict, title: str, message: str, priority: str) -> None:
    section = cfg["bark"]
    server = (section.get("server") or "https://api.day.app").rstrip("/")
    payload = {"device_key": section["device_key"], "title": title, "body": message,
               "level": {"default": "active", "high": "timeSensitive", "urgent": "critical"}[priority],
               "group": section.get("group") or "satel-bridge"}
    if priority == "urgent":
        payload["volume"] = 10
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        resp = await session.post(f"{server}/push", json=payload)
        resp.raise_for_status()


async def _send_pushcut(cfg: dict, title: str, message: str, priority: str) -> None:
    section = cfg["pushcut"]
    url = f"https://api.pushcut.io/v1/notifications/{quote(section['notification_name'], safe='')}"
    payload = {"title": title, "text": message}
    if priority != "default":
        payload["isTimeSensitive"] = True
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        resp = await session.post(url, json=payload, headers={"API-Key": section["api_key"]})
        resp.raise_for_status()


def _smtp_send_blocking(section: dict, title: str, message: str, priority: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = section["from"]
    msg["To"] = ", ".join(_email_recipients(section))
    if priority == "urgent":
        msg["X-Priority"] = "1"
        msg["Importance"] = "high"
    msg.set_content(message)
    host = section["host"]
    security = section.get("security", "starttls")
    port = int(section.get("port") or {"ssl": 465, "starttls": 587, "none": 25}[security])
    context = ssl.create_default_context()
    smtp = (smtplib.SMTP_SSL(host, port, timeout=15, context=context) if security == "ssl"
            else smtplib.SMTP(host, port, timeout=15))
    with smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        if section.get("username"):
            smtp.login(section["username"], section.get("password", ""))
        smtp.send_message(msg)


async def _send_email(cfg: dict, title: str, message: str, priority: str) -> None:
    await asyncio.to_thread(_smtp_send_blocking, cfg["email"], title, message, priority)


_SENDERS = {"ntfy": _send_ntfy, "email": _send_email, "pushover": _send_pushover,
            "bark": _send_bark, "pushcut": _send_pushcut}


class Notifier:
    def __init__(self, notify_cfg: dict | None):
        self.cfg = notify_cfg or {}
        self.topic_url = self.cfg.get("ntfy_topic_url")
        self.channels = [name for name in CHANNELS if _configured(self.cfg, name)]

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    async def send_detailed(self, title: str, message: str, priority: str = "default") -> dict[str, str | None]:
        priority = priority if priority in PRIORITIES else "default"

        async def one(name: str) -> tuple[str, str | None]:
            try:
                await _SENDERS[name](self.cfg, title, message, priority)
                return name, None
            except aiohttp.ClientResponseError as exc:
                return name, f"HTTP {exc.status}"
            except (aiohttp.ClientError, OSError, smtplib.SMTPException, asyncio.TimeoutError, ssl.SSLError) as exc:
                return name, f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                logger.exception("Nieoczekiwany błąd kanału powiadomień %s", name)
                return name, f"{type(exc).__name__}"

        results = dict(await asyncio.gather(*(one(n) for n in self.channels)))
        for name, error in results.items():
            if error:
                logger.error("Nie udało się wysłać powiadomienia (%s): %s", name, error)
        return results

    async def send(self, title: str, message: str, priority: str = "default") -> None:
        if not self.enabled:
            return
        await self.send_detailed(title, message, priority)


def _split_recipients(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\s]+", value)
    return [v.strip() for v in (value or []) if v and str(v).strip()]


def validate_channels(form: dict, existing: dict) -> tuple[dict, list[str]]:
    """Waliduje kanały z formularza; puste sekrety = zachowaj dotychczasowe. Zwraca (sekcje, błędy)."""
    errors: list[str] = []
    clean: dict = {}

    def secret(section: dict, old: dict, key: str) -> str:
        value = str(section.get(key) or "").strip()
        return value or str(old.get(key) or "")

    em = form.get("email")
    if isinstance(em, dict):
        old = existing.get("email") or {}
        enabled = bool(em.get("enabled"))
        out = {"enabled": enabled, "host": str(em.get("host") or "").strip(),
               "security": em.get("security") or "starttls", "username": str(em.get("username") or "").strip(),
               "password": secret(em, old, "password"), "from": str(em.get("from") or "").strip(),
               "to": _split_recipients(em.get("to"))}
        if out["username"] == "":
            out["password"] = ""
        try:
            out["port"] = int(em.get("port") or {"ssl": 465, "starttls": 587, "none": 25}.get(out["security"], 587))
            if not 1 <= out["port"] <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("E-mail: nieprawidłowy port")
        if out["security"] not in SECURITY_MODES:
            errors.append("E-mail: szyfrowanie musi być jednym z: " + ", ".join(SECURITY_MODES))
        if enabled:
            if not _HOST_RE.match(out["host"]):
                errors.append("E-mail: nieprawidłowy host SMTP")
            if not _EMAIL_RE.match(out["from"]):
                errors.append("E-mail: nieprawidłowy adres nadawcy")
            if not out["to"]:
                errors.append("E-mail: podaj co najmniej jednego odbiorcę")
        if any(not _EMAIL_RE.match(a) for a in out["to"]):
            errors.append("E-mail: nieprawidłowy adres odbiorcy")
        clean["email"] = out

    po = form.get("pushover")
    if isinstance(po, dict):
        old = existing.get("pushover") or {}
        out = {"enabled": bool(po.get("enabled")), "user_key": secret(po, old, "user_key"),
               "app_token": secret(po, old, "app_token"), "device": str(po.get("device") or "").strip()}
        if out["enabled"] and not (_PUSHOVER_RE.match(out["user_key"]) and _PUSHOVER_RE.match(out["app_token"])):
            errors.append("Pushover: user key i app token to 30 znaków (litery i cyfry)")
        if out["device"] and not re.match(r"^[A-Za-z0-9_\-]{1,25}$", out["device"]):
            errors.append("Pushover: nieprawidłowa nazwa urządzenia")
        clean["pushover"] = out

    bk = form.get("bark")
    if isinstance(bk, dict):
        old = existing.get("bark") or {}
        out = {"enabled": bool(bk.get("enabled")), "server": str(bk.get("server") or "").strip() or "https://api.day.app",
               "device_key": secret(bk, old, "device_key"), "group": str(bk.get("group") or "").strip()}
        if not _URL_RE.match(out["server"]):
            errors.append("Bark: adres serwera musi zaczynać się od http:// lub https://")
        if out["enabled"] and not _BARK_KEY_RE.match(out["device_key"]):
            errors.append("Bark: nieprawidłowy klucz urządzenia")
        clean["bark"] = out

    pc = form.get("pushcut")
    if isinstance(pc, dict):
        old = existing.get("pushcut") or {}
        out = {"enabled": bool(pc.get("enabled")), "api_key": secret(pc, old, "api_key"),
               "notification_name": str(pc.get("notification_name") or "").strip()}
        if out["enabled"] and not out["api_key"]:
            errors.append("Pushcut: podaj klucz API")
        if out["enabled"] and not _PUSHCUT_NAME_RE.match(out["notification_name"]):
            errors.append("Pushcut: nieprawidłowa nazwa powiadomienia")
        clean["pushcut"] = out
    return clean, errors


def public_view(cfg: dict) -> dict:
    """Konfiguracja kanałów BEZ sekretów (tylko flagi has_*)."""
    em = cfg.get("email") or {}
    po = cfg.get("pushover") or {}
    bk = cfg.get("bark") or {}
    pc = cfg.get("pushcut") or {}
    return {
        "satel_alarm": bool(cfg.get("satel_alarm", True)),
        "email": {"enabled": bool(em.get("enabled")), "host": em.get("host", ""), "port": em.get("port", 587),
                  "security": em.get("security", "starttls"), "username": em.get("username", ""),
                  "has_password": bool(em.get("password")), "from": em.get("from", ""),
                  "to": ", ".join(_email_recipients(em))},
        "pushover": {"enabled": bool(po.get("enabled")), "has_user_key": bool(po.get("user_key")),
                     "has_app_token": bool(po.get("app_token")), "device": po.get("device", "")},
        "bark": {"enabled": bool(bk.get("enabled")), "server": bk.get("server", "https://api.day.app"),
                 "has_device_key": bool(bk.get("device_key")), "group": bk.get("group", "")},
        "pushcut": {"enabled": bool(pc.get("enabled")), "has_api_key": bool(pc.get("api_key")),
                    "notification_name": pc.get("notification_name", "")},
    }
