from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="notifications",
    name="Powiadomienia (ntfy)",
    icon="🔔",
    description="Powiadomienia push o alarmach/zdarzeniach przez ntfy.sh (albo własny serwer ntfy).",
    config_url="/plugins/notifications",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_notifications_section() -> dict:
        return get_config_store().get("config").get("notifications", {}) or {}

    def _save_notifications_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["notifications"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/notifications")
    def notifications_plugin_page():
        return render_template("notifications/index.html")

    @app.route("/api/plugins/notifications/topic", methods=["GET"])
    def api_plugin_notifications_topic_get():
        section = _get_notifications_section()
        return jsonify({
            "ntfy_topic_url": section.get("ntfy_topic_url", ""),
            "grace_seconds": section.get("grace_seconds", 120),
        })

    @app.route("/api/plugins/notifications/topic", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    def api_plugin_notifications_topic_post():
        form = request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        existing = _get_notifications_section()
        try:
            grace_seconds = int(form.get("grace_seconds") or existing.get("grace_seconds") or 120)
        except (TypeError, ValueError):
            return jsonify({"error": "Okres ciszy (grace_seconds) musi być liczbą"}), 400
        if grace_seconds < 0:
            return jsonify({"error": "Okres ciszy (grace_seconds) nie może być ujemny"}), 400

        section = dict(existing)
        section.update({
            "ntfy_topic_url": (form.get("ntfy_topic_url") or "").strip(),
            "grace_seconds": grace_seconds,
        })
        _save_notifications_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu."})

    register_settings_route(
        app, "notifications", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_notifications_section, save_section=_save_notifications_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
