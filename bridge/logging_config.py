from __future__ import annotations

import logging

VALID_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

PLUGIN_LOGGERS: dict[str, list[str]] = {
    "satel": ["bridge.satel"],
    # Wtyczki "tuya_zigbee" i "tuya_cloud" zostały scalone do jednej "tuya_local"
    # (jedna strona /plugins/tuya) - obie odrębne wcześniej listy loggerów muszą
    # więc trafiać pod ten sam, jedyny już istniejący plugin_id, inaczej suwak
    # poziomu logowania na stronie Tuya nigdy nie obejmował bridge.tuya_cloud
    # (a przycisk "📜 Logi" na karcie wtyczki nie filtrował po nim wcale) -
    # błędy komend do Tuya Cloud (np. "device pool quota is insufficient")
    # widać było tylko pośrednio, w loggerze wywołującego (np. bridge.flow_engine).
    "tuya_local": ["bridge.tuya_local", "bridge.tuya_cloud"],
    "ebus": ["bridge.ebus"],
    "home_assistant": ["bridge.ha_mqtt"],
    "mqtt_publish": ["bridge.ha_mqtt"],
    "unifi_protect": ["bridge.unifi_protect"],
    "homewhiz": ["bridge.homewhiz"],
    "presence_simulation": ["bridge.presence_simulation"],
    "notifications": ["bridge.notifier"],
    "mcp_server": ["bridge.mcp_server"],
    "homekit": ["bridge.homekit"],
}


def plugin_config_section(config_raw: dict, plugin_id: str) -> dict:
    tuya = config_raw.get("tuya", {}) or {}
    return {
        "satel": config_raw.get("satel", {}) or {},
        "tuya_local": tuya,
        "ebus": config_raw.get("ebus", {}) or {},
        "home_assistant": config_raw.get("home_assistant", {}) or {},
        "mqtt_publish": config_raw.get("mqtt_publish", {}) or {},
        "unifi_protect": config_raw.get("unifi_protect") or {},
        "homewhiz": config_raw.get("homewhiz") or {},
        "presence_simulation": config_raw.get("presence_simulation", {}) or {},
        "notifications": config_raw.get("notifications", {}) or {},
        "mcp_server": config_raw.get("mcp_server", {}) or {},
        "homekit": config_raw.get("homekit", {}) or {},
    }.get(plugin_id, {})


def apply_log_levels(config_raw: dict) -> None:
    default_level = (config_raw.get("logging") or {}).get("default_level", "INFO")
    if default_level not in VALID_LEVELS:
        default_level = "INFO"
    logging.getLogger("bridge").setLevel(default_level)

    for plugin_id, logger_names in PLUGIN_LOGGERS.items():
        level = plugin_config_section(config_raw, plugin_id).get("log_level") or default_level
        if level not in VALID_LEVELS:
            level = default_level
        for name in logger_names:
            logging.getLogger(name).setLevel(level)
