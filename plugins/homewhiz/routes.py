# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import asyncio

from quart import Quart, jsonify, render_template, request

from bridge.homewhiz_client import (
    HomeWhizError, fetch_config_descriptor, is_dishwasher, list_appliances, list_possible_states, login,
)
from plugins import PluginMeta, register_settings_route

def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("homewhiz") or {}
    return {
        "enabled": cfg.get("enabled", True) and bool(cfg.get("username") and cfg.get("appliances")),
        "configured": bool(cfg.get("username") and cfg.get("appliances")),
        "detail": (
            f"{len(cfg.get('appliances', {}))} urządzeń" if cfg.get("appliances")
            else "brak poświadczeń/urządzeń"
        ),
    }


META = PluginMeta(
    id="homewhiz",
    name="Zmywarki/AGD HomeWhiz",
    icon="🍽",
    description="Stan zmywarek (i innego AGD) BEQ/Arcelik sterowanych przez chmurę HomeWhiz - dostępny "
                "jako warunek 'stan' w automatyzacjach.",
    config_url="/plugins/homewhiz",
    config_key="homewhiz",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_section() -> dict:
        return get_config_store().get("config").get("homewhiz") or {}

    def _save_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["homewhiz"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/homewhiz")
    async def homewhiz_plugin_page():
        return await render_template("homewhiz/index.html")

    @app.route("/api/plugins/homewhiz/credentials", methods=["GET"])
    async def api_plugin_homewhiz_credentials_get():
        section = _get_section()
        return jsonify({
            "username": section.get("username", ""),
            "password_set": bool(section.get("password")),
            "language": section.get("language", "en"),
        })

    @app.route("/api/plugins/homewhiz/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homewhiz_credentials_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        username = (form.get("username") or "").strip()
        if not username:
            return jsonify({"error": "Login (e-mail) do konta HomeWhiz jest wymagany"}), 400

        existing = _get_section()
        section = dict(existing)
        section.update({
            "username": username,
            "password": (form.get("password") or "").strip() or existing.get("password", ""),
            "language": (form.get("language") or "").strip() or existing.get("language", "en"),
        })
        section.setdefault("appliances", existing.get("appliances", {}))
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Włączenie/wyłączenie integracji wciąż wymaga pełnego restartu procesu bridge'a."})

    @app.route("/api/plugins/homewhiz/appliances", methods=["GET"])
    async def api_plugin_homewhiz_appliances_get():
        section = _get_section()
        appliances = section.get("appliances") or {}
        return jsonify({
            "appliances": [
                {"name": name, "appliance_id": details.get("appliance_id", "")}
                for name, details in sorted(appliances.items())
            ],
        })

    @app.route("/api/plugins/homewhiz/appliances", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homewhiz_appliances_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict) or not isinstance(form.get("appliances"), list):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        appliances: dict[str, dict] = {}
        errors = []
        for row in form["appliances"]:
            if not isinstance(row, dict):
                continue
            name = (row.get("name") or "").strip()
            appliance_id = (row.get("appliance_id") or "").strip()
            if not name:
                continue
            if not appliance_id:
                errors.append(f"Urządzenie '{name}': brak ID urządzenia")
                continue
            if name in appliances:
                errors.append(f"Nazwa '{name}' powtórzona - nazwy muszą być unikalne")
                continue
            appliances[name] = {"appliance_id": appliance_id}
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        existing = _get_section()
        if not existing.get("username"):
            return jsonify({"error": "Najpierw zapisz poświadczenia konta HomeWhiz wyżej."}), 400
        section = dict(existing)
        section["appliances"] = appliances
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(appliances),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/homewhiz/discover", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homewhiz_discover():
        section = _get_section()
        username, password = section.get("username", ""), section.get("password", "")
        if not username or not password:
            return jsonify({"error": "Najpierw zapisz login i hasło do konta HomeWhiz wyżej."}), 400

        async def _discover():
            session = await login(username, password)
            return await list_appliances(session)

        try:
            appliances = await _discover()
        except HomeWhizError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd odkrywania urządzeń HomeWhiz: {exc}"}), 502

        return jsonify({
            "appliances": [
                {
                    "appliance_id": a.get("applianceId", ""),
                    "name": a.get("name") or a.get("model") or a.get("applianceId", ""),
                    "brand": a.get("brand", ""),
                    "model": a.get("model", ""),
                    "is_dishwasher": is_dishwasher(a),
                }
                for a in appliances
            ],
        })

    @app.route("/api/plugins/homewhiz/appliances/<name>/states", methods=["GET"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homewhiz_appliance_states(name):
        section = _get_section()
        username, password = section.get("username", ""), section.get("password", "")
        if not username or not password:
            return jsonify({"error": "Najpierw zapisz login i hasło do konta HomeWhiz wyżej."}), 400
        appliance = (section.get("appliances") or {}).get(name)
        if not appliance or not appliance.get("appliance_id"):
            return jsonify({"error": f"Nieznane urządzenie '{name}' - zapisz je najpierw niżej."}), 404

        async def _fetch():
            session = await login(username, password)
            descriptor = await fetch_config_descriptor(
                session, appliance["appliance_id"], language=section.get("language", "en"),
            )
            return list_possible_states(descriptor)

        try:
            states = await _fetch()
        except HomeWhizError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd pobierania listy stanów urządzenia: {exc}"}), 502

        return jsonify({"states": states})

    def _validate_enable() -> str | None:
        section = _get_section()
        if not section.get("username") or not section.get("appliances"):
            return "Najpierw zapisz poświadczenia i przynajmniej jedno urządzenie HomeWhiz, zanim włączysz wtyczkę."
        return None

    register_settings_route(
        app, "homewhiz", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_section, save_section=_save_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        validate_enable=_validate_enable,
    )

    return META
