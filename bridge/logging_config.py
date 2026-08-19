"""Poziom logowania - globalny domyślny plus osobny per wtyczka.

Każdy moduł integracji ma już własny, stabilny logger (np.
`bridge.satel_client.py: logger = logging.getLogger("bridge.satel")`) - ten
moduł tylko mapuje id wtyczki (patrz PLUGIN_DEFS w dashboard/setup.py) na
nazwy tych loggerów i aplikuje poziom wczytany z konfiguracji. Wywoływane
przy starcie (main.py) i przy każdym reload_config() (bridge/rules_engine.py),
żeby zmiana poziomu z panelu zadziałała na żywo, bez restartu procesu.
"""
from __future__ import annotations

import logging

VALID_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Kilka wtyczek dzieli jeden logger modułu (Tuya lokalnie + Zigbee to ten
# sam bridge/tuya_local.py; Home Assistant + publikacja MQTT to ta sama
# klasa HomeAssistantMqttBridge w bridge/ha_mqtt.py, patrz rules_engine.py).
PLUGIN_LOGGERS: dict[str, list[str]] = {
    "satel": ["bridge.satel"],
    "tuya_local": ["bridge.tuya_local"],
    "tuya_zigbee": ["bridge.tuya_local"],
    "tuya_cloud": ["bridge.tuya_cloud"],
    "ebus": ["bridge.ebus"],
    "home_assistant": ["bridge.ha_mqtt"],
    "mqtt_publish": ["bridge.ha_mqtt"],
    "unifi_protect": ["bridge.unifi_protect"],
    "presence_simulation": ["bridge.presence_simulation"],
    "notifications": ["bridge.notifier"],
}


def _plugin_config_section(config_raw: dict, plugin_id: str) -> dict:
    """Sekcja configu, w której dana wtyczka trzyma swoje pole `log_level`
    - patrz też PLUGIN_DEFS/list_plugins w dashboard/setup.py po ten sam
    podział na sekcje."""
    tuya = config_raw.get("tuya", {}) or {}
    return {
        "satel": config_raw.get("satel", {}) or {},
        "tuya_local": tuya,
        "tuya_zigbee": tuya,
        "tuya_cloud": tuya.get("cloud", {}) or {},
        "ebus": config_raw.get("ebus", {}) or {},
        "home_assistant": config_raw.get("home_assistant", {}) or {},
        "mqtt_publish": config_raw.get("mqtt_publish", {}) or {},
        "unifi_protect": config_raw.get("unifi_protect") or {},
        "presence_simulation": config_raw.get("presence_simulation", {}) or {},
        "notifications": config_raw.get("notifications", {}) or {},
    }.get(plugin_id, {})


def apply_log_levels(config_raw: dict) -> None:
    """Ustawia poziom logowania: globalny domyślny (`logging.default_level`,
    domyślnie INFO) na loggerze-rodzicu "bridge" (obejmuje rdzeń - silnik
    reguł, odporność na awarie, itd. - i każdą wtyczkę BEZ własnego
    ustawienia), a potem nadpisuje go per wtyczka, jeśli ma ustawione własne
    `log_level`."""
    default_level = (config_raw.get("logging") or {}).get("default_level", "INFO")
    if default_level not in VALID_LEVELS:
        default_level = "INFO"
    logging.getLogger("bridge").setLevel(default_level)

    for plugin_id, logger_names in PLUGIN_LOGGERS.items():
        level = _plugin_config_section(config_raw, plugin_id).get("log_level") or default_level
        if level not in VALID_LEVELS:
            level = default_level
        for name in logger_names:
            logging.getLogger(name).setLevel(level)
