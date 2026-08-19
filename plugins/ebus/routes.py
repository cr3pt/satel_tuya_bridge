"""Wtyczka eBUS (dane z kotła) - własna strona konfiguracji (lista
czujników) i wykrywanie tematów MQTT ebusd (krótkie oraz w tle), wydzielone
z dashboard/app.py (patrz zakładka 🧩 Wtyczki -> "⚙ Konfiguruj"). Logika
zapisu/walidacji zostaje w dashboard/setup.py, ten moduł tylko routuje i
wywołuje - patrz plugins/satel/routes.py po ten sam wzorzec."""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from bridge.ebus_client import discover_topics as discover_ebus_topics, BackgroundTopicDiscovery
from dashboard.setup import clean_sensors_payload
from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="ebus",
    name="eBUS / kocioł (dane grzewcze)",
    icon="🔥",
    description="Lista czujników kotła (ebusd) oraz wykrywanie tematów MQTT.",
    config_url="/plugins/ebus",
)

# Jedna instancja procesu-wide, tak jak w dashboard/app.py - jeden nasłuch w
# tle naraz wystarcza dla panelu administrowanego przez jedną osobę (patrz
# bridge/ebus_client.py: BackgroundTopicDiscovery). Osobna instancja od tej
# w dashboard/app.py - nasłuch uruchomiony z /config i z /plugins/ebus nie
# dzieli stanu, ale to nieszkodliwe (i tak jeden użytkownik na raz).
_background_discovery = BackgroundTopicDiscovery()


def register(app: Flask, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:
    """Rejestruje routing wtyczki eBUS. `_ignored` połyka zależności
    przeznaczone dla INNYCH wtyczek - register_all() w plugins/__init__.py
    woła każdą wtyczkę z tym samym, wspólnym zestawem `deps`."""

    @app.route("/plugins/ebus")
    def ebus_plugin_page():
        return render_template("ebus/index.html")

    @app.route("/api/plugins/ebus/sensors", methods=["GET"])
    def api_plugin_ebus_sensors_get():
        return jsonify(get_config_store().get("sensors").get("sensors", []))

    @app.route("/api/plugins/ebus/sensors", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    def api_plugin_ebus_sensors_post():
        payload = request.get_json(silent=True)
        cleaned, error = clean_sensors_payload(payload)
        if error:
            return jsonify({"error": error}), 400

        get_config_store().set("sensors", {"sensors": cleaned})
        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(cleaned),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/ebus/discover", methods=["POST"])
    def api_plugin_ebus_discover():
        """Krótkie (kilka sekund) nasłuchiwanie MQTT ebusd - publiczny,
        tylko podgląd sieci. Host/port domyślnie z JUŻ ZAPISANEJ
        konfiguracji (jak /api/discover/ebus w starej zakładce /config)."""
        payload = request.get_json(silent=True) or {}
        ebus_cfg = get_config_raw().get("ebus", {})
        mqtt_host = payload.get("mqtt_host") or ebus_cfg.get("mqtt_host")
        mqtt_port = payload.get("mqtt_port") or ebus_cfg.get("mqtt_port", 1883)
        if not mqtt_host:
            return jsonify({"error": "Brak adresu brokera MQTT"}), 400
        try:
            topics = discover_ebus_topics(mqtt_host, int(mqtt_port))
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd wykrywania: {exc}"}), 502
        return jsonify({"topics": topics})

    @app.route("/api/plugins/ebus/discover/start", methods=["POST"])
    def api_plugin_ebus_discover_start():
        payload = request.get_json(silent=True) or {}
        ebus_cfg = get_config_raw().get("ebus", {})
        mqtt_host = payload.get("mqtt_host") or ebus_cfg.get("mqtt_host")
        mqtt_port = payload.get("mqtt_port") or ebus_cfg.get("mqtt_port", 1883)
        if not mqtt_host:
            return jsonify({"error": "Brak adresu brokera MQTT"}), 400
        try:
            duration_seconds = min(float(payload.get("duration_seconds") or 300), 600)
        except (TypeError, ValueError):
            duration_seconds = 300.0

        try:
            _background_discovery.start(mqtt_host, int(mqtt_port), duration_seconds=duration_seconds)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd uruchamiania nasłuchu: {exc}"}), 502

        return jsonify(_background_discovery.status())

    @app.route("/api/plugins/ebus/discover/status")
    def api_plugin_ebus_discover_status():
        return jsonify(_background_discovery.status())

    @app.route("/api/plugins/ebus/discover/stop", methods=["POST"])
    def api_plugin_ebus_discover_stop():
        return jsonify(_background_discovery.stop())

    def _get_ebus_section() -> dict:
        # STORED sekcja z dokumentu "config" - lista czujników żyje osobno
        # w dokumencie "sensors" (patrz get_config_raw()["ebus"]["sensors"]
        # wyżej, scalane dopiero przez BridgeConfig.load - to samo
        # zastrzeżenie co w plugins/satel/routes.py).
        return get_config_store().get("config").get("ebus", {})

    def _save_ebus_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["ebus"] = section
        get_config_store().set("config", current)

    register_settings_route(
        app, "ebus", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_ebus_section, save_section=_save_ebus_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
