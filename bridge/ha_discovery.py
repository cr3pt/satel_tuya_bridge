from __future__ import annotations

from .wmbus import detect_phase, field_unit


def _device_block(cfg: dict) -> dict:
    return {
        "identifiers": ["satel_tuya_bridge"],
        "name": cfg.get("device_name", "Satel-Tuya-eBUS Bridge"),
        "manufacturer": "DIY",
        "model": "satel_tuya_bridge",
    }


def build_boiler_sensor_entities(sensors_cfg: list[dict], ha_cfg: dict, boiler_topic_prefix: str) -> list[dict]:
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


def build_homewhiz_sensor_entities(appliances_cfg: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name in appliances_cfg:
        object_id = f"homewhiz_{name}"
        state_topic = f"{topic_prefix}/homewhiz/{name}/state"
        payload = {
            "name": f"Stan: {name}",
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/sensor/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": None,
        })
    return entities


def build_lg_thinq_sensor_entities(devices_cfg: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name in devices_cfg:
        object_id = f"lg_thinq_{name}"
        state_topic = f"{topic_prefix}/lg_thinq/{name}/state"
        payload = {
            "name": f"Stan: {name}",
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/sensor/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": None,
        })
    return entities


def build_iqua_sensor_entities(devices_cfg: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name in devices_cfg:
        object_id = f"iqua_{name}"
        state_topic = f"{topic_prefix}/iqua/{name}/state"
        payload = {
            "name": f"Stan: {name}",
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/sensor/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": None,
        })
    return entities


def build_smartthings_sensor_entities(devices_cfg: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    entities = []
    for name in devices_cfg:
        object_id = f"smartthings_{name}"
        state_topic = f"{topic_prefix}/smartthings/{name}/state"
        payload = {
            "name": f"Stan: {name}",
            "unique_id": f"satel_tuya_bridge_{object_id}",
            "state_topic": state_topic,
            "device": _device_block(ha_cfg),
        }
        entities.append({
            "discovery_topic": f"{prefix}/sensor/satel_tuya_bridge/{object_id}/config",
            "payload": payload,
            "state_topic": state_topic,
            "command_topic": None,
        })
    return entities


def build_satel_partition_entities(partitions: dict, ha_cfg: dict, topic_prefix: str) -> list[dict]:
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
    ha_cfg = config.raw.get("home_assistant", {})
    topic_prefix = ha_cfg.get("state_topic_prefix", "home/bridge")

    entities = []
    entities += build_boiler_sensor_entities(config.ebus.get("sensors", []), ha_cfg, boiler_topic_prefix)
    entities += build_tuya_switch_entities(config.tuya.get("devices", {}), ha_cfg, topic_prefix)
    entities += build_satel_input_entities(config.satel.get("monitored_zones", {}), ha_cfg, topic_prefix)
    entities += build_satel_output_entities(config.satel.get("monitored_outputs", {}), ha_cfg, topic_prefix)
    entities += build_satel_partition_entities(config.satel.get("partitions", {}), ha_cfg, topic_prefix)
    homewhiz_cfg = getattr(config, "homewhiz", None) or {}
    entities += build_homewhiz_sensor_entities(homewhiz_cfg.get("appliances", {}), ha_cfg, topic_prefix)
    lg_thinq_cfg = getattr(config, "lg_thinq", None) or {}
    entities += build_lg_thinq_sensor_entities(lg_thinq_cfg.get("devices", {}), ha_cfg, topic_prefix)
    iqua_cfg = getattr(config, "iqua", None) or {}
    entities += build_iqua_sensor_entities(iqua_cfg.get("devices", {}), ha_cfg, topic_prefix)
    smartthings_cfg = getattr(config, "smartthings", None) or {}
    entities += build_smartthings_sensor_entities(smartthings_cfg.get("devices", {}), ha_cfg, topic_prefix)
    return entities


def wmbus_state_topic(topic_prefix: str, meter_name: str, field: str) -> str:
    return f"{topic_prefix}/wmbus/{meter_name}/{field}/state"


def build_wmbus_field_entity(meter: dict, field: str, ha_cfg: dict, topic_prefix: str) -> dict:
    """Sensor HA dla jednego pola licznika wM-Bus. Pola zależą od sterownika
    wmbusmeters, więc encje powstają dynamicznie po pierwszym telegramie.
    Fazy L1/L2/L3 dostają znacznik w nazwie."""
    prefix = ha_cfg.get("discovery_prefix", "homeassistant")
    object_id = f"wmbus_{meter['name']}_{field}"
    state_topic = wmbus_state_topic(topic_prefix, meter["name"], field)
    phase = detect_phase(field)
    payload = {
        "name": f"{meter.get('label', meter['name'])}: {field.replace('_', ' ')}" + (f" [L{phase}]" if phase else ""),
        "unique_id": f"satel_tuya_bridge_{object_id}",
        "state_topic": state_topic,
        "device": _device_block(ha_cfg),
    }
    unit = field_unit(field)
    if unit:
        payload["unit_of_measurement"] = unit[0]
        if unit[1]:
            payload["device_class"] = unit[1]
        if unit[2]:
            payload["state_class"] = unit[2]
    return {
        "discovery_topic": f"{prefix}/sensor/satel_tuya_bridge/{object_id}/config",
        "payload": payload, "state_topic": state_topic, "command_topic": None,
    }
