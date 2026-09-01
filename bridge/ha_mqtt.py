"""Integracja z Home Assistant przez MQTT Discovery.

Publikuje encje (patrz ha_discovery.py) i ich stan, oraz przyjmuje komendy
sterujące dla Tuya (switch) i wyjść Satela (switch) - NIE dla stref
(świadomie, patrz ha_discovery.py). Osobny klient paho-mqtt od
EbusMqttListener/MqttBoilerPublisher - inna odpowiedzialność (sterowanie +
discovery, nie tylko odczyt danych z kotła), prościej trzymać oddzielnie.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import paho.mqtt.client as mqtt

from .resilience import HealthReporter

logger = logging.getLogger("bridge.ha_mqtt")


class HomeAssistantMqttBridge:
    def __init__(self, cfg: dict, entities: list[dict], history=None,
                 notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.cfg = cfg
        self.entities = entities
        self._command_handlers: dict[str, Callable[[str], Awaitable[None]]] = {}
        # Zapisujemy pętlę TERAZ (konstruktor wywoływany w kontekście
        # asyncio) - _on_message działa w wątku paho, gdzie
        # asyncio.get_event_loop() by się wywaliło (ten sam problem, który
        # testy złapały wcześniej w resilience.py).
        self._loop = asyncio.get_event_loop()
        self._health = HealthReporter(
            "home_assistant_mqtt", history=history, notify_cfg=notify_cfg,
            grace_seconds=grace_seconds, loop=self._loop,
        )
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)

    def register_command_handler(self, topic: str, handler: Callable[[str], Awaitable[None]]) -> None:
        """`handler` to korutyna przyjmująca treść wiadomości (str, np. 'ON'/'OFF')."""
        self._command_handlers[topic] = handler

    def start(self) -> None:
        self._client.connect_async(self.cfg["mqtt_host"], self.cfg.get("mqtt_port", 1883))
        self._client.loop_start()
        logger.info("Integracja Home Assistant (MQTT Discovery) uruchomiona")

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish_state(self, topic: str, value: str) -> None:
        self._client.publish(topic, payload=value, retain=True)

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.error("Nie udało się połączyć z MQTT (Home Assistant): kod %s", reason_code)
            self._health.report(False, f"kod błędu {reason_code}")
            return
        self._health.report(True)

        # Publikacja discovery (retained) - HA odbiera i tworzy encje
        for entity in self.entities:
            client.publish(entity["discovery_topic"], payload=json.dumps(entity["payload"]), retain=True)
        logger.info("Opublikowano %d encji Home Assistant Discovery", len(self.entities))

        # Subskrypcja tematów komend (Tuya switch, wyjścia Satela)
        for topic in self._command_handlers:
            client.subscribe(topic)
            logger.info("Subskrybuję temat komend HA: %s", topic)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self._health.report(False, f"kod {reason_code}" if reason_code != 0 else "")

    def _on_message(self, client, userdata, msg) -> None:
        handler = self._command_handlers.get(msg.topic)
        if handler is None:
            return
        payload = msg.payload.decode().strip()
        logger.info("Komenda z Home Assistant: %s = %s", msg.topic, payload)
        # Callback paho działa w OSOBNYM wątku - bezpieczne przekazanie do
        # pętli asyncio przez run_coroutine_threadsafe (ten sam wzorzec co
        # w ebus_client.py/tuya_cloud_events.py). Używamy zapisanej pętli,
        # NIE asyncio.get_event_loop() (to by się wywaliło w tym wątku).
        asyncio.run_coroutine_threadsafe(handler(payload), self._loop)
