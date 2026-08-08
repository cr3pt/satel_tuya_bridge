"""Zdarzenia ruchu z kamer UniFi Protect (biblioteka `uiprotect`, nieoficjalna,
używana też przez integrację UniFi Protect w Home Assistant). Połączenie
przez websocket do kontrolera - lekkie, nadaje się na RPi Zero W.
"""
from __future__ import annotations

import logging
from typing import Callable, Awaitable

from .resilience import Supervisor

logger = logging.getLogger("bridge.unifi_protect")


class UnifiProtectMotionListener:
    def __init__(self, cfg: dict, on_motion: Callable[[str], Awaitable[None]], history=None,
                 notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.cfg = cfg
        self.on_motion = on_motion
        self._api = None
        self._supervisor = Supervisor("unifi_protect", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)

    async def run_forever(self) -> None:
        """Utrzymuje połączenie z kontrolerem UniFi Protect z automatycznym
        restartem po awarii (patrz bridge/resilience.py)."""

        async def _connect_and_wait(on_connected) -> None:
            import asyncio
            from uiprotect import ProtectApiClient  # type: ignore

            self._api = ProtectApiClient(
                self.cfg["host"],
                443,
                self.cfg["username"],
                self.cfg["password"],
                verify_ssl=self.cfg.get("verify_ssl", False),
            )
            await self._api.update()
            self._api.subscribe_websocket(self._handle_update)
            logger.info("Nasłuch zdarzeń UniFi Protect uruchomiony")
            await on_connected()
            # subscribe_websocket działa w tle - czekamy tu w nieskończoność,
            # a jeśli klient wewnętrznie zgłosi problem (wyjątek w update()
            # przy kolejnej próbie odświeżenia), pętla poniżej to wykryje.
            while True:
                await asyncio.sleep(60)
                # Lekki "ping" - jeśli kontroler jest nieosiągalny, update()
                # rzuci wyjątkiem i supervisor złapie to jako rozłączenie.
                await self._api.update()

        await self._supervisor.run(_connect_and_wait)

    async def _handle_update(self, updates) -> None:
        # `updates` to obiekt WSSubscriptionMessage z uiprotect - zawiera
        # zmienione atrybuty modelu (kamery). Interesuje nas last_motion.
        try:
            obj = getattr(updates, "new_obj", None)
            if obj is None:
                return
            if getattr(obj, "is_motion_detected", False):
                camera_id = getattr(obj, "id", None)
                logger.info("Wykryto ruch na kamerze UniFi: %s", camera_id)
                await self.on_motion(camera_id)
        except Exception:
            logger.exception("Błąd przetwarzania zdarzenia UniFi Protect")
