from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable

import paho.mqtt.client as mqtt

from .resilience import HealthReporter

logger = logging.getLogger("bridge.ebus")


def discover_topics(mqtt_host: str, mqtt_port: int = 1883, topic_prefix: str = "ebusd",
                     timeout_seconds: float = 4.0) -> list[str]:
    topics: set[str] = set()

    def _on_message(client, userdata, msg) -> None:
        topics.add(msg.topic)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = _on_message
    try:
        client.connect(mqtt_host, mqtt_port, keepalive=max(int(timeout_seconds) + 5, 10))
    except OSError as exc:
        raise ConnectionError(f"Nie udało się połączyć z brokerem MQTT {mqtt_host}:{mqtt_port}: {exc}") from exc

    client.subscribe(f"{topic_prefix}/#")
    client.loop_start()
    try:
        time.sleep(timeout_seconds)
    finally:
        client.loop_stop()
        client.disconnect()

    return sorted(topics)


class BackgroundTopicDiscovery:

    def __init__(self):
        self._lock = threading.Lock()
        self._client: mqtt.Client | None = None
        self._topics: set[str] = set()
        self._running = False
        self._started_at: float | None = None
        self._duration_seconds: float = 0.0
        self._timer: threading.Timer | None = None

    def start(self, mqtt_host: str, mqtt_port: int = 1883, topic_prefix: str = "ebusd",
              duration_seconds: float = 300.0) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("Nasłuch już trwa - poczekaj na koniec albo zatrzymaj go najpierw")

            def _on_message(client, userdata, msg) -> None:
                with self._lock:
                    self._topics.add(msg.topic)

            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.on_message = _on_message
            try:
                client.connect(mqtt_host, mqtt_port, keepalive=max(int(duration_seconds) + 30, 60))
            except OSError as exc:
                raise ConnectionError(f"Nie udało się połączyć z brokerem MQTT {mqtt_host}:{mqtt_port}: {exc}") from exc

            client.subscribe(f"{topic_prefix}/#")
            client.loop_start()

            self._topics = set()
            self._client = client
            self._running = True
            self._started_at = time.time()
            self._duration_seconds = duration_seconds

            timer = threading.Timer(duration_seconds, self._stop_internal)
            timer.daemon = True
            timer.start()
            self._timer = timer

    def _stop_internal(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.loop_stop()
                self._client.disconnect()
                self._client = None
            self._running = False

    def stop(self) -> dict:
        if self._timer is not None:
            self._timer.cancel()
        self._stop_internal()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self._started_at) if self._started_at is not None else 0.0
            return {
                "running": self._running,
                "topics": sorted(self._topics),
                "elapsed_seconds": round(elapsed, 1),
                "duration_seconds": self._duration_seconds,
            }


def _is_named_value_bundle(parsed: dict) -> bool:
    return bool(parsed) and all(
        isinstance(v, dict) and "name" in v and "value" in v for v in parsed.values()
    )


def _format_named_value_bundle(parsed: dict) -> str:
    parts = [
        f"{v['name']}: {v['value']}" for v in parsed.values()
        if str(v.get("name", "")).strip() != "N_A"
    ]
    return " | ".join(parts)


class EbusMqttListener:
    def __init__(self, cfg: dict, on_value: Callable[[dict, object], None], history=None,
                 notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.cfg = cfg
        self.on_value = on_value
        self._topic_to_sensor = {s["mqtt_topic"]: s for s in cfg["sensors"]}
        self._health = HealthReporter(
            "ebus_mqtt", history=history, notify_cfg=notify_cfg,
            grace_seconds=grace_seconds, loop=asyncio.get_event_loop(),
        )
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)

    def update_sensors(self, cfg: dict) -> None:
        old_topics = set(self._topic_to_sensor)
        self.cfg = cfg
        self._topic_to_sensor = {s["mqtt_topic"]: s for s in cfg["sensors"]}
        for topic in set(self._topic_to_sensor) - old_topics:
            self._client.subscribe(topic)
            logger.info("Subskrybuję nowy topik ebusd (przeładowanie configu): %s", topic)

    def start(self) -> None:
        self._client.connect_async(self.cfg["mqtt_host"], self.cfg.get("mqtt_port", 1883))
        self._client.loop_start()
        logger.info("Nasłuch MQTT ebusd uruchomiony (%s:%s)", self.cfg["mqtt_host"], self.cfg.get("mqtt_port", 1883))

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.error("Nie udało się połączyć z MQTT ebusd (kod %s)", reason_code)
            self._health.report(False, f"kod błędu {reason_code}")
            return
        self._health.report(True)
        for topic in self._topic_to_sensor:
            client.subscribe(topic)
            logger.info("Subskrybuję topik ebusd: %s", topic)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.warning("Utracono połączenie MQTT z ebusd (kod %s) - paho ponowi próbę automatycznie", reason_code)
        self._health.report(False, f"kod {reason_code}" if reason_code != 0 else "")

    def _on_message(self, client, userdata, msg) -> None:
        sensor_cfg = self._topic_to_sensor.get(msg.topic)
        if sensor_cfg is None:
            return
        raw = msg.payload.decode().strip()
        value = self._parse_payload(raw, sensor_cfg)
        if value is None:
            return
        logger.debug("Odczyt eBUS: %s = %s", sensor_cfg["name"], value)
        self.on_value(sensor_cfg, value)

    @staticmethod
    def _parse_payload(raw: str, sensor_cfg: dict) -> object | None:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw

        if not isinstance(parsed, dict):
            return parsed

        mqtt_field = (sensor_cfg.get("mqtt_field") or "").strip()
        if mqtt_field:
            if mqtt_field not in parsed:
                logger.warning(
                    "Temat %s: skonfigurowane pole '%s' (czujnik '%s') nie występuje w tej "
                    "wiadomości JSON (dostępne pola: %s)",
                    sensor_cfg["mqtt_topic"], mqtt_field, sensor_cfg["name"], ", ".join(sorted(parsed)) or "brak",
                )
                return None
            field_value = parsed[mqtt_field]
        elif len(parsed) == 1:
            field_value = next(iter(parsed.values()))
        elif _is_named_value_bundle(parsed):
            return _format_named_value_bundle(parsed) or None
        else:
            logger.warning(
                "Temat %s: wiadomość JSON zawiera kilka pól (%s), ale czujnik '%s' nie ma "
                "ustawionego 'mqtt_field' - nie wiadomo, które wybrać, pomijam",
                sensor_cfg["mqtt_topic"], ", ".join(sorted(parsed)), sensor_cfg["name"],
            )
            return None

        if isinstance(field_value, dict) and "value" in field_value:
            return field_value["value"]
        return field_value
