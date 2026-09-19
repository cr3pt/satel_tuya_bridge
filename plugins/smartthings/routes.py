from __future__ import annotations

from quart import Quart, jsonify, render_template, request

from bridge.smartthings_client import SmartThingsError, list_devices
from plugins import PluginMeta, register_settings_route


def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("smartthings") or {}
    return {
        "enabled": cfg.get("enabled", True) and bool(cfg.get("personal_access_token") and cfg.get("devices")),
        "configured": bool(cfg.get("personal_access_token") and cfg.get("devices")),
        "detail": (
            f"{len(cfg.get('devices', {}))} urządzeń" if cfg.get("devices")
            else "brak tokenu/urządzeń"
        ),
    }


META = PluginMeta(
    id="smartthings",
    name="SmartThings",
    icon="🔷",
    description="Stan urządzeń podłączonych przez chmurę SmartThings (Samsung) - odczyt dowolnej "
                "capability/attribute (domyślnie przełącznik) - dostępny jako warunek 'stan' w "
                "automatyzacjach i jako trigger Flow.",
    config_url="/plugins/smartthings",
    config_key="smartthings",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_section() -> dict:
        return get_config_store().get("config").get("smartthings") or {}

    def _save_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["smartthings"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/smartthings")
    async def smartthings_plugin_page():
        return await render_template("smartthings/index.html")

    @app.route("/api/plugins/smartthings/credentials", methods=["GET"])
    async def api_plugin_smartthings_credentials_get():
        section = _get_section()
        return jsonify({
            "pat_set": bool(section.get("personal_access_token")),
        })

    @app.route("/api/plugins/smartthings/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_smartthings_credentials_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        existing = _get_section()
        section = dict(existing)
        pat = (form.get("personal_access_token") or "").strip()
        if pat:
            section["personal_access_token"] = pat
        elif not existing.get("personal_access_token"):
            return jsonify({"error": "Token dostępu (Personal Access Token) jest wymagany"}), 400
        section.setdefault("devices", existing.get("devices", {}))
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Włączenie/wyłączenie integracji wciąż wymaga pełnego restartu procesu bridge'a."})

    @app.route("/api/plugins/smartthings/devices", methods=["GET"])
    async def api_plugin_smartthings_devices_get():
        section = _get_section()
        devices = section.get("devices") or {}
        return jsonify({
            "devices": [
                {
                    "name": name,
                    "device_id": details.get("device_id", ""),
                    "capability": details.get("capability", "switch"),
                    "attribute": details.get("attribute", "switch"),
                }
                for name, details in sorted(devices.items())
            ],
        })

    @app.route("/api/plugins/smartthings/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_smartthings_devices_post():
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
            devices[name] = {
                "device_id": device_id,
                "capability": (row.get("capability") or "").strip() or "switch",
                "attribute": (row.get("attribute") or "").strip() or "switch",
            }
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        existing = _get_section()
        if not existing.get("personal_access_token"):
            return jsonify({"error": "Najpierw zapisz token dostępu konta SmartThings wyżej."}), 400
        section = dict(existing)
        section["devices"] = devices
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(devices),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/smartthings/discover", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_smartthings_discover():
        section = _get_section()
        pat = section.get("personal_access_token", "")
        if not pat:
            return jsonify({"error": "Najpierw zapisz token dostępu konta SmartThings wyżej."}), 400

        try:
            devices = await list_devices(pat)
        except SmartThingsError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd odkrywania urządzeń SmartThings: {exc}"}), 502

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

    def _validate_enable() -> str | None:
        section = _get_section()
        if not section.get("personal_access_token") or not section.get("devices"):
            return "Najpierw zapisz token dostępu i przynajmniej jedno urządzenie SmartThings, zanim włączysz wtyczkę."
        return None

    register_settings_route(
        app, "smartthings", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_section, save_section=_save_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        validate_enable=_validate_enable,
    )

    return META
