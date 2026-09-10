from __future__ import annotations

import asyncio

from quart import Quart, jsonify, render_template, request

from bridge.tuya_local import (
    TUYA_CLOUD_REGIONS, fetch_cloud_devices as fetch_tuya_cloud_devices, fetch_dp_schemas_for_devices,
)
from dashboard.setup import build_tuya_devices_dict, cloud_devices_to_rows, validate_cloud_devices
from plugins import PluginMeta, register_settings_route

def describe_state(config_raw: dict) -> dict:
    cloud = (config_raw.get("tuya", {}) or {}).get("cloud", {}) or {}
    return {
        "enabled": cloud.get("enabled", True) and bool(cloud.get("access_id")),
        "configured": bool(cloud.get("access_id")),
        "detail": "skonfigurowana" if cloud.get("access_id") else "brak poświadczeń",
    }


META = PluginMeta(
    id="tuya_cloud",
    name="Tuya Cloud (kamery + import urządzeń)",
    icon="☁️",
    description="Detekcja ruchu z kamer Tuya oraz import urządzeń z konta Tuya Cloud - wymaga projektu "
                "na developer.tuya.com.",
    config_url="/plugins/tuya_cloud",
    toggle_route="connections_restart",
    describe_state=describe_state,
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_cloud_section() -> dict:
        return get_config_store().get("config").get("tuya", {}).get("cloud", {}) or {}

    def _save_cloud_section(section: dict) -> None:
        current = get_config_store().get("config")
        current.setdefault("tuya", {})["cloud"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/tuya_cloud")
    async def tuya_cloud_plugin_page():
        return await render_template("tuya_cloud/index.html", regions=TUYA_CLOUD_REGIONS)

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

        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, fetch_tuya_cloud_devices, api_region, access_id, access_key,
            )
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd importu z Tuya Cloud: {exc}"}), 502
        return jsonify({"devices": devices})

    @app.route("/api/plugins/tuya_cloud/devices/refresh-dp-schema", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_refresh_dp_schema():
        cloud = _get_cloud_section()
        api_region = cloud.get("api_region", "")
        access_id = cloud.get("access_id", "")
        access_key = cloud.get("access_key", "")
        if not api_region or not access_id or not access_key:
            return jsonify({
                "error": "Najpierw zapisz poświadczenia Tuya Cloud wyżej (region, Access ID, Access Secret).",
            }), 400

        devices = {name: dict(cfg) for name, cfg in get_config_raw().get("tuya", {}).get("cloud_devices", {}).items()}
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
        existing["cloud_devices"] = devices
        get_config_store().set("tuya_devices", existing)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "updated": updated, "failed": failed,
            "message": f"Zaktualizowano opisy pól dla {len(updated)}/{len(device_ids)} urządzeń.",
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

        try:
            devices = await asyncio.get_event_loop().run_in_executor(
                None, fetch_tuya_cloud_devices, api_region, access_id, access_key,
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

    @app.route("/api/plugins/tuya_cloud/devices/move-to-local", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_tuya_cloud_move_to_local():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        name = (form.get("name") or "").strip()
        ip = (form.get("ip") or "").strip()
        local_key = (form.get("local_key") or "").strip()
        if not name or not ip or not local_key:
            return jsonify({"error": "Nazwa, adres IP i local_key są wymagane"}), 400

        tuya_devices = get_config_store().get("tuya_devices")
        cloud_devices = tuya_devices.get("cloud_devices", {})
        if name not in cloud_devices:
            return jsonify({"error": f"Nieznane urządzenie chmurowe '{name}' (mogło już zostać przeniesione)"}), 404
        local_devices = tuya_devices.setdefault("devices", {})
        if name in local_devices:
            return jsonify({
                "error": f"Urządzenie lokalne o nazwie '{name}' już istnieje - zmień nazwę jednego z nich",
            }), 400

        cloud_cfg = cloud_devices.pop(name)
        local_devices[name] = {
            "id": cloud_cfg.get("id", ""),
            "ip": ip,
            "local_key": local_key,
            "type": "socket",
            "dp_schema": form.get("dp_schema") or cloud_cfg.get("dp_schema", {}),
        }
        tuya_devices["cloud_devices"] = cloud_devices
        tuya_devices["devices"] = local_devices
        get_config_store().set("tuya_devices", tuya_devices)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": f"Przeniesiono '{name}' do sterowania lokalnego (zakładka 🔌 Tuya (LAN))."})

    register_settings_route(
        app, "tuya_cloud", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_cloud_section, save_section=_save_cloud_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
