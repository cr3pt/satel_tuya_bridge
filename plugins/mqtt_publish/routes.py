from __future__ import annotations

from quart import Quart, jsonify, render_template, request

from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="mqtt_publish",
    name="Publikacja stanu MQTT",
    icon="📡",
    description="Generyczna publikacja stanu Satela i Tuya po MQTT (osobno od Home Assistant Discovery).",
    config_url="/plugins/mqtt_publish",
)


def _known_prefixes(config_raw: dict) -> list[str]:
    prefixes = set()
    ha_cfg = config_raw.get("home_assistant", {}) or {}
    prefixes.add(ha_cfg.get("discovery_prefix", "homeassistant"))
    prefixes.add(ha_cfg.get("state_topic_prefix", "home/bridge"))

    topics = (config_raw.get("mqtt_publish", {}) or {}).get("topics", {}) or {}
    prefixes.add(topics.get("satel", "satel"))
    prefixes.add(topics.get("tuya", "tuya"))
    prefixes.add(topics.get("homewhiz", "homewhiz"))

    boiler_mqtt = (config_raw.get("boiler_output", {}) or {}).get("mqtt", {}) or {}
    prefixes.add(boiler_mqtt.get("topic_prefix", "home/boiler"))

    return sorted(prefixes)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, get_history, **_ignored) -> PluginMeta:

    def _get_mqtt_publish_section() -> dict:
        return get_config_store().get("config").get("mqtt_publish", {}) or {}

    def _save_mqtt_publish_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["mqtt_publish"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/mqtt_publish")
    async def mqtt_publish_plugin_page():
        return await render_template("mqtt_publish/index.html")

    @app.route("/api/plugins/mqtt_publish/topics", methods=["GET"])
    async def api_plugin_mqtt_publish_topics_get():
        section = _get_mqtt_publish_section()
        topics = section.get("topics", {}) or {}
        return jsonify({
            "satel_topic": topics.get("satel", "satel"),
            "tuya_topic": topics.get("tuya", "tuya"),
            "homewhiz_topic": topics.get("homewhiz", "homewhiz"),
        })

    @app.route("/api/plugins/mqtt_publish/topics", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_mqtt_publish_topics_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        existing = _get_mqtt_publish_section()
        section = dict(existing)
        section["topics"] = {
            "satel": (form.get("satel_topic") or "").strip() or "satel",
            "tuya": (form.get("tuya_topic") or "").strip() or "tuya",
            "homewhiz": (form.get("homewhiz_topic") or "").strip() or "homewhiz",
        }
        _save_mqtt_publish_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu."})

    @app.route("/api/plugins/mqtt_publish/explorer", methods=["GET"])
    async def api_plugin_mqtt_publish_explorer_get():
        entries = get_history().get_status("mqtt_topic")
        topics = [{"topic": e["label"], "payload": e["value"], "ts": e["ts"]} for e in entries]
        return jsonify({"topics": topics, "known_prefixes": _known_prefixes(get_config_raw())})

    @app.route("/api/plugins/mqtt_publish/publish", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_mqtt_publish_publish_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        topic = (form.get("topic") or "").strip()
        if not topic:
            return jsonify({"error": "Temat jest wymagany"}), 400

        prefixes = _known_prefixes(get_config_raw())
        if not any(topic == prefix or topic.startswith(prefix + "/") for prefix in prefixes):
            return jsonify({
                "error": f"Temat musi zaczynać się od jednego z własnych prefixów bridge'a: {', '.join(prefixes)}",
            }), 400

        payload = form.get("payload", "")
        retain = bool(form.get("retain", False))
        command_id = get_history().enqueue_command("mqtt_publish", {"topic": topic, "payload": payload, "retain": retain})
        return jsonify({"command_id": command_id, "message": f"Zlecono publikację na '{topic}'"})

    register_settings_route(
        app, "mqtt_publish", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_mqtt_publish_section, save_section=_save_mqtt_publish_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
