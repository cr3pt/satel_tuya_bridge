"""Wspólny mechanizm powiadomień push (ntfy.sh lub self-hosted).

Wydzielony do osobnego modułu, bo korzystają z niego zarówno alarmy
progowe (bridge/alarms.py), jak i monitoring "zdrowia" połączeń
(bridge/resilience.py) oraz statusu urządzeń Tuya (rules_engine.py) -
nie chcemy trzech kopii tego samego kodu wysyłki HTTP.
"""
from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger("bridge.notifier")


class Notifier:
    def __init__(self, notify_cfg: dict | None):
        self.topic_url = (notify_cfg or {}).get("ntfy_topic_url")

    @property
    def enabled(self) -> bool:
        return bool(self.topic_url)

    async def send(self, title: str, message: str, priority: str = "default") -> None:
        if not self.enabled:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    self.topic_url,
                    data=message.encode("utf-8"),
                    headers={"Title": title, "Priority": priority},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except aiohttp.ClientError as exc:
            logger.error("Nie udało się wysłać powiadomienia ntfy: %s", exc)
