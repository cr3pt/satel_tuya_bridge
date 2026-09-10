from __future__ import annotations

import asyncio
import ipaddress

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


def _zigbee_devices(config_raw: dict) -> dict:
    tuya_devices = (config_raw.get("tuya", {}) or {}).get("devices", {}) or {}
    return {name: cfg for name, cfg in tuya_devices.items() if cfg.get("gateway")}


def describe_state(config_raw: dict) -> dict:
    tuya = config_raw.get("tuya", {}) or {}
    tuya_devices = tuya.get("devices", {}) or {}
    zigbee_devices = _zigbee_devices(config_raw)
    return {
        "enabled": tuya.get("enabled", True) and len(tuya_devices) > len(zigbee_devices),
        "configured": len(tuya_devices) > len(zigbee_devices),
        "detail": f"{len(tuya_devices) - len(zigbee_devices)} urządzeń WiFi",
        "poll_interval_seconds": tuya.get("status_poll_interval_seconds", 30),
    }


def describe_zigbee_state(config_raw: dict) -> dict:
    tuya = config_raw.get("tuya", {}) or {}
    zigbee_devices = _zigbee_devices(config_raw)
    zigbee_gateways = tuya.get("zigbee_gateways", {}) or {}
    return {
        "enabled": tuya.get("enabled", True) and bool(zigbee_gateways),
        "configured": bool(zigbee_gateways),
        "detail": f"{len(zigbee_gateways)} bramek, {len(zigbee_devices)} urządzeń",
    }


META = PluginMeta(
    id="tuya_local",
    name="Tuya - urządzenia lokalne (WiFi)",
    icon="🔌",
    description="Sterowanie gniazdkami/przełącznikami/ściemniaczami Tuya bezpośrednio w LAN, bez chmury "
                "(tinytuya) - obejmuje też podurządzenia Zigbee.",
    config_url="/plugins/tuya",
    config_key="tuya",
    has_poll_interval=True,
    min_poll_interval_seconds=3,
    toggle_route="toggle_prereq_connections_live",
    describe_state=describe_state,
)

ZIGBEE_META = PluginMeta(
    id="tuya_zigbee",
    name="Tuya - bramki Zigbee",
    icon="🐝",
    description="Czujniki/przełączniki Zigbee podpięte pod bramkę Tuya (Tuya Zigbee Gateway/Hub) - dzieli "
                "połączenie z 'Tuya lokalnie' wyżej, brak osobnego przełącznika.",
    config_url="/plugins/tuya",
    toggleable=False,
    restart_required=False,
    describe_state=describe_zigbee_state,
)

METAS = [META, ZIGBEE_META]


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
        tuyadevices_for_scan = []
        for name, cfg in tuya.get("cloud_devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name
        for name, cfg in tuya.get("devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name
            if cfg.get("id") and cfg.get("local_key"):
                tuyadevices_for_scan.append({"id": cfg["id"], "key": cfg["local_key"], "name": name})
        scan_networks = tuya.get("scan_networks") or []

        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: scan_tuya_network(networks=scan_networks, tuyadevices=tuyadevices_for_scan),
            )
        except Exception as exc:
            return jsonify({"error": f"Błąd skanowania: {exc}"}), 502
        for d in devices:
            d["known_name"] = known_by_id.get(d.get("id"), "")
        return jsonify({"devices": devices})

    @app.route("/api/plugins/tuya/scan-networks", methods=["GET"])
    async def api_plugin_tuya_scan_networks_get():
        tuya = get_config_raw().get("tuya", {})
        return jsonify({"scan_networks": tuya.get("scan_networks") or []})

    @app.route("/api/plugins/tuya/scan-networks", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_scan_networks_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict) or not isinstance(form.get("scan_networks"), list):
            return jsonify({"error": "Nieprawidłowe dane - oczekiwano listy 'scan_networks'"}), 400

        networks = []
        for entry in form["scan_networks"]:
            value = (entry or "").strip() if isinstance(entry, str) else ""
            if not value:
                continue
            try:
                ipaddress.ip_network(value, strict=False)
            except ValueError:
                return jsonify({"error": f"Nieprawidłowy adres IP albo zakres CIDR: '{value}'"}), 400
            networks.append(value)

        current = get_config_store().get("config")
        current.setdefault("tuya", {})["scan_networks"] = networks
        get_config_store().set("config", current)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "scan_networks": networks,
            "message": "Zapisano. Zmiana zadziała przy następnym skanowaniu, bez restartu procesu.",
        })

    @app.route("/api/plugins/tuya/cloud-import", methods=["POST"])
    async def api_plugin_tuya_cloud_import():
        payload = await request.get_json(silent=True) or {}
        api_region = (payload.get("api_region") or "").strip()
        access_id = (payload.get("access_id") or "").strip()
        access_key = (payload.get("access_key") or "").strip()
        if not api_region or not access_id or not access_key:
            return jsonify({"error": "Region, Access ID i Access Secret są wymagane"}), 400

        scan_networks = get_config_raw().get("tuya", {}).get("scan_networks") or []
        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, fetch_tuya_cloud_devices, api_region, access_id, access_key, scan_networks,
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
