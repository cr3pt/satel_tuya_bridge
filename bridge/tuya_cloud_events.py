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
        self._loop = asyncio.get_event_loop()
        self._auth_error: str | None = None
        self._supervisor = Supervisor("tuya_cloud", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)

    async def run_forever(self) -> None:

        async def _start_and_wait(on_connected) -> None:
            import asyncio
            from tuya_connector import TuyaOpenPulsar, TuyaCloudPulsarTopic

            logging.getLogger("websocket").setLevel(logging.CRITICAL)

            self._auth_error = None
            self._pulsar = TuyaOpenPulsar(
                self.cloud_cfg["access_id"],
                self.cloud_cfg["access_key"],
                self.cloud_cfg["mq_endpoint"],
                TuyaCloudPulsarTopic.PROD,
            )
            self._wrap_error_handler()
            self._pulsar.add_message_listener(self._handle_message)
            self._pulsar.start()
            logger.info("Nasłuch zdarzeń kamer Tuya (Pulsar) uruchomiony")
            await on_connected()
            while True:
                await asyncio.sleep(15)
                if self._auth_error:
                    raise ConnectionError(
                        "Błąd uwierzytelniania Tuya Cloud Pulsar (401 Unauthorized): "
                        f"{self._auth_error}. Sprawdź: (1) czy Access ID/Access Secret "
                        "są przepisane poprawnie (bez spacji na początku/końcu), "
                        "(2) czy w projekcie na developer.tuya.com w zakładce "
                        "'Cloud' -> Twój projekt -> 'Service API' włączona jest "
                        "subskrypcja 'Message Service' oraz czy kamery są powiązane "
                        "z projektem, (3) czy mq_endpoint odpowiada regionowi "
                        "projektu - patrz sekcja 'Konto Tuya Cloud' w README."
                    )
                thread = getattr(self._pulsar, "_TuyaOpenPulsar__client_thread", None)
                if thread is not None and not thread.is_alive():
                    raise ConnectionError("Wątek klienta Pulsar zakończył działanie")

        await self._supervisor.run(_start_and_wait)

    def _wrap_error_handler(self) -> None:
        original_on_error = self._pulsar._on_error

        def wrapped_on_error(ws_app, error):
            text = str(error)
            if "401" in text or "Unauthorized" in text:
                self._auth_error = text
            return original_on_error(ws_app, error)

        self._pulsar._on_error = wrapped_on_error
        self._pulsar.ws_app.on_error = wrapped_on_error

    def stop(self) -> None:
        if self._pulsar:
            self._pulsar.stop()

    def _handle_message(self, msg: dict) -> None:
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
