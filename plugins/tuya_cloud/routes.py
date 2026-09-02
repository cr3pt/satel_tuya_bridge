from __future__ import annotations

from quart import Quart, jsonify, render_template, request

from bridge.tuya_local import TUYA_CLOUD_REGIONS, fetch_cloud_devices as fetch_tuya_cloud_devices
from dashboard.setup import build_tuya_devices_dict, cloud_devices_to_rows, validate_cloud_devices
from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="tuya_cloud",
    name="Tuya Cloud (API)",
    icon="☁",
    description="Poświadczenia Tuya Cloud API, urządzenia sterowane przez chmurę i import kamer.",
    config_url="/plugins/tuya_cloud",
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
            devices = fetch_tuya_cloud_devices(api_region, access_id, access_key)
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd importu z Tuya Cloud: {exc}"}), 502
        return jsonify({"devices": devices})

    register_settings_route(
        app, "tuya_cloud", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_cloud_section, save_section=_save_cloud_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
