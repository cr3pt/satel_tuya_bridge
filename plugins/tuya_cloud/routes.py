"""Wtyczka Tuya Cloud (poświadczenia API + import urządzeń/kamer) - własna
strona konfiguracji, wydzielona ze wspólnej zakładki 🔌 Połączenia (patrz
zakładka 🧩 Wtyczki -> "⚙ Konfiguruj"). Skan LAN i import urządzeń zostają w
plugins/tuya_local/routes.py (to tam trafiają zaimportowane urządzenia) -
tu tylko poświadczenia API + włącznik/poziom logowania. Patrz
plugins/satel/routes.py po ten sam wzorzec wstrzykiwania zależności."""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="tuya_cloud",
    name="Tuya Cloud (API)",
    icon="☁",
    description="Poświadczenia Tuya Cloud API - potrzebne do importu urządzeń i kamer.",
    config_url="/plugins/tuya_cloud",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:
    """Rejestruje routing wtyczki Tuya Cloud. `_ignored` połyka zależności
    przeznaczone dla INNYCH wtyczek - register_all() w plugins/__init__.py
    woła każdą wtyczkę z tym samym, wspólnym zestawem `deps`."""

    def _get_cloud_section() -> dict:
        return get_config_store().get("config").get("tuya", {}).get("cloud", {}) or {}

    def _save_cloud_section(section: dict) -> None:
        current = get_config_store().get("config")
        current.setdefault("tuya", {})["cloud"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/tuya_cloud")
    def tuya_cloud_plugin_page():
        return render_template("tuya_cloud/index.html")

    @app.route("/api/plugins/tuya_cloud/credentials", methods=["GET"])
    def api_plugin_tuya_cloud_credentials_get():
        cloud = _get_cloud_section()
        return jsonify({
            "access_id": cloud.get("access_id", ""),
            "access_key_set": bool(cloud.get("access_key")),
            "api_endpoint": cloud.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": cloud.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
        })

    @app.route("/api/plugins/tuya_cloud/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    def api_plugin_tuya_cloud_credentials_post():
        form = request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        access_id = (form.get("access_id") or "").strip()
        if not access_id:
            return jsonify({"error": "Access ID jest wymagany"}), 400

        existing = _get_cloud_section()
        # Pole-sekret (Access Secret): pusta wartość w formularzu = ZACHOWAJ
        # dotychczasową (przeglądarka nigdy nie dostaje prawdziwego sekretu
        # z powrotem - patrz GET wyżej, tylko flaga access_key_set).
        section = dict(existing)
        section.update({
            "access_id": access_id,
            "access_key": (form.get("access_key") or "").strip() or existing.get("access_key", ""),
            "api_endpoint": (form.get("api_endpoint") or "").strip() or existing.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": (form.get("mq_endpoint") or "").strip() or existing.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
        })
        _save_cloud_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu."})

    register_settings_route(
        app, "tuya_cloud", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_cloud_section, save_section=_save_cloud_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
