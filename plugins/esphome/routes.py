# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import asyncio

from quart import Quart, jsonify, render_template, request

from bridge import energy_meters
from plugins import PluginMeta, register_settings_route


def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("esphome") or {}
    devices = cfg.get("devices") or {}
    return {
        "enabled": cfg.get("enabled", True) and bool(devices),
        "configured": bool(devices),
        "detail": f"{len(devices)} urządzeń" if devices else "brak urządzeń",
    }


META = PluginMeta(
    id="esphome",
    name="ESPHome",
    icon="🔌",
    description="Natywne połączenie (bez MQTT/HA) z urządzeniami ESPHome - czujniki, przełączniki, "
                "światła i przyciski jako warunki 'stan' w automatyzacjach i sterowanie z panelu.",
    config_url="/plugins/esphome",
    config_key="esphome",
    describe_state=describe_state,
    category="Dom",
)


def _mask_devices(devices: dict) -> list[dict]:
    return [
        {
            "name": name,
            "host": d.get("host", ""),
            "port": d.get("port", 6053),
            "label": d.get("label", ""),
            "enabled": d.get("enabled", True),
            "noise_psk_set": bool(d.get("noise_psk")),
            "password_set": bool(d.get("password")),
        }
        for name, d in sorted(devices.items())
    ]


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store, get_history,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_section() -> dict:
        return get_config_store().get("config").get("esphome") or {}

    def _save_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["esphome"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/esphome")
    async def esphome_plugin_page():
        return await render_template("esphome/index.html")

    @app.route("/api/plugins/esphome/devices", methods=["GET"])
    async def api_plugin_esphome_devices_get():
        return jsonify({"devices": _mask_devices(_get_section().get("devices") or {})})

    @app.route("/api/plugins/esphome/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_esphome_devices_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict) or not isinstance(form.get("devices"), list):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        existing = _get_section().get("devices") or {}
        devices: dict[str, dict] = {}
        errors = []
        for row in form["devices"]:
            if not isinstance(row, dict):
                continue
            name = (row.get("name") or "").strip()
            host = (row.get("host") or "").strip()
            if not name:
                continue
            if not host:
                errors.append(f"Urządzenie '{name}': brak adresu/hosta")
                continue
            if name in devices:
                errors.append(f"Nazwa '{name}' powtórzona - nazwy muszą być unikalne")
                continue
            try:
                port = int(row.get("port") or 6053)
            except (TypeError, ValueError):
                errors.append(f"Urządzenie '{name}': nieprawidłowy port")
                continue
            # Puste noise_psk/password = zachowaj dotychczasowe (ten sam wzorzec co
            # klucze AES liczników wM-Bus - patrz plugins/wmbus/routes.py).
            noise_psk = (row.get("noise_psk") or "").strip() or (existing.get(name) or {}).get("noise_psk", "")
            password = (row.get("password") or "").strip() or (existing.get(name) or {}).get("password", "")
            devices[name] = {
                "host": host, "port": port, "label": (row.get("label") or "").strip(),
                "enabled": bool(row.get("enabled", True)),
                "noise_psk": noise_psk, "password": password,
            }
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        section = dict(_get_section())
        section["devices"] = devices
        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(devices),
            "message": "Zapisano. Nowe/zmienione urządzenia ESPHome wymagają PEŁNEGO restartu procesu "
                       "bridge'a (połączenia są nawiązywane tylko przy starcie, w odróżnieniu od reszty "
                       "ustawień, które przeładowują się same w ciągu 1-2s).",
        })

    @app.route("/api/plugins/esphome/test", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_esphome_test():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        host = (form.get("host") or "").strip()
        if not host:
            return jsonify({"error": "Host jest wymagany"}), 400
        try:
            port = int(form.get("port") or 6053)
        except (TypeError, ValueError):
            return jsonify({"error": "Nieprawidłowy port"}), 400
        noise_psk = (form.get("noise_psk") or "").strip() or None
        password = (form.get("password") or "").strip() or None

        from aioesphomeapi import APIClient, APIConnectionError

        client = APIClient(host, port, password=password, noise_psk=noise_psk)
        try:
            await asyncio.wait_for(client.connect(login=True), timeout=10)
            info = await client.device_info()
            entities, _services = await client.list_entities_services()
        except asyncio.TimeoutError:
            return jsonify({"error": "Przekroczono czas połączenia (10s) - sprawdź adres/port"}), 502
        except APIConnectionError as exc:
            return jsonify({"error": f"Nie udało się połączyć: {exc}"}), 502
        finally:
            await client.disconnect()

        return jsonify({
            "name": info.name, "model": info.model, "esphome_version": info.esphome_version,
            "uses_password": info.uses_password, "entities": len(entities),
        })

    @app.route("/api/plugins/esphome/status", methods=["GET"])
    async def api_plugin_esphome_status():
        history = get_history()
        meta_by_key = {e["key"]: e for e in history.get_status("esphome_meta")}
        value_by_key = {e["key"]: e for e in history.get_status("esphome")}
        health_by_key = {e["key"]: e for e in history.get_status("system_health")}

        devices_cfg = _get_section().get("devices") or {}
        result = []
        for name, cfg in sorted(devices_cfg.items()):
            health_entry = health_by_key.get(f"health:esphome_{name}")
            prefix = f"esphome_meta:{name}:"
            entities = []
            for meta_key, meta_entry in meta_by_key.items():
                if not meta_key.startswith(prefix):
                    continue
                object_id = meta_key[len(prefix):]
                value_entry = value_by_key.get(f"esphome:{name}:{object_id}")
                entities.append({
                    "object_id": object_id,
                    "name": meta_entry["label"],
                    "kind": meta_entry["value"],
                    "value": value_entry["value"] if value_entry else None,
                    "ts": value_entry["ts"] if value_entry else None,
                    "is_energy_meter": energy_meters.is_energy_meter(history, f"esphome_{name}_{object_id}"),
                })
            entities.sort(key=lambda e: e["object_id"])
            result.append({
                "name": name,
                "label": cfg.get("label") or name,
                "host": cfg.get("host", ""),
                "enabled": cfg.get("enabled", True),
                "connected": bool(health_entry) and health_entry["value"].startswith("połączono"),
                "health_detail": health_entry["value"] if health_entry else "brak danych (bridge nie wystartował?)",
                "entities": entities,
            })
        return jsonify({"devices": result})

    @app.route("/api/plugins/esphome/energy_meter", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_esphome_energy_meter():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        device = (form.get("device") or "").strip()
        object_id = (form.get("object_id") or "").strip()
        if not device or not object_id:
            return jsonify({"error": "Wymagane: device, object_id"}), 400

        sensor = f"esphome_{device}_{object_id}"
        history = get_history()
        if form.get("flag", True):
            label = (form.get("label") or "").strip() or f"{device}: {object_id}"
            energy_meters.mark_energy_meter(history, sensor, label)
            message = f"Oznaczono '{object_id}' jako licznik energii."
        else:
            energy_meters.unmark_energy_meter(history, sensor)
            message = f"Odznaczono '{object_id}' jako licznik energii."
        energy_meters.sync_energy_sensors(history, get_config_store().get("config").get("energy"))
        return jsonify({"sensor": sensor, "message": message})

    @app.route("/api/plugins/esphome/control", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_esphome_control():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        device = (form.get("device") or "").strip()
        object_id = (form.get("object_id") or "").strip()
        action = (form.get("action") or "").strip()
        if not device or not object_id or action not in ("switch", "light", "button"):
            return jsonify({"error": "Wymagane: device, object_id, action (switch/light/button)"}), 400

        params = {"device": device, "object_id": object_id}
        if action == "switch":
            command = "esphome_switch"
            params["state"] = bool(form.get("state"))
        elif action == "light":
            command = "esphome_light"
            params["state"] = bool(form.get("state"))
            if form.get("brightness") is not None:
                try:
                    params["brightness"] = float(form["brightness"])
                except (TypeError, ValueError):
                    return jsonify({"error": "Nieprawidłowa jasność"}), 400
        else:
            command = "esphome_button"

        command_id = get_history().enqueue_command(command, params)
        return jsonify({"command_id": command_id, "status": "pending"})

    def _validate_enable() -> str | None:
        if not _get_section().get("devices"):
            return "Najpierw zapisz przynajmniej jedno urządzenie ESPHome, zanim włączysz wtyczkę."
        return None

    register_settings_route(
        app, "esphome", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_section, save_section=_save_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        validate_enable=_validate_enable,
    )

    return META
