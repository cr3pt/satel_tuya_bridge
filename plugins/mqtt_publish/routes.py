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


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

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

    register_settings_route(
        app, "mqtt_publish", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_mqtt_publish_section, save_section=_save_mqtt_publish_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
