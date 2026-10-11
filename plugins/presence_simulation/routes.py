# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

from quart import Quart, jsonify, render_template

from plugins import PluginMeta, register_settings_route

def describe_state(config_raw: dict) -> dict:
    presence = config_raw.get("presence_simulation", {}) or {}
    devices = presence.get("devices") or []
    return {
        "enabled": bool(presence.get("enabled")),
        "configured": bool(devices),
        "detail": f"{len(devices)} urządzeń" if devices else "brak urządzeń",
    }


META = PluginMeta(
    id="presence_simulation",
    name="Symulacja obecności",
    icon="🎭",
    description="Losowe włączanie/wyłączanie wskazanych urządzeń Tuya pod nieobecność domowników.",
    config_url="/plugins/presence_simulation",
    restart_required=False,
    toggle_route="automations",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_presence_section() -> dict:
        return get_config_store().get("automations").get("presence_simulation", {}) or {}

    def _save_presence_section(section: dict) -> None:
        current = get_config_store().get("automations")
        current["presence_simulation"] = section
        get_config_store().set("automations", current)

    @app.route("/plugins/presence_simulation")
    async def presence_simulation_plugin_page():
        return await render_template("presence_simulation/index.html")

    @app.route("/api/plugins/presence_simulation/summary", methods=["GET"])
    async def api_plugin_presence_simulation_summary_get():
        section = _get_presence_section()
        return jsonify({"devices": section.get("devices", [])})

    register_settings_route(
        app, "presence_simulation", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_presence_section, save_section=_save_presence_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
