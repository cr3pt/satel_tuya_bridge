from __future__ import annotations

import asyncio

from quart import Quart, jsonify, render_template, request

from bridge.ebus_client import discover_topics as discover_ebus_topics, BackgroundTopicDiscovery
from dashboard.setup import (
    clean_sensors_payload,
    ebus_connection_to_form, build_ebus_connection_patch, validate_ebus_connection,
)
from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="ebus",
    name="eBUS / kocioł (dane grzewcze)",
    icon="🔥",
    description="Lista czujników kotła (ebusd) oraz wykrywanie tematów MQTT.",
    config_url="/plugins/ebus",
)

_background_discovery = BackgroundTopicDiscovery()


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    @app.route("/plugins/ebus")
    async def ebus_plugin_page():
        return await render_template("ebus/index.html")

    @app.route("/api/plugins/ebus/sensors", methods=["GET"])
    async def api_plugin_ebus_sensors_get():
        return jsonify(get_config_store().get("sensors").get("sensors", []))

    @app.route("/api/plugins/ebus/sensors", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_ebus_sensors_post():
        payload = await request.get_json(silent=True)
        cleaned, error = clean_sensors_payload(payload)
        if error:
            return jsonify({"error": error}), 400

        get_config_store().set("sensors", {"sensors": cleaned})
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(cleaned),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/ebus/discover", methods=["POST"])
    async def api_plugin_ebus_discover():
        payload = await request.get_json(silent=True) or {}
        ebus_cfg = get_config_raw().get("ebus", {})
        mqtt_host = payload.get("mqtt_host") or ebus_cfg.get("mqtt_host")
        mqtt_port = payload.get("mqtt_port") or ebus_cfg.get("mqtt_port", 1883)
        if not mqtt_host:
            return jsonify({"error": "Brak adresu brokera MQTT"}), 400
        try:
            topics = await asyncio.get_event_loop().run_in_executor(
                None, discover_ebus_topics, mqtt_host, int(mqtt_port),
            )
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd wykrywania: {exc}"}), 502
        return jsonify({"topics": topics})

    @app.route("/api/plugins/ebus/discover/start", methods=["POST"])
    async def api_plugin_ebus_discover_start():
        payload = await request.get_json(silent=True) or {}
        ebus_cfg = get_config_raw().get("ebus", {})
        mqtt_host = payload.get("mqtt_host") or ebus_cfg.get("mqtt_host")
        mqtt_port = payload.get("mqtt_port") or ebus_cfg.get("mqtt_port", 1883)
        if not mqtt_host:
            return jsonify({"error": "Brak adresu brokera MQTT"}), 400
        try:
            duration_seconds = min(float(payload.get("duration_seconds") or 300), 600)
        except (TypeError, ValueError):
            duration_seconds = 300.0

        try:
            _background_discovery.start(mqtt_host, int(mqtt_port), duration_seconds=duration_seconds)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd uruchamiania nasłuchu: {exc}"}), 502

        return jsonify(_background_discovery.status())

    @app.route("/api/plugins/ebus/discover/status")
    async def api_plugin_ebus_discover_status():
        return jsonify(_background_discovery.status())

    @app.route("/api/plugins/ebus/discover/stop", methods=["POST"])
    async def api_plugin_ebus_discover_stop():
        return jsonify(_background_discovery.stop())

    @app.route("/api/plugins/ebus/connection", methods=["GET"])
    async def api_plugin_ebus_connection_get():
        current = get_config_store().get("config")
        return jsonify(ebus_connection_to_form(current.get("ebus", {}), current.get("boiler_output", {})))

    @app.route("/api/plugins/ebus/connection", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_ebus_connection_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_ebus_connection(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        current = get_config_store().get("config")
        new_config = build_ebus_connection_patch(form, current)
        get_config_store().set("config", new_config)

        return jsonify({
            "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a - "
                       "użyj przycisku restartu w zakładce ⚙ Ustawienia -> Ogólne.",
        })

    def _get_ebus_section() -> dict:
        return get_config_store().get("config").get("ebus", {})

    def _save_ebus_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["ebus"] = section
        get_config_store().set("config", current)

    register_settings_route(
        app, "ebus", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_ebus_section, save_section=_save_ebus_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
