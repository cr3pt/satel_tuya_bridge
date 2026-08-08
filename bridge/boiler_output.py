"""Wystawianie danych z kotła Wolf dalej.

O tym, CO trafia dokąd, decyduje teraz konfiguracja `targets` PRZY KAŻDYM
CZUJNIKU w config.yaml (ebus.sensors) - nie ma już globalnego "trybu" tu.
Ten moduł tylko implementuje same kanały wyjściowe:

  MQTT (ZALECANE) - publikuje wartości na lokalny broker MQTT jako
  proste sensory. Stamtąd bardzo łatwo podłączyć Home Assistant (auto-
  discovery), Node-RED, dashboard, itd. Stabilne, lokalne, bez zależności
  od chmury Tuya.

  Wirtualne urządzenie Tuya - próba wystawienia danych jako prawdziwe
  urządzenie w ekosystemie/aplikacji Tuya. WAŻNE ograniczenia, o których
  warto wiedzieć zanim się na to zdecydujesz:
    - Tuya nie oferuje żadnego "prostego" sposobu na zarejestrowanie
      dowolnego urządzenia jako w pełni działającego bez certyfikacji
      sprzętu. Funkcja "Virtual Device" na Tuya IoT Platform jest
      pomyślana jako narzędzie deweloperskie do TESTOWANIA integracji,
      nie jako trwałe rozwiązanie produkcyjne dla smart home.
    - Musisz ręcznie utworzyć takie urządzenie w panelu Tuya IoT Platform
      i połączyć je ze swoim projektem/kontem zanim ten kod zacznie
      działać - to nie da się w pełni zautomatyzować z zewnątrz.
    - Może przestać działać przy zmianach polityki Tuya względem kont
      deweloperskich/limitów.
  Kod poniżej AKTUALIZUJE stan takiego już istniejącego urządzenia
  (device shadow) przez Tuya Cloud API - nie tworzy go od zera.
"""
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
        self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)
        self._client.connect_async(mqtt_cfg["host"], mqtt_cfg.get("port", 1883))
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        self._health.report(rc == 0, f"kod {rc}" if rc != 0 else "")

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._health.report(False, f"kod {rc}" if rc != 0 else "")

    def publish(self, name: str, value) -> None:
        topic = f"{self.topic_prefix}/{name}"
        self._client.publish(topic, payload=str(value), retain=True)
        logger.debug("Opublikowano %s = %s", topic, value)


class TuyaVirtualDevicePublisher:
    """Aktualizuje DP-y istniejącego, ręcznie utworzonego wirtualnego
    urządzenia Tuya. Wymaga konta developera Tuya - patrz docstring modułu.

    W przeciwieństwie do wcześniejszej wersji, kod DP NIE jest już
    wyszukiwany po nazwie czujnika w wewnętrznej mapie - jest przekazywany
    wprost przy każdym wywołaniu (patrz `sensor.targets.tuya_dp` w
    config.yaml), bo to samo pole eBUS teoretycznie mogłoby trafiać do
    różnych DP w różnych kontekstach."""

    def __init__(self, cloud_cfg: dict, virtual_device_cfg: dict):
        from tuya_connector import TuyaOpenAPI  # type: ignore

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
            # Chmura Tuya bywa niedostępna chwilowo - nie chcemy przez to
            # tracić lokalnej publikacji MQTT, więc tylko logujemy, nie
            # przerywamy działania bridge'a.
            logger.exception("Wyjątek przy aktualizacji wirtualnego urządzenia Tuya")


def build_boiler_publishers(config, history=None) -> dict:
    """Buduje publishery na podstawie tego, czego FAKTYCZNIE potrzebują
    czujniki w ebus.sensors (nie ma już globalnego przełącznika trybu) -
    MQTT buduje się, jeśli przynajmniej jeden czujnik ma
    targets.mqtt_publish=true, analogicznie dla Tuya. Zwraca słownik
    {"mqtt": ..., "tuya": ...} - brakujący klucz = kanał nieużywany."""
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
            # TuyaOpenAPI.connect() łączy się z chmurą Tuya OD RAZU przy
            # konstrukcji - błędne/placeholder poświadczenia albo brak
            # internetu w tym momencie NIE MOGĄ wywalić całego bridge'a
            # (Satel i Tuya lokalnie nie mają z wirtualnym urządzeniem nic
            # wspólnego i powinny normalnie wystartować). Ten kanał po
            # prostu zostaje wyłączony - reszta systemu działa dalej.
            logger.exception(
                "Nie udało się zainicjować wirtualnego urządzenia Tuya - ten kanał będzie "
                "wyłączony, ale reszta bridge'a wystartuje normalnie. Sprawdź poświadczenia "
                "w config.yaml (tuya.cloud) i połączenie z internetem."
            )

    return publishers
