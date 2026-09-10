from __future__ import annotations

import asyncio
import ipaddress

from quart import Quart, jsonify, redirect, render_template, request

from bridge.tuya_local import (
    TUYA_CLOUD_REGIONS,
    scan_network as scan_tuya_network,
    fetch_cloud_devices as fetch_tuya_cloud_devices,
    fetch_dp_schemas_for_devices,
)
from dashboard.setup import (
    build_tuya_devices_dict, validate_tuya_devices, validate_cloud_devices,
    tuya_devices_to_rows, zigbee_gateways_to_rows, cloud_devices_to_rows,
)
from plugins import PluginMeta, register_settings_route


def _zigbee_devices(config_raw: dict) -> dict:
    tuya_devices = (config_raw.get("tuya", {}) or {}).get("devices", {}) or {}
    return {name: cfg for name, cfg in tuya_devices.items() if cfg.get("gateway")}


def describe_state(config_raw: dict) -> dict:
    tuya = config_raw.get("tuya", {}) or {}
    tuya_devices = tuya.get("devices", {}) or {}
    zigbee_devices = _zigbee_devices(config_raw)
    cloud_devices = tuya.get("cloud_devices", {}) or {}
    cloud = tuya.get("cloud", {}) or {}
    detail_parts = [f"{len(tuya_devices) - len(zigbee_devices)} WiFi"]
    if zigbee_devices:
        detail_parts.append(f"{len(zigbee_devices)} Zigbee")
    if cloud_devices:
        detail_parts.append(f"{len(cloud_devices)} chmurowych")
    return {
        "enabled": tuya.get("enabled", True) and (len(tuya_devices) > len(zigbee_devices) or bool(cloud.get("access_id"))),
        "configured": len(tuya_devices) > len(zigbee_devices) or bool(cloud.get("access_id")),
        "detail": ", ".join(detail_parts),
        "poll_interval_seconds": tuya.get("status_poll_interval_seconds", 30),
    }


META = PluginMeta(
    id="tuya_local",
    name="Tuya (LAN + Cloud + Zigbee)",
    icon="🔌",
    description="Wszystko związane z Tuya w jednym miejscu: gniazdka/przełączniki/ściemniacze sterowane "
                "bezpośrednio w LAN (tinytuya), bramki i podurządzenia Zigbee, oraz import/sterowanie "
                "przez Tuya Cloud (w tym detekcja ruchu z kamer Tuya).",
    config_url="/plugins/tuya",
    config_key="tuya",
    has_poll_interval=True,
    min_poll_interval_seconds=3,
    toggle_route="toggle_prereq_connections_live",
    describe_state=describe_state,
    category="Tuya",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_cloud_section() -> dict:
        return get_config_store().get("config").get("tuya", {}).get("cloud", {}) or {}

    def _save_cloud_section(section: dict) -> None:
        current = get_config_store().get("config")
        current.setdefault("tuya", {})["cloud"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/tuya")
    async def tuya_plugin_page():
        return await render_template("tuya_local/index.html", regions=TUYA_CLOUD_REGIONS)

    @app.route("/plugins/tuya_cloud")
    async def tuya_cloud_plugin_page_redirect():
        # Tuya Cloud (dawniej osobna wtyczka) - teraz jedna, wspólna strona Tuya.
        return redirect("/plugins/tuya")

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
        known_gateway_by_id: dict[str, str] = {}
        tuyadevices_for_scan = []
        for name, cfg in tuya.get("cloud_devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name
        for name, cfg in tuya.get("devices", {}).items():
            if cfg.get("id"):
                known_by_id[cfg["id"]] = name
            if cfg.get("id") and cfg.get("local_key"):
                tuyadevices_for_scan.append({"id": cfg["id"], "key": cfg["local_key"], "name": name})
        for name, cfg in tuya.get("zigbee_gateways", {}).items():
            if cfg.get("id"):
                known_gateway_by_id[cfg["id"]] = name
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
            # Bramka Zigbee zapisana już wcześniej (np. bez adresu IP, dopóki nie
            # odpowiadała w sieci) - osobne pole niż known_name, żeby panel mógł
            # od razu zaproponować uzupełnienie jej adresu IP zamiast dodawać
            # duplikat jako zwykłe urządzenie.
            d["known_gateway_name"] = known_gateway_by_id.get(d.get("id"), "")
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
        wide_ranges = []
        for entry in form["scan_networks"]:
            value = (entry or "").strip() if isinstance(entry, str) else ""
            if not value:
                continue
            try:
                net = ipaddress.ip_network(value, strict=False)
            except ValueError:
                return jsonify({"error": f"Nieprawidłowy adres IP albo zakres CIDR: '{value}'"}), 400
            if net.num_addresses > 64:
                wide_ranges.append(f"{value} ({net.num_addresses} adresów)")
            networks.append(value)

        current = get_config_store().get("config")
        current.setdefault("tuya", {})["scan_networks"] = networks
        get_config_store().set("config", current)
        reload_bridge_config()
        enqueue_bridge_reload()

        message = "Zapisano. Zmiana zadziała przy następnym skanowaniu, bez restartu procesu."
        if wide_ranges:
            message += (
                " ⚠️ Bardzo szeroki zakres: " + ", ".join(wide_ranges) + " - aktywny skan tylu adresów "
                "naraz jest wolny i może chwilowo zakłócić komunikację z już połączonymi urządzeniami "
                "Tuya (błędy typu \"Check device key or version\" na innych urządzeniach w trakcie "
                "skanowania) - rozważ węższy zakres albo pojedyncze adresy zamiast całej podsieci."
            )

        return jsonify({
            "scan_networks": networks,
            "message": message,
        })

    @app.route("/api/plugins/tuya_cloud/credentials", methods=["GET"])
    async def api_plugin_tuya_cloud_credentials_get():
        cloud = _get_cloud_section()
        return jsonify({
            "access_id": cloud.get("access_id", ""),
            "access_key_set": bool(cloud.get("access_key")),
            "api_region": cloud.get("api_region", "eu"),
            "api_endpoint": cloud.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": cloud.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
        })

    @app.route("/api/plugins/tuya_cloud/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_credentials_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        access_id = (form.get("access_id") or "").strip()
        if not access_id:
            return jsonify({"error": "Access ID jest wymagany"}), 400
        api_region = (form.get("api_region") or "").strip()
        if api_region and api_region not in TUYA_CLOUD_REGIONS:
            return jsonify({"error": f"Nieznany region: {api_region}"}), 400

        existing = _get_cloud_section()
        section = dict(existing)
        section.update({
            "access_id": access_id,
            "access_key": (form.get("access_key") or "").strip() or existing.get("access_key", ""),
            "api_region": api_region or existing.get("api_region", "eu"),
            "api_endpoint": (form.get("api_endpoint") or "").strip() or existing.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": (form.get("mq_endpoint") or "").strip() or existing.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
        })
        _save_cloud_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu."})

    @app.route("/api/plugins/tuya_cloud/devices", methods=["GET"])
    async def api_plugin_tuya_cloud_devices_get():
        cloud_devices = get_config_raw().get("tuya", {}).get("cloud_devices", {})
        return jsonify({"devices": cloud_devices_to_rows(cloud_devices)})

    @app.route("/api/plugins/tuya_cloud/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_devices_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_cloud_devices(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        tuya_devices_dict = build_tuya_devices_dict(form)
        existing = get_config_store().get("tuya_devices")
        existing["cloud_devices"] = tuya_devices_dict["cloud_devices"]
        get_config_store().set("tuya_devices", existing)

        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(tuya_devices_dict["cloud_devices"]),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/tuya_cloud/cloud-import", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_cloud_import():
        cloud = _get_cloud_section()
        api_region = cloud.get("api_region", "")
        access_id = cloud.get("access_id", "")
        access_key = cloud.get("access_key", "")
        if not api_region or not access_id or not access_key:
            return jsonify({
                "error": "Najpierw zapisz poświadczenia Tuya Cloud wyżej (region, Access ID, Access Secret).",
            }), 400

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

    async def _refresh_dp_schema(devices: dict, api_region: str, access_id: str, access_key: str):
        """Pobiera z Tuya Cloud opisy pól (dp_schema) dla przekazanego słownika
        urządzeń (kluczowanego nazwą) i nadpisuje w miejscu pole "dp_schema" -
        wspólne dla urządzeń lokalnych/Zigbee (tuya.devices) i chmurowych
        (tuya.cloud_devices), bo obie sekcje przechowują dp_schema identycznie."""
        device_ids = {name: cfg["id"] for name, cfg in devices.items() if cfg.get("id")}
        if not device_ids:
            return [], []
        schemas, _fetch_errors = await asyncio.get_event_loop().run_in_executor(
            None, fetch_dp_schemas_for_devices, api_region, access_id, access_key, list(device_ids.values()),
        )
        updated, failed = [], []
        for name, dev_id in device_ids.items():
            if dev_id in schemas and schemas[dev_id]:
                devices[name]["dp_schema"] = schemas[dev_id]
                updated.append(name)
            else:
                failed.append(name)
        return updated, failed

    @app.route("/api/plugins/tuya/devices/refresh-dp-schema", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_refresh_dp_schema():
        cloud = _get_cloud_section()
        api_region = cloud.get("api_region", "")
        access_id = cloud.get("access_id", "")
        access_key = cloud.get("access_key", "")
        if not api_region or not access_id or not access_key:
            return jsonify({
                "error": "Najpierw zapisz poświadczenia Tuya Cloud wyżej (region, Access ID, Access Secret).",
            }), 400

        tuya = get_config_raw().get("tuya", {})
        local_devices = {name: dict(cfg) for name, cfg in tuya.get("devices", {}).items()}
        cloud_devices = {name: dict(cfg) for name, cfg in tuya.get("cloud_devices", {}).items()}
        if not local_devices and not cloud_devices:
            return jsonify({"error": "Brak skonfigurowanych urządzeń z ID do opisania"}), 400

        try:
            local_updated, local_failed = await _refresh_dp_schema(local_devices, api_region, access_id, access_key)
            cloud_updated, cloud_failed = await _refresh_dp_schema(cloud_devices, api_region, access_id, access_key)
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd komunikacji z Tuya Cloud: {exc}"}), 502

        tuya_devices = get_config_store().get("tuya_devices")
        tuya_devices["devices"] = local_devices
        tuya_devices["cloud_devices"] = cloud_devices
        get_config_store().set("tuya_devices", tuya_devices)
        reload_bridge_config()
        enqueue_bridge_reload()

        updated, failed = local_updated + cloud_updated, local_failed + cloud_failed
        return jsonify({
            "updated": updated, "failed": failed,
            "message": f"Zaktualizowano opisy pól dla {len(updated)}/{len(updated) + len(failed)} urządzeń.",
        })

    @app.route("/api/plugins/tuya_cloud/devices/rescan", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_rescan():
        cloud = _get_cloud_section()
        api_region = cloud.get("api_region", "")
        access_id = cloud.get("access_id", "")
        access_key = cloud.get("access_key", "")
        if not api_region or not access_id or not access_key:
            return jsonify({
                "error": "Najpierw zapisz poświadczenia Tuya Cloud wyżej (region, Access ID, Access Secret).",
            }), 400

        cloud_devices = get_config_raw().get("tuya", {}).get("cloud_devices", {})
        by_id = {cfg["id"]: name for name, cfg in cloud_devices.items() if cfg.get("id")}
        if not by_id:
            return jsonify({"error": "Brak skonfigurowanych urządzeń chmurowych do przeskanowania"}), 400
        scan_networks = get_config_raw().get("tuya", {}).get("scan_networks") or []

        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, fetch_tuya_cloud_devices, api_region, access_id, access_key, scan_networks,
            )
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd komunikacji z Tuya Cloud: {exc}"}), 502

        results = []
        for d in devices:
            name = by_id.get(d.get("id", ""))
            if not name:
                continue
            results.append({
                "name": name,
                "id": d.get("id", ""),
                "ip": d.get("ip", ""),
                "local_key": d.get("local_key", ""),
                "version": d.get("version", ""),
                "dp_schema": d.get("dp_schema", {}),
                "can_move_to_local": bool(d.get("ip") and d.get("local_key")),
            })
        movable = sum(1 for r in results if r["can_move_to_local"])
        return jsonify({
            "devices": results,
            "message": (
                f"Przeskanowano {len(results)}/{len(by_id)} urządzeń chmurowych - "
                f"{movable} może przejść na sterowanie lokalne."
            ) if results else "Żadne ze skonfigurowanych urządzeń chmurowych nie zostało odnalezione w Tuya Cloud.",
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
