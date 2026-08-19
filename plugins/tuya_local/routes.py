"""Wtyczka Tuya lokalne (LAN) - własna strona konfiguracji (urządzenia +
bramki Zigbee), skan sieci LAN i import z Tuya Cloud, wydzielone z
dashboard/app.py (patrz zakładka 🧩 Wtyczki -> "⚙ Konfiguruj"). Logika
zapisu/walidacji zostaje w dashboard/setup.py (współdzielona z kreatorem
/setup), ten moduł tylko routuje i wywołuje - patrz plugins/satel/routes.py
po ten sam wzorzec."""
from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from bridge.tuya_local import scan_network as scan_tuya_network, fetch_cloud_devices as fetch_tuya_cloud_devices
from dashboard.setup import (
    build_tuya_devices_dict, validate_tuya_devices, tuya_devices_to_rows, zigbee_gateways_to_rows,
)
from plugins import PluginMeta

META = PluginMeta(
    id="tuya_local",
    name="Urządzenia Tuya (LAN)",
    icon="🔌",
    description="Urządzenia Tuya lokalne + bramki Zigbee, skan sieci LAN i import z Tuya Cloud.",
    config_url="/plugins/tuya",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:
    """Rejestruje routing wtyczki Tuya. `_ignored` połyka zależności
    przeznaczone dla INNYCH wtyczek - register_all() w plugins/__init__.py
    woła każdą wtyczkę z tym samym, wspólnym zestawem `deps`."""

    @app.route("/plugins/tuya")
    def tuya_plugin_page():
        return render_template("tuya_local/index.html")

    @app.route("/api/plugins/tuya/devices", methods=["GET"])
    def api_plugin_tuya_devices_get():
        tuya = get_config_raw().get("tuya", {})
        return jsonify({
            "devices": tuya_devices_to_rows(tuya.get("devices", {})),
            "zigbee_gateways": zigbee_gateways_to_rows(tuya.get("zigbee_gateways", {})),
        })

    @app.route("/api/plugins/tuya/devices", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    def api_plugin_tuya_devices_post():
        form = request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_tuya_devices(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        tuya_devices_dict = build_tuya_devices_dict(form)
        get_config_store().set("tuya_devices", tuya_devices_dict)

        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": len(tuya_devices_dict["devices"]),
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/tuya/scan", methods=["POST"])
    def api_plugin_tuya_scan():
        """Publiczny (jak /api/discover/tuya w kreatorze) - tylko skan
        broadcastu UDP w sieci LAN, nic nie zmienia. NIE zwraca local_key
        (nie jest rozgłaszany ze względów bezpieczeństwa) - trzeba go
        osobno wyciągnąć narzędziem `python -m tinytuya wizard` (patrz
        README) albo importem z Tuya Cloud (przycisk niżej)."""
        try:
            devices = scan_tuya_network()
        except Exception as exc:
            return jsonify({"error": f"Błąd skanowania: {exc}"}), 502
        return jsonify({"devices": devices})

    @app.route("/api/plugins/tuya/cloud-import", methods=["POST"])
    def api_plugin_tuya_cloud_import():
        """Publiczny - poświadczenia Tuya Cloud przychodzą w ciele żądania
        i NIE są nigdzie zapisywane przez ten endpoint (o zapis dba osobny
        krok - "Zapisz urządzenia" wyżej)."""
        payload = request.get_json(silent=True) or {}
        api_region = (payload.get("api_region") or "").strip()
        access_id = (payload.get("access_id") or "").strip()
        access_key = (payload.get("access_key") or "").strip()
        if not api_region or not access_id or not access_key:
            return jsonify({"error": "Region, Access ID i Access Secret są wymagane"}), 400

        try:
            devices = fetch_tuya_cloud_devices(api_region, access_id, access_key)
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd importu z Tuya Cloud: {exc}"}), 502
        return jsonify({"devices": devices})

    return META
