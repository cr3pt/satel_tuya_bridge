from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from quart import Quart, jsonify, request

from bridge.logging_config import VALID_LEVELS


@dataclass(frozen=True)
class PluginMeta:
    id: str
    name: str
    icon: str
    description: str
    config_url: str | None = None


def register_settings_route(
    app: Quart, plugin_id: str, *, require_auth: Callable, get_auth_guard: Callable,
    get_section: Callable[[], dict], save_section: Callable[[dict], None],
    reload_bridge_config: Callable, enqueue_bridge_reload: Callable,
    supports_enabled: bool = True,
    default_enabled: bool = True,
    validate_enable: Callable[[], str | None] | None = None,
) -> None:

    @app.route(f"/api/plugins/{plugin_id}/settings", methods=["GET"],
               endpoint=f"plugin_{plugin_id}_settings_get")
    async def _settings_get():
        section = get_section() or {}
        result = {"log_level": section.get("log_level") or ""}
        if supports_enabled:
            result["enabled"] = bool(section.get("enabled", default_enabled))
        return jsonify(result)

    @app.route(f"/api/plugins/{plugin_id}/settings", methods=["POST"],
               endpoint=f"plugin_{plugin_id}_settings_post")
    @require_auth(get_guard=get_auth_guard)
    async def _settings_post():
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        section = dict(get_section() or {})
        enabled_changed = False
        if supports_enabled and "enabled" in body:
            new_enabled = bool(body.get("enabled"))
            if new_enabled and validate_enable is not None:
                error = validate_enable()
                if error:
                    return jsonify({"error": error}), 400
            enabled_changed = bool(section.get("enabled", default_enabled)) != new_enabled
            section["enabled"] = new_enabled
        if "log_level" in body:
            level = (body.get("log_level") or "").strip().upper()
            if level and level not in VALID_LEVELS:
                return jsonify({"error": f"Nieprawidłowy poziom logowania: {level}"}), 400
            if level:
                section["log_level"] = level
            else:
                section.pop("log_level", None)

        save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        message = "Zapisano."
        if enabled_changed:
            message += " Włączenie/wyłączenie wymaga pełnego restartu procesu bridge'a."
        else:
            message += " Zmiany zadziałają w ciągu ok. 1-2s, bez restartu."
        return jsonify({"message": message})


def register_all(app: Quart, **deps) -> list[PluginMeta]:
    from plugins.satel import routes as satel_routes
    from plugins.tuya_local import routes as tuya_routes
    from plugins.ebus import routes as ebus_routes
    from plugins.tuya_cloud import routes as tuya_cloud_routes
    from plugins.home_assistant import routes as home_assistant_routes
    from plugins.mqtt_publish import routes as mqtt_publish_routes
    from plugins.unifi_protect import routes as unifi_protect_routes
    from plugins.homewhiz import routes as homewhiz_routes
    from plugins.notifications import routes as notifications_routes
    from plugins.presence_simulation import routes as presence_simulation_routes
    from plugins.mcp_server import routes as mcp_server_routes
    from plugins.homekit import routes as homekit_routes

    metas = []
    for routes_module in (
        satel_routes, tuya_routes, ebus_routes, tuya_cloud_routes, home_assistant_routes,
        mqtt_publish_routes, unifi_protect_routes, homewhiz_routes, notifications_routes,
        presence_simulation_routes, mcp_server_routes, homekit_routes,
    ):
        metas.append(routes_module.register(app, **deps))
    return metas
