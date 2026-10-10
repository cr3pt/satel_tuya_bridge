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
    "wmbus": ["bridge.wmbus"],
    "home_assistant": ["bridge.ha_mqtt"],
    "mqtt_publish": ["bridge.ha_mqtt"],
    "unifi_protect": ["bridge.unifi_protect"],
    "homewhiz": ["bridge.homewhiz"],
    "lg_thinq": ["bridge.lg_thinq"],
    "iqua": ["bridge.iqua"],
    "smartthings": ["bridge.smartthings"],
    "presence_simulation": ["bridge.presence_simulation"],
    "notifications": ["bridge.notifier"],
    "mcp_server": ["bridge.mcp_server"],
    "homekit": ["bridge.homekit"],
    # Flow (/flows) nie jest wtyczką z własną stroną w 🧩 Wtyczki (nie da się go
    # wyłączyć), ale poziom logowania i tak trzeba dać się ustawić - stąd osobny
    # wpis tutaj + mały panel "Poziom logowania" bezpośrednio na stronie /flows,
    # zamiast pełnej karty wtyczki. Bez tego bridge.flow_engine w ogóle nie
    # pojawiał się na liście loggerów w Historii logów (lista jest budowana z
    # loggerów, które faktycznie coś zalogowały - a silnik Flow logował dotąd
    # WYŁĄCZNIE błędy, nigdy zwykły przebieg węzłów).
    "flow_engine": ["bridge.flow_engine"],
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
        "lg_thinq": config_raw.get("lg_thinq") or {},
        "iqua": config_raw.get("iqua") or {},
        "smartthings": config_raw.get("smartthings") or {},
        "presence_simulation": config_raw.get("presence_simulation", {}) or {},
        "notifications": config_raw.get("notifications", {}) or {},
        "mcp_server": config_raw.get("mcp_server", {}) or {},
        "homekit": config_raw.get("homekit", {}) or {},
        "flow_engine": config_raw.get("flow_engine", {}) or {},
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
