# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

from quart import Quart, jsonify, render_template, request

from bridge import notifier as notifier_mod

from plugins import PluginMeta, register_settings_route

def describe_state(config_raw: dict) -> dict:
    notifications = config_raw.get("notifications", {}) or {}
    channels = notifier_mod.Notifier(notifications).channels
    return {
        "enabled": notifications.get("enabled", True) and bool(channels),
        "configured": bool(channels),
        "detail": "kanały: " + ", ".join(channels) if channels else "nieskonfigurowane",
    }


META = PluginMeta(
    id="notifications",
    name="Powiadomienia (ntfy, e-mail, Pushover, Bark, Pushcut)",
    icon="🔔",
    description="Powiadomienia (ntfy, e-mail, Pushover, Bark, Pushcut - iPhone/Apple) przy alarmie "
                "Satel, utracie połączenia z centralą/MQTT/Tuya/kamerami oraz przy alarmach progowych.",
    config_url="/plugins/notifications",
    config_key="notifications",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_notifications_section() -> dict:
        return get_config_store().get("config").get("notifications", {}) or {}

    def _save_notifications_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["notifications"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/notifications")
    async def notifications_plugin_page():
        return await render_template("notifications/index.html")

    @app.route("/api/plugins/notifications/topic", methods=["GET"])
    async def api_plugin_notifications_topic_get():
        section = _get_notifications_section()
        return jsonify({
            "ntfy_topic_url": section.get("ntfy_topic_url", ""),
            "grace_seconds": section.get("grace_seconds", 120),
        })

    @app.route("/api/plugins/notifications/topic", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_notifications_topic_post():
        form = await request.get_json(silent=True)
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

    @app.route("/api/plugins/notifications/channels", methods=["GET"])
    async def api_plugin_notifications_channels_get():
        return jsonify(notifier_mod.public_view(_get_notifications_section()))

    @app.route("/api/plugins/notifications/channels", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_notifications_channels_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        existing = _get_notifications_section()
        clean, errors = notifier_mod.validate_channels(form, existing)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400
        section = dict(existing)
        section.update(clean)
        if "satel_alarm" in form:
            section["satel_alarm"] = bool(form["satel_alarm"])
        _save_notifications_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()
        return jsonify({"message": "Zapisano. Bridge przeładuje konfigurację w ciągu ok. 1-2s.",
                        "channels": notifier_mod.Notifier(section).channels})

    @app.route("/api/plugins/notifications/test", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_notifications_test():
        body = await request.get_json(silent=True) or {}
        priority = body.get("priority") if body.get("priority") in notifier_mod.PRIORITIES else "default"
        notifier = notifier_mod.Notifier(_get_notifications_section())
        if not notifier.enabled:
            return jsonify({"error": "Żaden kanał powiadomień nie jest skonfigurowany"}), 400
        results = await notifier.send_detailed(
            "🔔 Test powiadomień", "Testowe powiadomienie z panelu Satel-Tuya-eBUS.", priority=priority,
        )
        return jsonify({"results": results, "ok": all(v is None for v in results.values())})

    register_settings_route(
        app, "notifications", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_notifications_section, save_section=_save_notifications_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
