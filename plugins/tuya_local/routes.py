from __future__ import annotations

import asyncio

from quart import Quart, jsonify, render_template, request

from bridge.tuya_local import (
    scan_network as scan_tuya_network,
    fetch_cloud_devices as fetch_tuya_cloud_devices,
    fetch_dp_schemas_for_devices,
)
from dashboard.setup import (
    build_tuya_devices_dict, validate_tuya_devices, tuya_devices_to_rows, zigbee_gateways_to_rows,
)
from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="tuya_local",
    name="Urządzenia Tuya (LAN)",
    icon="🔌",
    description="Urządzenia Tuya lokalne + bramki Zigbee, skan sieci LAN i import z Tuya Cloud.",
    config_url="/plugins/tuya",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    @app.route("/plugins/tuya")
    async def tuya_plugin_page():
        return await render_template("tuya_local/index.html")

    @app.route("/api/plugins/tuya/devices", methods=["GET"])
    async def api_plugin_tuya_devices_get():
        tuya = get_config_raw().get("tuya", {})
        return jsonify({
            "devices": tuya_devices_to_rows(tuya.get("devices", {})),
            "zigbee_gateways": zigbee_gateways_to_rows(tuya.get("zigbee_gateways", {})),
        })

    @app.route("/api/plugins/tuya/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_devices_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_tuya_devices(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        tuya_devices_dict = build_tuya_devices_dict(form)
        existing = get_config_store().get("tuya_devices")
        existing["devices"] = tuya_devices_dict["devices"]
        existing["zigbee_gateways"] = tuya_devices_dict["zigbee_gateways"]
        get_config_store().set("tuya_devices", existing)

        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(tuya_devices_dict["devices"]),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/tuya/scan", methods=["POST"])
    async def api_plugin_tuya_scan():
        tuya = get_config_raw().get("tuya", {})
        known_by_id: dict[str, str] = {}
        for name, cfg in tuya.get("cloud_devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name
        for name, cfg in tuya.get("devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name

        try:
            devices = await asyncio.get_event_loop().run_in_executor(None, scan_tuya_network)
        except Exception as exc:
            return jsonify({"error": f"Błąd skanowania: {exc}"}), 502
        for d in devices:
            d["known_name"] = known_by_id.get(d.get("id"), "")
        return jsonify({"devices": devices})

    @app.route("/api/plugins/tuya/cloud-import", methods=["POST"])
    async def api_plugin_tuya_cloud_import():
        payload = await request.get_json(silent=True) or {}
        api_region = (payload.get("api_region") or "").strip()
        access_id = (payload.get("access_id") or "").strip()
        access_key = (payload.get("access_key") or "").strip()
        if not api_region or not access_id or not access_key:
            return jsonify({"error": "Region, Access ID i Access Secret są wymagane"}), 400

        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, fetch_tuya_cloud_devices, api_region, access_id, access_key,
            )
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd importu z Tuya Cloud: {exc}"}), 502
        return jsonify({"devices": devices})

    @app.route("/api/plugins/tuya/devices/refresh-dp-schema", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_refresh_dp_schema():
        cloud_cfg = get_config_raw().get("tuya", {}).get("cloud", {}) or {}
        api_region = cloud_cfg.get("api_region", "")
        access_id = cloud_cfg.get("access_id", "")
        access_key = cloud_cfg.get("access_key", "")
        if not api_region or not access_id or not access_key:
            return jsonify({
                "error": "Najpierw zapisz poświadczenia w zakładce ☁ Tuya Cloud (region, Access ID, Access Secret).",
            }), 400

        devices = {name: dict(cfg) for name, cfg in get_config_raw().get("tuya", {}).get("devices", {}).items()}
        device_ids = {name: cfg["id"] for name, cfg in devices.items() if cfg.get("id")}
        if not device_ids:
            return jsonify({"error": "Brak skonfigurowanych urządzeń z ID do opisania"}), 400

        try:
            schemas, fetch_errors = await asyncio.get_event_loop().run_in_executor(
                None, fetch_dp_schemas_for_devices, api_region, access_id, access_key, list(device_ids.values()),
            )
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd komunikacji z Tuya Cloud: {exc}"}), 502

        updated, failed = [], []
        for name, dev_id in device_ids.items():
            if dev_id in schemas and schemas[dev_id]:
                devices[name]["dp_schema"] = schemas[dev_id]
                updated.append(name)
            else:
                failed.append(name)
        existing = get_config_store().get("tuya_devices")
        existing["devices"] = devices
        get_config_store().set("tuya_devices", existing)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "updated": updated, "failed": failed,
            "message": f"Zaktualizowano opisy pól dla {len(updated)}/{len(device_ids)} urządzeń.",
        })

    def _get_tuya_section() -> dict:
        return get_config_store().get("config").get("tuya", {})

    def _save_tuya_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["tuya"] = section
        get_config_store().set("config", current)

    register_settings_route(
        app, "tuya_local", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_tuya_section, save_section=_save_tuya_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
