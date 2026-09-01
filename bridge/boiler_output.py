from __future__ import annotations

import asyncio
import logging

import paho.mqtt.client as mqtt

from .resilience import HealthReporter

logger = logging.getLogger("bridge.boiler_output")


class MqttBoilerPublisher:
    def __init__(self, mqtt_cfg: dict, history=None, notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.topic_prefix = mqtt_cfg.get("topic_prefix", "home/boiler")
        self._health = HealthReporter(
            "mqtt_publish", history=history, notify_cfg=notify_cfg,
            grace_seconds=grace_seconds, loop=asyncio.get_event_loop(),
        )
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)
        self._client.connect_async(mqtt_cfg["host"], mqtt_cfg.get("port", 1883))
        self._client.loop_start()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        self._health.report(reason_code == 0, f"kod {reason_code}" if reason_code != 0 else "")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self._health.report(False, f"kod {reason_code}" if reason_code != 0 else "")

    def publish(self, name: str, value) -> None:
        topic = f"{self.topic_prefix}/{name}"
        self._client.publish(topic, payload=str(value), retain=True)
        logger.debug("Opublikowano %s = %s", topic, value)


class TuyaVirtualDevicePublisher:

    def __init__(self, cloud_cfg: dict, virtual_device_cfg: dict):
        from tuya_connector import TuyaOpenAPI

        self.device_id = virtual_device_cfg["device_id"]
        self.api = TuyaOpenAPI(
            cloud_cfg["api_endpoint"], cloud_cfg["access_id"], cloud_cfg["access_key"]
        )
        self.api.connect()

    def publish_to_dp(self, dp_code: str, value) -> None:
        commands = {"commands": [{"code": dp_code, "value": value}]}
        try:
            resp = self.api.post(f"/v1.0/devices/{self.device_id}/commands", commands)
            if not resp.get("success"):
                logger.error("Błąd aktualizacji wirtualnego urządzenia Tuya (DP %s): %s", dp_code, resp)
            else:
                logger.debug("Zaktualizowano DP %s = %s na wirtualnym urządzeniu Tuya", dp_code, value)
        except Exception:
            logger.exception("Wyjątek przy aktualizacji wirtualnego urządzenia Tuya")


def build_boiler_publishers(config, history=None) -> dict:
    notify_cfg = config.raw.get("notifications")
    sensors = config.ebus.get("sensors", [])
    needs_mqtt = any(s.get("targets", {}).get("mqtt_publish") for s in sensors)
    needs_tuya = any(s.get("targets", {}).get("tuya_virtual_device") for s in sensors)

    publishers = {}
    boiler_cfg = config.boiler_output

    if needs_mqtt:
        publishers["mqtt"] = MqttBoilerPublisher(boiler_cfg["mqtt"], history=history, notify_cfg=notify_cfg)

    if needs_tuya:
        try:
            publishers["tuya"] = TuyaVirtualDevicePublisher(config.tuya["cloud"], boiler_cfg["tuya_virtual_device"])
        except Exception:
            logger.exception(
                "Nie udało się zainicjować wirtualnego urządzenia Tuya - ten kanał będzie "
                "wyłączony, ale reszta bridge'a wystartuje normalnie. Sprawdź poświadczenia "
                "w config.yaml (tuya.cloud) i połączenie z internetem."
            )

    return publishers
