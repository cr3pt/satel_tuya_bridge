# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

from quart import Quart, jsonify, render_template, request

from plugins import PluginMeta, register_settings_route

def describe_state(config_raw: dict) -> dict:
    ha = config_raw.get("home_assistant", {}) or {}
    ebus = config_raw.get("ebus", {}) or {}
    return {
        "enabled": bool(ha.get("enabled")),
        "configured": bool(ha.get("mqtt_host") or ebus.get("mqtt_host")),
        "detail": ha.get("mqtt_host") or ebus.get("mqtt_host") or "brak brokera MQTT",
    }


META = PluginMeta(
    id="home_assistant",
    name="Home Assistant (MQTT Discovery)",
    icon="🏠",
    description="Automatyczne udostępnienie encji (wejścia/wyjścia Satela, urządzenia Tuya, czujniki "
                "kotła) w Home Assistant.",
    config_url="/plugins/home_assistant",
    toggle_route="connections_restart",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_ha_section() -> dict:
        return get_config_store().get("config").get("home_assistant", {}) or {}

    def _save_ha_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["home_assistant"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/home_assistant")
    async def home_assistant_plugin_page():
        return await render_template("home_assistant/index.html")

    @app.route("/api/plugins/home_assistant/broker", methods=["GET"])
    async def api_plugin_home_assistant_broker_get():
        ha = _get_ha_section()
        ebus_mqtt_host = get_config_raw().get("ebus", {}).get("mqtt_host", "")
        return jsonify({
            "mqtt_host": ha.get("mqtt_host", ""),
            "mqtt_port": ha.get("mqtt_port", 1883),
            "discovery_prefix": ha.get("discovery_prefix", "homeassistant"),
            "state_topic_prefix": ha.get("state_topic_prefix", "home/bridge"),
            "device_name": ha.get("device_name", "Satel-Tuya-eBUS Bridge"),
            "ebus_mqtt_host": ebus_mqtt_host,
        })

    @app.route("/api/plugins/home_assistant/broker", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_home_assistant_broker_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        ebus_mqtt_host = get_config_raw().get("ebus", {}).get("mqtt_host", "")
        mqtt_host = (form.get("mqtt_host") or "").strip() or ebus_mqtt_host
        if not mqtt_host:
            return jsonify({"error": "Adres brokera MQTT jest wymagany (albo skonfiguruj broker eBUS)"}), 400

        existing = _get_ha_section()
        section = dict(existing)
        section.update({
            "mqtt_host": mqtt_host,
            "mqtt_port": int(form.get("mqtt_port") or existing.get("mqtt_port") or 1883),
            "discovery_prefix": (form.get("discovery_prefix") or "").strip() or existing.get("discovery_prefix", "homeassistant"),
            "state_topic_prefix": (form.get("state_topic_prefix") or "").strip() or existing.get("state_topic_prefix", "home/bridge"),
            "device_name": (form.get("device_name") or "").strip() or existing.get("device_name", "Satel-Tuya-eBUS Bridge"),
        })
        _save_ha_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Włączenie/wyłączenie integracji wciąż wymaga pełnego restartu procesu bridge'a."})

    register_settings_route(
        app, "home_assistant", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_ha_section, save_section=_save_ha_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
