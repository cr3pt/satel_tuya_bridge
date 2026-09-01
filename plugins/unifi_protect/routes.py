from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="unifi_protect",
    name="Kamery UniFi Protect",
    icon="📹",
    description="Zrzuty z kamer UniFi Protect przy alarmie/zdarzeniach.",
    config_url="/plugins/unifi_protect",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_unifi_section() -> dict:
        return get_config_store().get("config").get("unifi_protect") or {}

    def _save_unifi_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["unifi_protect"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/unifi_protect")
    def unifi_protect_plugin_page():
        return render_template("unifi_protect/index.html")

    @app.route("/api/plugins/unifi_protect/credentials", methods=["GET"])
    def api_plugin_unifi_protect_credentials_get():
        section = _get_unifi_section()
        return jsonify({
            "configured": bool(section),
            "host": section.get("host", ""),
            "username": section.get("username", ""),
            "password_set": bool(section.get("password")),
            "verify_ssl": bool(section.get("verify_ssl", False)),
        })

    @app.route("/api/plugins/unifi_protect/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    def api_plugin_unifi_protect_credentials_post():
        form = request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        host = (form.get("host") or "").strip()
        if not host:
            return jsonify({"error": "Adres kontrolera UniFi Protect jest wymagany"}), 400

        existing = _get_unifi_section()
        section = dict(existing)
        section.update({
            "host": host,
            "username": (form.get("username") or "").strip() or existing.get("username", ""),
            "password": (form.get("password") or "").strip() or existing.get("password", ""),
            "verify_ssl": bool(form.get("verify_ssl")),
        })
        _save_unifi_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Włączenie/wyłączenie integracji wciąż wymaga pełnego restartu procesu bridge'a."})

    def _validate_enable() -> str | None:
        if not _get_unifi_section().get("host"):
            return "Najpierw zapisz adres kontrolera UniFi Protect niżej, zanim włączysz wtyczkę."
        return None

    register_settings_route(
        app, "unifi_protect", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_unifi_section, save_section=_save_unifi_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        validate_enable=_validate_enable,
    )

    return META
