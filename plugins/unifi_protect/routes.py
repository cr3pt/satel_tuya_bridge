from __future__ import annotations

import io

from quart import Quart, jsonify, render_template, request, send_file

from plugins import PluginMeta, register_settings_route

def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("unifi_protect") or {}
    configured = config_raw.get("unifi_protect") is not None
    return {
        "enabled": cfg.get("enabled", True) and configured,
        "configured": configured,
        "detail": "skonfigurowane" if configured else "nieskonfigurowane",
    }


META = PluginMeta(
    id="unifi_protect",
    name="Kamery UniFi Protect",
    icon="📹",
    description="Detekcja ruchu z kamer UniFi Protect jako alternatywa/uzupełnienie kamer Tuya.",
    config_url="/plugins/unifi_protect",
    config_key="unifi_protect",
    describe_state=describe_state,
    category="Ochrona",
)

_client_cache: dict[tuple, object] = {}


def _client_cache_key(section: dict) -> tuple:
    return (
        section.get("host"), section.get("username"), section.get("password"),
        bool(section.get("verify_ssl", False)),
    )


async def _get_client(section: dict):
    from uiprotect import ProtectApiClient

    key = _client_cache_key(section)
    client = _client_cache.get(key)
    if client is None:
        client = ProtectApiClient(
            section["host"], 443, section.get("username", ""), section.get("password", ""),
            verify_ssl=section.get("verify_ssl", False),
        )
        await client.update()
        _client_cache[key] = client
    return client


async def _call_with_retry(section: dict, action):
    client = await _get_client(section)
    try:
        return await action(client)
    except Exception:
        _client_cache.pop(_client_cache_key(section), None)
        client = await _get_client(section)
        return await action(client)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_unifi_section() -> dict:
        return get_config_store().get("config").get("unifi_protect") or {}

    def _save_unifi_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["unifi_protect"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/unifi_protect")
    async def unifi_protect_plugin_page():
        return await render_template("unifi_protect/index.html")

    @app.route("/api/plugins/unifi_protect/credentials", methods=["GET"])
    async def api_plugin_unifi_protect_credentials_get():
        section = _get_unifi_section()
        return jsonify({
            "configured": bool(section),
            "host": section.get("host", ""),
            "username": section.get("username", ""),
            "password_set": bool(section.get("password")),
            "verify_ssl": bool(section.get("verify_ssl", False)),
        })

    @app.route("/api/plugins/unifi_protect/credentials", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_unifi_protect_credentials_post():
        form = await request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        host = (form.get("host") or "").strip()
        if not host:
            return jsonify({"error": "Adres kontrolera UniFi Protect jest wymagany"}), 400

        existing = _get_unifi_section()
        section = dict(existing)
        section.update({
            "host": host,
            "username": (form.get("username") or "").strip() or existing.get("username", ""),
            "password": (form.get("password") or "").strip() or existing.get("password", ""),
            "verify_ssl": bool(form.get("verify_ssl")),
        })
        _save_unifi_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({"message": "Zapisano. Włączenie/wyłączenie integracji wciąż wymaga pełnego restartu procesu bridge'a."})

    @app.route("/api/plugins/unifi_protect/cameras", methods=["GET"])
    async def api_plugin_unifi_protect_cameras_get():
        section = _get_unifi_section()
        if not section.get("host"):
            return jsonify({"error": "Integracja UniFi Protect nie jest skonfigurowana"}), 400
        try:
            cameras = await _call_with_retry(section, lambda client: client.get_cameras())
        except Exception as exc:
            return jsonify({"error": f"Nie udało się połączyć z kontrolerem UniFi Protect: {exc}"}), 502
        return jsonify({"cameras": [{"id": c.id, "name": c.name} for c in cameras]})

    @app.route("/api/plugins/unifi_protect/cameras/<camera_id>/snapshot", methods=["GET"])
    async def api_plugin_unifi_protect_snapshot_get(camera_id):
        section = _get_unifi_section()
        if not section.get("host"):
            return jsonify({"error": "Integracja UniFi Protect nie jest skonfigurowana"}), 400
        try:
            snapshot = await _call_with_retry(section, lambda client: client.get_camera_snapshot(camera_id))
        except Exception as exc:
            return jsonify({"error": f"Nie udało się pobrać zrzutu: {exc}"}), 502
        if snapshot is None:
            return jsonify({"error": "Brak zrzutu dla tej kamery (offline?)"}), 404
        response = await send_file(io.BytesIO(snapshot), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "no-store"
        return response

    def _validate_enable() -> str | None:
        if not _get_unifi_section().get("host"):
            return "Najpierw zapisz adres kontrolera UniFi Protect niżej, zanim włączysz wtyczkę."
        return None

    register_settings_route(
        app, "unifi_protect", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_unifi_section, save_section=_save_unifi_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        validate_enable=_validate_enable,
    )

    return META
