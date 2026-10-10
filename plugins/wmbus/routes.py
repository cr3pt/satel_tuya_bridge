from __future__ import annotations

import time

from quart import Quart, jsonify, render_template, request

from bridge import wmbus
from plugins import PluginMeta, register_settings_route


def describe_state(config_raw: dict) -> dict:
    wm = config_raw.get("wmbus", {}) or {}
    meters = wm.get("meters", []) or []
    return {
        "enabled": bool(wm.get("enabled")) and bool(meters),
        "configured": bool(meters),
        "detail": f"liczników: {len(meters)}" if meters else "brak liczników",
    }


META = PluginMeta(
    id="wmbus",
    name="Liczniki wM-Bus (wmbusmeters, AES, 3 fazy)",
    icon="⚡",
    description="Odczyt liczników wody/ciepła/prądu przez wmbusmeters (szyfrowanie AES-128) - historia, "
                "alarmy, osobno fazy L1/L2/L3, Home Assistant.",
    config_url="/plugins/wmbus",
    config_key="wmbus",
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store, get_config_path, get_history,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_section() -> dict:
        return get_config_store().get("config").get("wmbus", {}) or {}

    def _save_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["wmbus"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/wmbus")
    async def wmbus_plugin_page():
        return await render_template("wmbus/index.html")

    @app.route("/api/plugins/wmbus/meters", methods=["GET"])
    async def api_plugin_wmbus_meters_get():
        """Lista liczników BEZ kluczy AES (tylko has_key + zamaskowana końcówka)."""
        section = _get_section()
        return jsonify({
            "device": section.get("device", "auto"),
            "topic_prefix": section.get("topic_prefix", "wmbusmeters"),
            "mqtt_host": section.get("mqtt_host", ""),
            "meters": wmbus.public_view(section.get("meters", []) or []),
        })

    @app.route("/api/plugins/wmbus/meters", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_wmbus_meters_post():
        """Puste `key` = zachowaj dotychczasowy klucz; `clear_key: true` = usuń."""
        body = await request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("meters"), list) \
                or not all(isinstance(m, dict) for m in body["meters"]):
            return jsonify({"error": "Oczekiwano obiektu z listą 'meters'"}), 400

        section = dict(_get_section())
        device = (body.get("device") or section.get("device") or "auto").strip()
        topic_prefix = (body.get("topic_prefix") or section.get("topic_prefix") or "wmbusmeters").strip()
        mqtt_host = (body.get("mqtt_host") or "").strip()

        incoming = [{**m, "name": (m.get("name") or "").strip(), "id": str(m.get("id") or "").strip(),
                     "key": (m.get("key") or "").strip()} for m in body["meters"]]
        merged = wmbus.merge_keys(incoming, section.get("meters", []) or [])
        errors = wmbus.validate_meters(merged)
        if not wmbus.DEVICE_RE.match(device):
            errors.append("Urządzenie wmbusmeters: nieprawidłowa wartość (np. auto, im871a:t1)")
        if not wmbus.TOPIC_RE.match(topic_prefix):
            errors.append("Prefiks tematu MQTT: litery, cyfry, _ - /")
        if mqtt_host and not wmbus.HOST_RE.match(mqtt_host):
            errors.append("Host MQTT: nieprawidłowy")
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        cleaned = [wmbus.normalize_meter(m) for m in merged]
        section.update({"device": device, "topic_prefix": topic_prefix, "meters": cleaned})
        if mqtt_host:
            section["mqtt_host"] = mqtt_host
        else:
            section.pop("mqtt_host", None)
        section.setdefault("enabled", False)

        conf_dir = get_config_path().parent / "wmbusmeters_conf"
        broker = section.get("mqtt_host") or get_config_store().get("config").get("ebus", {}).get("mqtt_host") or "127.0.0.1"
        port = section.get("mqtt_port") or get_config_store().get("config").get("ebus", {}).get("mqtt_port", 1883)
        try:
            wmbus.write_wmbusmeters_files(section, broker, port, cleaned, conf_dir)
        except (ValueError, OSError) as exc:
            return jsonify({"error": f"Nie wygenerowano plików wmbusmeters: {exc}"}), 400

        _save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()
        get_history().write_event("system", source="dashboard", detail={"message": f"Zapisano {len(cleaned)} liczników wM-Bus"})
        return jsonify({
            "saved": len(cleaned),
            "conf_dir": str(conf_dir),
            "message": "Zapisano. Pliki dla wmbusmeters wygenerowano w katalogu wmbusmeters_conf obok bazy "
                       "konfiguracji (patrz README). Lista liczników zadziała w bridge w ciągu 1-2 s; włączenie "
                       "wtyczki wymaga restartu.",
        })

    @app.route("/api/plugins/wmbus/readings", methods=["GET"])
    async def api_plugin_wmbus_readings():
        return jsonify(get_history().get_status("wmbus"))

    @app.route("/api/plugins/wmbus/series", methods=["GET"])
    async def api_plugin_wmbus_series():
        """Szeregi czasowe pól licznika do wykresów (z downsamplingiem do `points`)."""
        name = request.args.get("meter", "")
        meter = next((m for m in _get_section().get("meters", []) or [] if m.get("name") == name), None)
        if meter is None:
            return jsonify({"error": f"Nie znaleziono licznika '{name}'"}), 404
        try:
            hours = min(max(float(request.args.get("hours", 24)), 0.1), 24 * 366)
            points = min(max(int(request.args.get("points", 300)), 10), 2000)
        except ValueError:
            return jsonify({"error": "Nieprawidłowe 'hours' lub 'points'"}), 400
        now = time.time()
        history = get_history()
        prefix = f"wmbus_{name}_"
        series = []
        for sensor in history.get_sensors():
            if not sensor.startswith(prefix):
                continue
            field = sensor[len(prefix):]
            info = wmbus.field_unit(field)
            data = history.get_history(sensor, now - hours * 3600, now)
            if not data:
                continue
            series.append({"field": field, "phase": wmbus.detect_phase(field),
                           "unit": info[0] if info else "", "kind": (info[1] if info else None) or "other",
                           "points": wmbus.downsample([[r["t"], r["v"]] for r in data], points)})
        return jsonify({"meter": name, "label": meter.get("label") or name, "from": now - hours * 3600,
                        "to": now, "series": series})

    register_settings_route(
        app, "wmbus", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_section, save_section=_save_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        default_enabled=False,
    )
    return META
