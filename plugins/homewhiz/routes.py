"""Wtyczka zmywarek/AGD HomeWhiz (BEQ/Arcelik) - poświadczenia konta chmury
HomeWhiz + odkrywanie/wybór monitorowanych urządzeń. Patrz
bridge/homewhiz_client.py po sam mechanizm połączenia (SigV4/MQTT-over-
WebSocket do AWS IoT Core) i plugins/satel/routes.py po ten sam wzorzec
wstrzykiwania zależności."""
from __future__ import annotations

import asyncio

from flask import Flask, jsonify, render_template, request

from bridge.homewhiz_client import HomeWhizError, is_dishwasher, list_appliances, login
from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="homewhiz",
    name="Zmywarki/AGD HomeWhiz",
    icon="🍽",
    description="Stan zmywarek (i innego AGD) BEQ/Arcelik sterowanych przez chmurę HomeWhiz.",
    config_url="/plugins/homewhiz",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:
    """Rejestruje routing wtyczki HomeWhiz. `_ignored` połyka zależności
    przeznaczone dla INNYCH wtyczek - register_all() w plugins/__init__.py
    woła każdą wtyczkę z tym samym, wspólnym zestawem `deps`."""

    def _get_section() -> dict:
        return get_config_store().get("config").get("homewhiz") or {}

    def _save_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["homewhiz"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/homewhiz")
    def homewhiz_plugin_page():
        return render_template("homewhiz/index.html")

    @app.route("/api/plugins/homewhiz/credentials", methods=["GET"])
    def api_plugin_homewhiz_credentials_get():
        section = _get_section()
        return jsonify({
            "username": section.get("username", ""),
            "password_set": bool(section.get("password")),
            "language": section.get("language", "en"),
        })

    @app.route("/api/plugins/homewhiz/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    def api_plugin_homewhiz_credentials_post():
        form = request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        username = (form.get("username") or "").strip()
        if not username:
            return jsonify({"error": "Login (e-mail) do konta HomeWhiz jest wymagany"}), 400

        existing = _get_section()
        # Pole-sekret (hasło): pusta wartość w formularzu = ZACHOWAJ
        # dotychczasowe - patrz GET wyżej, tylko flaga password_set.
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
    def api_plugin_homewhiz_appliances_get():
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
    def api_plugin_homewhiz_appliances_post():
        form = request.get_json(silent=True)
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
    def api_plugin_homewhiz_discover():
        """Odkrywa urządzenia przypisane do konta HomeWhiz, przy użyciu JUŻ
        ZAPISANYCH poświadczeń (zakładka wyżej) - analogicznie do importu w
        panelu Tuya Cloud (plugins/tuya_cloud/routes.py)."""
        section = _get_section()
        username, password = section.get("username", ""), section.get("password", "")
        if not username or not password:
            return jsonify({"error": "Najpierw zapisz login i hasło do konta HomeWhiz wyżej."}), 400

        async def _discover():
            session = await login(username, password)
            return await list_appliances(session)

        try:
            appliances = asyncio.run(_discover())
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
