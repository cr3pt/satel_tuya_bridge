# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

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
        self.history = history
        self._command_handlers: dict[str, Callable[[str], Awaitable[None]]] = {}
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
        self._record_topic(topic, value)

    def publish_raw(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self._client.publish(topic, payload=payload, qos=qos, retain=retain)
        self._record_topic(topic, payload)

    def _record_topic(self, topic: str, payload) -> None:
        if self.history is not None:
            self.history.set_status(f"mqtt:{topic}", "mqtt_topic", payload, label=topic)

    def publish_discovery(self, entity: dict) -> None:
        """Dodaje encję w trakcie działania (np. pole licznika wM-Bus znane
        dopiero po pierwszym telegramie); ponownie publikowana po reconnect."""
        self.entities.append(entity)
        payload = json.dumps(entity["payload"])
        self._client.publish(entity["discovery_topic"], payload=payload, retain=True)
        self._record_topic(entity["discovery_topic"], payload)

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.error("Nie udało się połączyć z MQTT (Home Assistant): kod %s", reason_code)
            self._health.report(False, f"kod błędu {reason_code}")
            return
        self._health.report(True)

        for entity in self.entities:
            payload = json.dumps(entity["payload"])
            client.publish(entity["discovery_topic"], payload=payload, retain=True)
            self._record_topic(entity["discovery_topic"], payload)
        logger.info("Opublikowano %d encji Home Assistant Discovery", len(self.entities))

        for topic in self._command_handlers:
            client.subscribe(topic)
            logger.info("Subskrybuję temat komend HA: %s", topic)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self._health.report(False, f"kod {reason_code}" if reason_code != 0 else "")

    def _on_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode().strip()
        self._record_topic(msg.topic, payload)
        handler = self._command_handlers.get(msg.topic)
        if handler is None:
            return
        logger.info("Komenda z Home Assistant: %s = %s", msg.topic, payload)
        asyncio.run_coroutine_threadsafe(handler(payload), self._loop)
