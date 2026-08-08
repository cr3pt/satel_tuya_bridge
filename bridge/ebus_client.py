"""Odczyt danych z kotła Wolf przez eBUS.

Zamiast pisać własny dekoder protokołu eBUS (skomplikowany, specyficzny dla
producenta), wykorzystujemy `ebusd` (https://github.com/john30/ebusd) -
dojrzały, open-source'owy daemon z gotowymi definicjami dla kotłów Wolf.
ebusd łączy się z modułem eBUS podpiętym do RPi i wystawia odczytane
wartości przez MQTT (zalecane) albo swój natywny protokół TCP.

Ten moduł subskrybuje topiki MQTT publikowane przez ebusd i przekazuje
wartości dalej (do reguł automatyki oraz do publish_boiler_data.py).

Instalacja/konfiguracja ebusd z wyjściem MQTT (skrót, patrz README):
  ebusd --mqtthost=127.0.0.1 --mqttport=1883 --mqtttopic=ebusd \
        -d /dev/ttyUSB0 --scanconfig
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

import paho.mqtt.client as mqtt

from .resilience import HealthReporter

logger = logging.getLogger("bridge.ebus")


class EbusMqttListener:
    def __init__(self, cfg: dict, on_value: Callable[[dict, object], None], history=None,
                 notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.cfg = cfg
        self.on_value = on_value
        # temat MQTT -> pełna konfiguracja czujnika (name, label, targets...)
        # - patrz config.example.yaml (ebus.sensors)
        self._topic_to_sensor = {s["mqtt_topic"]: s for s in cfg["sensors"]}
        # HealthReporter dostaje referencję do bieżącej pętli asyncio -
        # callbacki paho-mqtt wykonują się w OSOBNYM wątku (własny wątek
        # sieciowy biblioteki), więc powiadomienia (korutyny) trzeba
        # planować przez run_coroutine_threadsafe, stąd jawny `loop`.
        self._health = HealthReporter(
            "ebus_mqtt", history=history, notify_cfg=notify_cfg,
            grace_seconds=grace_seconds, loop=asyncio.get_event_loop(),
        )
        self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        # Wbudowany w paho-mqtt automatyczny reconnect (używany przez
        # loop_start) - exponential backoff między 1s a 120s.
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)

    def start(self) -> None:
        # connect_async + loop_start: nie blokuje startu procesu jeśli
        # broker MQTT nie jest jeszcze gotowy (np. ebusd startuje wolniej
        # niż bridge po restarcie RPi) - połączenie i reconnecty dzieją
        # się w tle automatycznie.
        self._client.connect_async(self.cfg["mqtt_host"], self.cfg.get("mqtt_port", 1883))
        self._client.loop_start()
        logger.info("Nasłuch MQTT ebusd uruchomiony (%s:%s)", self.cfg["mqtt_host"], self.cfg.get("mqtt_port", 1883))

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            logger.error("Nie udało się połączyć z MQTT ebusd (kod %s)", rc)
            self._health.report(False, f"kod błędu {rc}")
            return
        self._health.report(True)
        for topic in self._topic_to_sensor:
            client.subscribe(topic)
            logger.info("Subskrybuję topik ebusd: %s", topic)

    def _on_disconnect(self, client, userdata, rc) -> None:
        if rc != 0:
            logger.warning("Utracono połączenie MQTT z ebusd (kod %s) - paho ponowi próbę automatycznie", rc)
        self._health.report(False, f"kod {rc}" if rc != 0 else "")

    def _on_message(self, client, userdata, msg) -> None:
        sensor_cfg = self._topic_to_sensor.get(msg.topic)
        if sensor_cfg is None:
            return
        raw = msg.payload.decode().strip()
        try:
            value: object = float(raw)
        except ValueError:
            # Nie każde pole eBUS jest liczbą (np. status palnika może być
            # tekstem typu "heating"/"idle") - przekazujemy dalej jako
            # tekst; handle_boiler_value() w rules_engine.py sam decyduje,
            # co da się z takim polem zrobić (np. pomija numeryczny alarm
            # progowy, ale nadal zapisuje status i publikuje wartość).
            value = raw
        logger.debug("Odczyt eBUS: %s = %s", sensor_cfg["name"], value)
        self.on_value(sensor_cfg, value)
