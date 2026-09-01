from __future__ import annotations

from flask import Flask, jsonify, render_template

from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="presence_simulation",
    name="Symulacja obecności",
    icon="🎭",
    description="Losowe załączanie wybranych urządzeń Tuya, gdy nikogo nie ma w domu.",
    config_url="/plugins/presence_simulation",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_presence_section() -> dict:
        return get_config_store().get("automations").get("presence_simulation", {}) or {}

    def _save_presence_section(section: dict) -> None:
        current = get_config_store().get("automations")
        current["presence_simulation"] = section
        get_config_store().set("automations", current)

    @app.route("/plugins/presence_simulation")
    def presence_simulation_plugin_page():
        return render_template("presence_simulation/index.html")

    @app.route("/api/plugins/presence_simulation/summary", methods=["GET"])
    def api_plugin_presence_simulation_summary_get():
        section = _get_presence_section()
        return jsonify({"devices": section.get("devices", [])})

    register_settings_route(
        app, "presence_simulation", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_presence_section, save_section=_save_presence_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
