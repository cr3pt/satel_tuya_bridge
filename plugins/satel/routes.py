from __future__ import annotations

from quart import Quart, jsonify, render_template, request

from bridge.satel_client import BackgroundZoneOutputScan
from dashboard.setup import (
    build_satel_fields_dict, validate_satel_fields,
    dict_to_name_number_rows, zones_to_rows, virtual_inputs_to_rows,
    satel_connection_to_form, build_satel_connection_patch, validate_satel_connection,
)
from plugins import PluginMeta, register_settings_route


def describe_state(config_raw: dict) -> dict:
    satel = config_raw.get("satel", {}) or {}
    return {
        "enabled": satel.get("enabled", True) and bool(satel.get("host")),
        "configured": bool(satel.get("host")),
        "detail": (
            f"{satel.get('host', '?')}:{satel.get('port', '?')}"
            + ("" if satel.get("integration_key") else " (tylko odczyt - brak klucza integracji)")
        ) if satel.get("host") else "nieskonfigurowana",
        "poll_interval_seconds": satel.get("status_poll_interval_seconds", 3),
    }


META = PluginMeta(
    id="satel",
    name="Centrala Satel Integra",
    icon="🔒",
    description="Odczyt wejść/wyjść/stref i sterowanie (uzbrajanie, wyjścia).",
    config_url="/plugins/satel",
    config_key="satel",
    has_poll_interval=True,
    min_poll_interval_seconds=2,
    toggle_route="toggle_prereq_connections_live",
    describe_state=describe_state,
)

_background_scan = BackgroundZoneOutputScan()


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             get_config_path, reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    @app.route("/plugins/satel")
    async def satel_plugin_page():
        return await render_template("satel/index.html")

    @app.route("/api/plugins/satel/fields", methods=["GET"])
    async def api_plugin_satel_fields_get():
        satel = get_config_raw().get("satel", {})
        return jsonify({
            "partitions": dict_to_name_number_rows(satel.get("partitions", {})),
            "monitored_zones": zones_to_rows(satel.get("monitored_zones", {}), satel.get("zone_types", {})),
            "monitored_outputs": zones_to_rows(satel.get("monitored_outputs", {}), satel.get("output_types", {})),
            "virtual_inputs": virtual_inputs_to_rows(satel.get("virtual_inputs", {}).get("inputs", {})),
            "virtual_inputs_mode": satel.get("virtual_inputs", {}).get("mode", "library"),
            "zone_types": satel.get("zone_types", {}),
            "output_types": satel.get("output_types", {}),
        })

    @app.route("/api/plugins/satel/fields", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_satel_fields_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_satel_fields(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        satel_fields_dict = build_satel_fields_dict(form)
        get_config_store().set("satel_fields", satel_fields_dict)

        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": {k: len(v) if isinstance(v, dict) else v for k, v in satel_fields_dict.items()},
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/satel/scan/start", methods=["POST"])
    async def api_plugin_satel_scan_start():
        payload = await request.get_json(silent=True) or {}
        satel_cfg = get_config_raw().get("satel", {})
        host = (payload.get("host") or satel_cfg.get("host") or "").strip()
        if not host:
            return jsonify({"error": "Brak adresu IP centrali"}), 400
        try:
            port = int(payload.get("port") or satel_cfg.get("port") or 7094)
        except (TypeError, ValueError):
            return jsonify({"error": "Nieprawidłowy port"}), 400
        try:
            timeout_seconds = min(float(payload.get("timeout_seconds") or 30), 120)
        except (TypeError, ValueError):
            timeout_seconds = 30.0
        encryption_enabled = (
            payload["encryption_enabled"] if "encryption_enabled" in payload
            else satel_cfg.get("encryption_enabled")
        )
        encryption_key = (payload.get("encryption_key") or satel_cfg.get("encryption_key") or None) if encryption_enabled else None

        try:
            _background_scan.start(host, port, timeout_seconds=timeout_seconds, encryption_key=encryption_key)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

        return jsonify(_background_scan.status())

    @app.route("/api/plugins/satel/scan/status")
    async def api_plugin_satel_scan_status():
        return jsonify(_background_scan.status())

    def _get_satel_section() -> dict:
        return get_config_store().get("config").get("satel", {})

    def _save_satel_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["satel"] = section
        get_config_store().set("config", current)

    @app.route("/api/plugins/satel/connection", methods=["GET"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_satel_connection_get():
        return jsonify(satel_connection_to_form(_get_satel_section()))

    @app.route("/api/plugins/satel/connection", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_satel_connection_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_satel_connection(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        new_section = build_satel_connection_patch(form, _get_satel_section())
        _save_satel_section(new_section)

        return jsonify({
            "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a - "
                       "użyj przycisku restartu w zakładce ⚙ Ustawienia -> Ogólne.",
        })

    register_settings_route(
        app, "satel", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_satel_section, save_section=_save_satel_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
