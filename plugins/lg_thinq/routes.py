from __future__ import annotations

import uuid

from quart import Quart, jsonify, render_template, request

from bridge.lg_thinq_client import LgThinqError, fetch_possible_states, list_devices
from plugins import PluginMeta, register_settings_route


def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("lg_thinq") or {}
    return {
        "enabled": cfg.get("enabled", True) and bool(cfg.get("personal_access_token") and cfg.get("devices")),
        "configured": bool(cfg.get("personal_access_token") and cfg.get("devices")),
        "detail": (
            f"{len(cfg.get('devices', {}))} urządzeń" if cfg.get("devices")
            else "brak tokenu PAT/urządzeń"
        ),
    }


META = PluginMeta(
    id="lg_thinq",
    name="Pralki/AGD LG ThinQ",
    icon="🧺",
    description="Stan pralek, suszarek, zmywarek i innego AGD LG sterowanego przez oficjalne API LG ThinQ "
                "Connect - dostępny jako warunek 'stan' w automatyzacjach i jako trigger Flow.",
    config_url="/plugins/lg_thinq",
    config_key="lg_thinq",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_section() -> dict:
        return get_config_store().get("config").get("lg_thinq") or {}

    def _save_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["lg_thinq"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/lg_thinq")
    async def lg_thinq_plugin_page():
        return await render_template("lg_thinq/index.html")

    @app.route("/api/plugins/lg_thinq/credentials", methods=["GET"])
    async def api_plugin_lg_thinq_credentials_get():
        section = _get_section()
        return jsonify({
            "pat_set": bool(section.get("personal_access_token")),
            "country_code": section.get("country_code", "PL"),
            "client_id": section.get("client_id", ""),
        })

    @app.route("/api/plugins/lg_thinq/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_lg_thinq_credentials_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        country_code = (form.get("country_code") or "").strip().upper()
        if not country_code:
            return jsonify({"error": "Kod kraju (np. PL) jest wymagany"}), 400

        existing = _get_section()
        section = dict(existing)
        pat = (form.get("personal_access_token") or "").strip()
        if pat:
            section["personal_access_token"] = pat
        elif not existing.get("personal_access_token"):
            return jsonify({"error": "Token PAT (Personal Access Token) jest wymagany"}), 400
        section["country_code"] = country_code
        # client_id musi być stały (ten sam token PAT + inny client_id = LG traktuje to
        # jako inne urządzenie klienckie) - generujemy raz i trzymamy, żeby użytkownik
        # nie musiał sam wymyślać UUID4.
        section.setdefault("client_id", existing.get("client_id") or str(uuid.uuid4()))
        section.setdefault("devices", existing.get("devices", {}))
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Włączenie/wyłączenie integracji wciąż wymaga pełnego restartu procesu bridge'a."})

    @app.route("/api/plugins/lg_thinq/devices", methods=["GET"])
    async def api_plugin_lg_thinq_devices_get():
        section = _get_section()
        devices = section.get("devices") or {}
        return jsonify({
            "devices": [
                {"name": name, "device_id": details.get("device_id", "")}
                for name, details in sorted(devices.items())
            ],
        })

    @app.route("/api/plugins/lg_thinq/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_lg_thinq_devices_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict) or not isinstance(form.get("devices"), list):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        devices: dict[str, dict] = {}
        errors = []
        for row in form["devices"]:
            if not isinstance(row, dict):
                continue
            name = (row.get("name") or "").strip()
            device_id = (row.get("device_id") or "").strip()
            if not name:
                continue
            if not device_id:
                errors.append(f"Urządzenie '{name}': brak ID urządzenia")
                continue
            if name in devices:
                errors.append(f"Nazwa '{name}' powtórzona - nazwy muszą być unikalne")
                continue
            devices[name] = {"device_id": device_id}
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        existing = _get_section()
        if not existing.get("personal_access_token"):
            return jsonify({"error": "Najpierw zapisz token PAT konta LG ThinQ wyżej."}), 400
        section = dict(existing)
        section["devices"] = devices
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(devices),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/lg_thinq/discover", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_lg_thinq_discover():
        section = _get_section()
        pat, country_code, client_id = (
            section.get("personal_access_token", ""), section.get("country_code", "PL"), section.get("client_id", ""),
        )
        if not pat or not client_id:
            return jsonify({"error": "Najpierw zapisz token PAT (i kod kraju) konta LG ThinQ wyżej."}), 400

        try:
            devices = await list_devices(pat, country_code, client_id)
        except LgThinqError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd odkrywania urządzeń LG ThinQ: {exc}"}), 502

        return jsonify({
            "devices": [
                {
                    "device_id": d["device_id"],
                    "name": d.get("alias") or d["device_id"],
                    "device_type": d.get("device_type", ""),
                    "model_name": d.get("model_name", ""),
                }
                for d in devices
            ],
        })

    @app.route("/api/plugins/lg_thinq/devices/<name>/states", methods=["GET"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_lg_thinq_device_states(name):
        section = _get_section()
        pat, country_code, client_id = (
            section.get("personal_access_token", ""), section.get("country_code", "PL"), section.get("client_id", ""),
        )
        if not pat or not client_id:
            return jsonify({"error": "Najpierw zapisz token PAT konta LG ThinQ wyżej."}), 400
        device = (section.get("devices") or {}).get(name)
        if not device or not device.get("device_id"):
            return jsonify({"error": f"Nieznane urządzenie '{name}' - zapisz je najpierw niżej."}), 404

        try:
            states = await fetch_possible_states(pat, country_code, client_id, device["device_id"])
        except LgThinqError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd pobierania listy stanów urządzenia: {exc}"}), 502

        return jsonify({"states": states})

    def _validate_enable() -> str | None:
        section = _get_section()
        if not section.get("personal_access_token") or not section.get("devices"):
            return "Najpierw zapisz token PAT i przynajmniej jedno urządzenie LG ThinQ, zanim włączysz wtyczkę."
        return None

    register_settings_route(
        app, "lg_thinq", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_section, save_section=_save_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        validate_enable=_validate_enable,
    )

    return META
