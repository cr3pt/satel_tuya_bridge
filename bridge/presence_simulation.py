"""Symulacja obecności - losowe włączanie/wyłączanie wybranych urządzeń
Tuya w oknie czasowym (np. wieczorami), gdy funkcja jest włączona.

Celowo NIEZALEŻNA od stanu uzbrojenia centrali Satel - włączasz/wyłączasz
ją ręcznie w config.yaml (`presence_simulation.enabled`), np. na czas
wyjazdu. Prostsze i bardziej przewidywalne niż automatyczne wiązanie z
trybem uzbrojenia (które i tak włącza się/wyłącza też wtedy, gdy ktoś w
domu tylko wychodzi na chwilę i uzbraja "na wszelki wypadek")."""
from __future__ import annotations

import asyncio
import logging
import random

from .scheduling import is_within_window

logger = logging.getLogger("bridge.presence_simulation")


class PresenceSimulation:
    def __init__(self, cfg: dict, tuya, location_cfg: dict | None = None, history=None):
        self.cfg = cfg or {}
        self.tuya = tuya
        self.location_cfg = location_cfg
        self.history = history

    async def run_forever(self) -> None:
        if not self.cfg.get("enabled"):
            logger.info("Symulacja obecności wyłączona (presence_simulation.enabled: false)")
            return
        devices = self.cfg.get("devices", [])
        if not devices:
            logger.warning("Symulacja obecności włączona, ale brak urządzeń w presence_simulation.devices")
            return

        logger.info("Symulacja obecności uruchomiona (%d urządzeń)", len(devices))
        while True:
            if is_within_window(self.cfg.get("window"), self.location_cfg):
                device = random.choice(devices)
                turn_on = random.random() < 0.5
                try:
                    if turn_on:
                        await self.tuya.turn_on(device)
                    else:
                        await self.tuya.turn_off(device)
                    logger.info("Symulacja obecności: %s -> %s", device, "włączone" if turn_on else "wyłączone")
                    if self.history:
                        self.history.write_event(
                            "presence_simulation", source=device, detail={"turned_on": turn_on},
                        )
                except Exception:
                    logger.exception("Błąd symulacji obecności dla '%s'", device)

            lo = self.cfg.get("min_interval_minutes", 10)
            hi = self.cfg.get("max_interval_minutes", 40)
            await asyncio.sleep(random.uniform(lo, hi) * 60)
