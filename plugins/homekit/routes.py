from __future__ import annotations

import json
import pathlib

from pyhap.util import generate_pincode
from quart import Quart, jsonify, render_template, request

from bridge.homekit_bridge import TUYA_ACCESSORY_TYPES, resolve_homekit_config
from plugins import PluginMeta, register_settings_route


def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("homekit", {}) or {}
    enabled = bool(cfg.get("enabled"))
    return {
        "enabled": enabled,
        "configured": True,
        "detail": f"port {cfg.get('port', _DEFAULT_PORT)}" if enabled else "wyłączony",
    }


META = PluginMeta(
    id="homekit",
    name="Most HomeKit (Apple)",
    icon="🏡",
    description="Natywny most HomeKit (Apple Home) - centrala Satel jako system alarmowy, urządzenia "
                "Tuya, sceny.",
    config_url="/plugins/homekit",
    config_key="homekit",
    toggle_route="toggle_no_prereq",
    describe_state=describe_state,
)

_DEFAULT_PORT = 51826
_DEFAULT_BRIDGE_NAME = "Satel-Tuya-eBUS"


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store, get_config_path,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_homekit_section() -> dict:
        return get_config_store().get("config").get("homekit", {}) or {}

    def _save_homekit_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["homekit"] = section
        get_config_store().set("config", current)

    def _persist_path() -> pathlib.Path:
        return pathlib.Path(get_config_path()).parent / "homekit_state.json"

    def _is_paired() -> bool:
        persist_path = _persist_path()
        if not persist_path.exists():
            return False
        try:
            data = json.loads(persist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(data.get("paired_clients"))

    @app.route("/plugins/homekit")
    async def homekit_plugin_page():
        return await render_template("homekit/index.html")

    @app.route("/api/plugins/homekit/config", methods=["GET"])
    async def api_plugin_homekit_config_get():
        section = _get_homekit_section()
        return jsonify({
            "port": section.get("port", _DEFAULT_PORT),
            "bridge_name": section.get("bridge_name", _DEFAULT_BRIDGE_NAME),
            "pincode": section.get("pincode"),
            "paired": _is_paired(),
        })

    @app.route("/api/plugins/homekit/config", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homekit_config_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        section = dict(_get_homekit_section())
        if "port" in form:
            try:
                port = int(form["port"])
            except (TypeError, ValueError):
                return jsonify({"error": "Port musi być liczbą"}), 400
            if not (1 <= port <= 65535):
                return jsonify({"error": "Port musi być w zakresie 1-65535"}), 400
            section["port"] = port
        if "bridge_name" in form:
            name = (form.get("bridge_name") or "").strip()
            section["bridge_name"] = name or _DEFAULT_BRIDGE_NAME

        section.setdefault("pincode", generate_pincode().decode())

        _save_homekit_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "message": "Zapisano. Zmiana portu/nazwy mostu wymaga pełnego restartu procesu bridge'a "
                       "(zakładka ⚙ Ustawienia ogólne -> \"Zrestartuj bridge i panel teraz\").",
        })

    @app.route("/api/plugins/homekit/regenerate-pincode", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homekit_regenerate_pincode_post():
        section = dict(_get_homekit_section())
        section["pincode"] = generate_pincode().decode()
        _save_homekit_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()
        return jsonify({
            "pincode": section["pincode"],
            "message": "Wygenerowano nowy kod parowania - zacznie obowiązywać po pełnym restarcie "
                       "procesu bridge'a.",
        })

    @app.route("/api/plugins/homekit/reset-pairings", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homekit_reset_pairings_post():
        _persist_path().unlink(missing_ok=True)
        return jsonify({
            "message": "Usunięto stan parowania - wszystkie dotąd sparowane urządzenia (w tym Home app) "
                       "będą wymagały ponownego dodania mostu. Wymagany pełny restart procesu bridge'a.",
        })

    @app.route("/api/plugins/homekit/entities", methods=["GET"])
    async def api_plugin_homekit_entities_get():
        resolved = resolve_homekit_config(get_config_raw())
        return jsonify({
            "rooms": resolved["rooms"],
            "partitions": [
                {"name": name, "enabled": info["enabled"]}
                for name, info in resolved["partitions"].items()
            ],
            "devices": [
                {"name": name, "type": info["type"], "room": info["room"], "is_dimmer": info["is_dimmer"]}
                for name, info in resolved["devices"].items()
            ],
            "scenes": [
                {"name": name, "enabled": info["enabled"]}
                for name, info in resolved["scenes"].items()
            ],
            "type_choices": [
                {"id": type_id, "label": type_def["label"]}
                for type_id, type_def in TUYA_ACCESSORY_TYPES.items()
            ],
        })

    @app.route("/api/plugins/homekit/entities", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_homekit_entities_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        resolved = resolve_homekit_config(get_config_raw())
        known_partitions = set(resolved["partitions"])
        known_devices = set(resolved["devices"])
        known_scenes = set(resolved["scenes"])

        rooms = [str(r).strip() for r in (form.get("rooms") or []) if str(r).strip()]
        seen_rooms = []
        for room in rooms:
            if room not in seen_rooms:
                seen_rooms.append(room)
        rooms = seen_rooms

        partitions = {}
        for name, patch in (form.get("partitions") or {}).items():
            if name not in known_partitions:
                return jsonify({"error": f"Nieznana partycja: '{name}'"}), 400
            partitions[name] = {"enabled": bool((patch or {}).get("enabled"))}

        devices = {}
        for name, patch in (form.get("devices") or {}).items():
            if name not in known_devices:
                return jsonify({"error": f"Nieznane urządzenie Tuya: '{name}'"}), 400
            type_id = (patch or {}).get("type") or ""
            if type_id and type_id not in TUYA_ACCESSORY_TYPES:
                return jsonify({"error": f"Nieznany rodzaj urządzenia: '{type_id}'"}), 400
            room = ((patch or {}).get("room") or "").strip()
            devices[name] = {"type": type_id, "room": room}

        scenes = {}
        for name, patch in (form.get("scenes") or {}).items():
            if name not in known_scenes:
                return jsonify({"error": f"Nieznana scena: '{name}'"}), 400
            scenes[name] = {"enabled": bool((patch or {}).get("enabled"))}

        section = dict(_get_homekit_section())
        section["rooms"] = rooms
        section["partitions"] = partitions
        section["devices"] = devices
        section["scenes"] = scenes
        _save_homekit_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "message": "Zapisano. Zmiana listy udostępnionych urządzeń/partycji/scen wymaga "
                       "pełnego restartu procesu bridge'a (zakładka ⚙ Ustawienia ogólne -> "
                       "\"Zrestartuj bridge i panel teraz\").",
        })

    register_settings_route(
        app, "homekit", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_homekit_section, save_section=_save_homekit_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        default_enabled=False,
    )

    return META
