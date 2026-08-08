"""Budowanie encji Home Assistant przez MQTT Discovery.

Home Assistant nasłuchuje na `<discovery_prefix>/<component>/<object_id>/config`
- publikacja retained wiadomości JSON na taki temat automatycznie tworzy
encję w HA, bez ręcznego pisania configuration.yaml po stronie HA.

Świadomie NIE publikujemy `alarm_control_panel` dla stref Satela - to
ominęłoby autoryzację panelu webowego (MQTT nie ma wbudowanego mechanizmu
hasła per-żądanie jak nasz `/api/satel/arm`). Strefy trafiają do HA jako
`binary_sensor` (tylko odczyt - "uzbrojona: tak/nie"). Jeśli chcesz
uzbrajać z poziomu Home Assistant, użyj integracji `rest_command` w HA,
żeby wywoływała nasz UWIERZYTELNIONY endpoint `/api/satel/arm` - patrz
README, sekcja "Home Assistant".

Wszystkie encje grupowane są pod jednym urządzeniem w HA (ten sam blok
`device` w każdym payloadzie) - dzięki temu w interfejsie HA widać je
razem, nie jako rozproszoną listę.
"""
from __future__ import annotations


def _device_block(cfg: dict) -> dict:
    return {
        "identifiers": ["satel_tuya_bridge"],
        "name": cfg.get("device_name", "Satel-Tuya-eBUS Bridge"),
        "manufacturer": "DIY",
        "model": "satel_tuya_bridge",
    }


def build_boiler_sensor_entities(sensors_cfg: list[dict], ha_cfg: dict, boiler_topic_prefix: str) -> list[dict]:
    """Jeden sensor HA per czujnik eBUS z targets.mqtt_publish=true (bo
    tylko takie faktycznie trafiają na MQTT - patrz boiler_output.py)."""
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for sensor in sensors_cfg:
        if not sensor.get("targets", {}).get("mqtt_publish"):
            continue
        name = sensor["name"]
        label = sensor.get("label", name)
        object_id = f"boiler_{name}"
        is_temperature = "temp" in name.lower() or "temp" in label.lower()

        payload = {
            "name": label,
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": f"{boiler_topic_prefix}/{name}",
            "device": _device_block(ha_cfg),
        }
        if is_temperature:
            payload["device_class"] = "temperature"
            payload["unit_of_measurement"] = "°C"
            payload["state_class"] = "measurement"

        entities.append({
            "discovery_topic": f"{prefix}/sensor/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": payload["state_topic"],
            "command_topic": None,
        })
    return entities


def build_tuya_switch_entities(tuya_devices_cfg: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    """Jeden switch HA per urządzenie Tuya - Z możliwością sterowania
    (command_topic), bo to ten sam poziom ryzyka co fizyczne kliknięcie
    włącznika, nie funkcja bezpieczeństwa jak uzbrojenie alarmu."""
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name in tuya_devices_cfg:
        object_id = f"tuya_{name}"
        state_topic = f"{topic_prefix}/tuya/{name}/state"
        command_topic = f"{topic_prefix}/tuya/{name}/set"
        payload = {
            "name": name.replace("_", " ").title(),
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "command_topic": command_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/switch/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": command_topic,
        })
    return entities


def build_satel_input_entities(monitored_zones: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    """Wejścia (czujki) jako binary_sensor - tylko odczyt, sensowne
    (fizyczna czujka nie da się "włączyć" zdalnie)."""
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name, number in monitored_zones.items():
        object_id = f"satel_input_{number}"
        state_topic = f"{topic_prefix}/satel/input/{number}/state"
        payload = {
            "name": f"Czujka: {name}",
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "motion",
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/binary_sensor/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": None,
        })
    return entities


def build_satel_output_entities(monitored_outputs: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    """Wyjścia Satela jako switch - sterowalne (to przekaźnik, nie funkcja
    bezpieczeństwa - podobny poziom ryzyka co włącznik Tuya)."""
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name, number in monitored_outputs.items():
        object_id = f"satel_output_{number}"
        state_topic = f"{topic_prefix}/satel/output/{number}/state"
        command_topic = f"{topic_prefix}/satel/output/{number}/set"
        payload = {
            "name": f"Wyjście: {name}",
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "command_topic": command_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/switch/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": command_topic,
        })
    return entities


def build_satel_partition_entities(partitions: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    """Strefy jako binary_sensor - CELOWO tylko odczyt (uzasadnienie w
    docstringu modułu). Sterowanie -> patrz README, sekcja Home Assistant."""
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name, number in partitions.items():
        object_id = f"satel_partition_{number}"
        state_topic = f"{topic_prefix}/satel/partition/{number}/state"
        payload = {
            "name": f"Strefa uzbrojona: {name}",
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "safety",
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/binary_sensor/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": None,
        })
    return entities


def build_all_entities(config, boiler_topic_prefix: str) -> list[dict]:
    """Zbiera wszystkie encje na podstawie pełnej konfiguracji bridge'a."""
    ha_cfg = config.raw.get("home_assistant", {})
    topic_prefix = ha_cfg.get("state_topic_prefix", "home/bridge")

    entities = []
    entities += build_boiler_sensor_entities(config.ebus.get("sensors", []), ha_cfg, boiler_topic_prefix)
    entities += build_tuya_switch_entities(config.tuya.get("devices", {}), ha_cfg, topic_prefix)
    entities += build_satel_input_entities(config.satel.get("monitored_zones", {}), ha_cfg, topic_prefix)
    entities += build_satel_output_entities(config.satel.get("monitored_outputs", {}), ha_cfg, topic_prefix)
    entities += build_satel_partition_entities(config.satel.get("partitions", {}), ha_cfg, topic_prefix)
    return entities
