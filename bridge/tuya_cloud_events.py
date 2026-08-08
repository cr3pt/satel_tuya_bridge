"""Zdarzenia ruchu z kamer Tuya.

WAŻNE ograniczenie: w przeciwieństwie do przełączników/gniazdek (sterowanych
lokalnie przez tinytuya), zdarzenia detekcji z kamer Tuya (ruch, sylwetka
osoby) NIE są dostępne lokalnie w sieci LAN. Wymagają:
  1. konta na developer.tuya.com (Tuya IoT Development Platform),
  2. utworzenia projektu chmurowego i powiązania z nim Twoich kamer
     (przez połączenie z kontem w aplikacji Tuya Smart/Smart Life),
  3. włączenia Message Service (subskrypcja Pulsar) dla projektu.

Zdarzenie ruchu przychodzi jako wiadomość Pulsar z polem zbliżonym do
`alarm_info_report` / `edge_device_alarm` w zależności od modelu kamery -
dokładny kształt payloadu warto zweryfikować podglądając surowe wiadomości
w zakładce "Cloud Debugging" na platformie Tuya dla Twoich konkretnych
kamer, bo różni się między modelami/firmware.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from .resilience import Supervisor

logger = logging.getLogger("bridge.tuya_cloud")


class TuyaCameraMotionListener:
    def __init__(self, cloud_cfg: dict, on_motion: Callable[[str], Awaitable[None]], history=None,
                 notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.cloud_cfg = cloud_cfg
        self.on_motion = on_motion
        self.history = history
        self._pulsar = None
        # Zapisujemy pętlę TERAZ (konstruktor wywoływany w kontekście
        # asyncio, wewnątrz RulesEngine.start()) - _handle_message działa
        # w wątku biblioteki Pulsar, gdzie asyncio.get_event_loop() rzuca
        # RuntimeError w Pythonie 3.12 (ten sam błąd, jaki testy złapały
        # wcześniej w bridge/resilience.py - poprawiony tu prewencyjnie).
        self._loop = asyncio.get_event_loop()
        self._supervisor = Supervisor("tuya_cloud", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)

    async def run_forever(self) -> None:
        """Uruchamia nasłuch Pulsar z automatycznym restartem po awarii.
        Biblioteka tuya-connector-python ma pewien wbudowany reconnect na
        poziomie WebSocket, ale nie jest to udokumentowane wystarczająco
        pewnie, żeby polegać na nim w 100% - supervisor jest dodatkową
        siatką bezpieczeństwa: jeśli klient Pulsar padnie/zawiesi się,
        zostanie odtworzony od zera po backoffie."""

        async def _start_and_wait(on_connected) -> None:
            import asyncio
            from tuya_connector import TuyaOpenPulsar, TuyaCloudPulsarTopic  # type: ignore

            self._pulsar = TuyaOpenPulsar(
                self.cloud_cfg["access_id"],
                self.cloud_cfg["access_key"],
                self.cloud_cfg["mq_endpoint"],
                TuyaCloudPulsarTopic.PROD,
            )
            self._pulsar.add_message_listener(self._handle_message)
            self._pulsar.start()
            logger.info("Nasłuch zdarzeń kamer Tuya (Pulsar) uruchomiony")
            await on_connected()
            # TuyaOpenPulsar.start() działa w tle (własny wątek) - żeby
            # supervisor mógł wykryć "śmierć" wątku, czekamy tu i
            # okresowo sprawdzamy czy wątek nadal żyje.
            # UWAGA: nazwa atrybutu wątku poniżej jest NIEZWERYFIKOWANA
            # (nie ma pewnego publicznego API do tego w tuya-connector-python)
            # - potraktuj to jako best-effort. Jeśli atrybut nie istnieje,
            # `thread` zostanie None i ta konkretna kontrola po prostu nie
            # zadziała - supervisor nadal złapie inne rodzaje awarii
            # (wyjątki przy starcie), tylko nie "cichą śmierć" wątku.
            while True:
                await asyncio.sleep(15)
                thread = getattr(self._pulsar, "_TuyaOpenPulsar__client_thread", None)
                if thread is not None and not thread.is_alive():
                    raise ConnectionError("Wątek klienta Pulsar zakończył działanie")

        await self._supervisor.run(_start_and_wait)

    def stop(self) -> None:
        if self._pulsar:
            self._pulsar.stop()

    def _handle_message(self, msg: dict) -> None:
        # Uruchamiane w wątku biblioteki Pulsar (nie asyncio) - trzeba
        # bezpiecznie przekazać zdarzenie do pętli asyncio. Używamy
        # zapisanej w __init__ pętli, NIE asyncio.get_event_loop() (patrz
        # komentarz przy self._loop w __init__).
        import asyncio

        try:
            payload = msg.get("data", {})
            device_id = payload.get("devId") or payload.get("deviceId")
            camera_ids = self.cloud_cfg.get("camera_device_ids", [])
            if device_id in camera_ids:
                logger.info("Wykryto zdarzenie z kamery Tuya: %s", device_id)
                asyncio.run_coroutine_threadsafe(self.on_motion(device_id), self._loop)
        except Exception:
            logger.exception("Błąd przetwarzania wiadomości Pulsar")
