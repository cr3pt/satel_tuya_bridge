# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

from quart import Quart, jsonify, render_template, request

from bridge.iqua_client import IquaError, test_connection
from plugins import PluginMeta, register_settings_route


def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("iqua") or {}
    return {
        "enabled": cfg.get("enabled", True) and bool(cfg.get("username") and cfg.get("password") and cfg.get("devices")),
        "configured": bool(cfg.get("username") and cfg.get("password") and cfg.get("devices")),
        "detail": (
            f"{len(cfg.get('devices', {}))} urządzeń" if cfg.get("devices")
            else "brak loginu/urządzeń"
        ),
    }


META = PluginMeta(
    id="iqua",
    name="Zmiękczacze wody iQua",
    icon="💧",
    description="Stan zmiękczaczy wody Ecowater/Viessmann Aquahome (aplikacja iQua) - poziom soli, "
                "zużycie wody - dostępny jako warunek 'stan' w automatyzacjach i jako trigger Flow.",
    config_url="/plugins/iqua",
    config_key="iqua",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_section() -> dict:
        return get_config_store().get("config").get("iqua") or {}

    def _save_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["iqua"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/iqua")
    async def iqua_plugin_page():
        return await render_template("iqua/index.html")

    @app.route("/api/plugins/iqua/credentials", methods=["GET"])
    async def api_plugin_iqua_credentials_get():
        section = _get_section()
        return jsonify({
            "username": section.get("username", ""),
            "password_set": bool(section.get("password")),
        })

    @app.route("/api/plugins/iqua/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_iqua_credentials_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        username = (form.get("username") or "").strip()
        if not username:
            return jsonify({"error": "Login (e-mail) konta iQua jest wymagany"}), 400

        existing = _get_section()
        section = dict(existing)
        password = form.get("password") or ""
        if password:
            section["password"] = password
        elif not existing.get("password"):
            return jsonify({"error": "Hasło konta iQua jest wymagane"}), 400
        section["username"] = username
        section.setdefault("devices", existing.get("devices", {}))
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Włączenie/wyłączenie integracji wciąż wymaga pełnego restartu procesu bridge'a."})

    @app.route("/api/plugins/iqua/devices", methods=["GET"])
    async def api_plugin_iqua_devices_get():
        section = _get_section()
        devices = section.get("devices") or {}
        return jsonify({
            "devices": [
                {"name": name, "serial_number": details.get("serial_number", "")}
                for name, details in sorted(devices.items())
            ],
        })

    @app.route("/api/plugins/iqua/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_iqua_devices_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict) or not isinstance(form.get("devices"), list):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        devices: dict[str, dict] = {}
        errors = []
        for row in form["devices"]:
            if not isinstance(row, dict):
                continue
            name = (row.get("name") or "").strip()
            serial_number = (row.get("serial_number") or "").strip()
            if not name:
                continue
            if not serial_number:
                errors.append(f"Urządzenie '{name}': brak numeru seryjnego (DSN#)")
                continue
            if name in devices:
                errors.append(f"Nazwa '{name}' powtórzona - nazwy muszą być unikalne")
                continue
            devices[name] = {"serial_number": serial_number}
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        existing = _get_section()
        if not existing.get("username") or not existing.get("password"):
            return jsonify({"error": "Najpierw zapisz login i hasło konta iQua wyżej."}), 400
        section = dict(existing)
        section["devices"] = devices
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(devices),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/iqua/devices/test", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_iqua_devices_test():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        serial_number = (form.get("serial_number") or "").strip()
        if not serial_number:
            return jsonify({"error": "Numer seryjny (DSN#) jest wymagany"}), 400

        section = _get_section()
        username, password = section.get("username", ""), section.get("password", "")
        if not username or not password:
            return jsonify({"error": "Najpierw zapisz login i hasło konta iQua wyżej."}), 400

        try:
            data = await test_connection(username, password, serial_number)
        except IquaError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd testu urządzenia iQua: {exc}"}), 502

        return jsonify({
            "model": data.model,
            "state": data.state.value,
            "salt_level_percent": data.salt_level_percent,
        })

    def _validate_enable() -> str | None:
        section = _get_section()
        if not section.get("username") or not section.get("password") or not section.get("devices"):
            return "Najpierw zapisz login/hasło i przynajmniej jedno urządzenie iQua, zanim włączysz wtyczkę."
        return None

    register_settings_route(
        app, "iqua", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_section, save_section=_save_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        validate_enable=_validate_enable,
    )

    return META
