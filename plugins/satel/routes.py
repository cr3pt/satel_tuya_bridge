"""Wtyczka Satel - własna strona konfiguracji (strefy/wyjścia/wejścia IP/
partycje) i wykrywanie aktywnych stref/wyjść, wydzielone z dashboard/app.py
(patrz zakładka 🧩 Wtyczki -> "⚙ Konfiguruj"). Logika zapisu/walidacji
pozostaje w dashboard/setup.py (współdzielona z kreatorem /setup, patrz
tamtejsze docstringi) - ten moduł tylko routuje i wywołuje."""
from __future__ import annotations

import asyncio
from flask import Flask, jsonify, render_template, request

from bridge.satel_client import discover_active_zones_and_outputs
from dashboard.setup import (
    build_satel_fields_dict, validate_satel_fields,
    dict_to_name_number_rows, zones_to_rows, virtual_inputs_to_rows,
)
from plugins import PluginMeta

META = PluginMeta(
    id="satel",
    name="Centrala Satel Integra",
    icon="🔒",
    description="Strefy/wyjścia/wejścia IP/partycje oraz wykrywanie aktywnych stref przez nasłuch.",
    config_url="/plugins/satel",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             get_config_path, reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:
    """Rejestruje routing wtyczki Satel. `_ignored` połyka zależności
    przeznaczone dla INNYCH wtyczek - register_all() w plugins/__init__.py
    woła każdą wtyczkę z tym samym, wspólnym zestawem `deps`."""

    @app.route("/plugins/satel")
    def satel_plugin_page():
        return render_template("satel/index.html")

    @app.route("/api/plugins/satel/fields", methods=["GET"])
    def api_plugin_satel_fields_get():
        satel = get_config_raw().get("satel", {})
        return jsonify({
            "partitions": dict_to_name_number_rows(satel.get("partitions", {})),
            "monitored_zones": zones_to_rows(satel.get("monitored_zones", {}), satel.get("zone_types", {})),
            "monitored_outputs": zones_to_rows(satel.get("monitored_outputs", {}), satel.get("output_types", {})),
            "virtual_inputs": virtual_inputs_to_rows(satel.get("virtual_inputs", {}).get("inputs", {})),
            "zone_types": satel.get("zone_types", {}),
            "output_types": satel.get("output_types", {}),
        })

    @app.route("/api/plugins/satel/fields", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    def api_plugin_satel_fields_post():
        form = request.get_json(silent=True)
        if not isinstance(form, dict):
            return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

        errors = validate_satel_fields(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        satel_fields_dict = build_satel_fields_dict(form)
        get_config_store().set("satel_fields", satel_fields_dict)

        reload_bridge_config()
        enqueue_bridge_reload()

        return jsonify({
            "saved": {k: len(v) if isinstance(v, dict) else v for k, v in satel_fields_dict.items()},
            "message": "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu.",
        })

    @app.route("/api/plugins/satel/scan", methods=["POST"])
    def api_plugin_satel_scan():
        """Publiczny (jak /api/discover/satel w kreatorze) - to tylko
        nasłuch, nic nie zmienia w centrali. Host/port brane domyślnie z
        JUŻ ZAPISANEJ konfiguracji (nie trzeba ich przepisywać z
        zablokowanej hasłem zakładki 🔌 Połączenia)."""
        payload = request.get_json(silent=True) or {}
        satel_cfg = get_config_raw().get("satel", {})
        host = (payload.get("host") or satel_cfg.get("host") or "").strip()
        if not host:
            return jsonify({"error": "Brak adresu IP centrali"}), 400
        try:
            port = int(payload.get("port") or satel_cfg.get("port") or 7094)
        except (TypeError, ValueError):
            return jsonify({"error": "Nieprawidłowy port"}), 400
        try:
            timeout_seconds = min(float(payload.get("timeout_seconds") or 30), 120)
        except (TypeError, ValueError):
            timeout_seconds = 30.0

        try:
            result = asyncio.run(discover_active_zones_and_outputs(host, port, timeout_seconds=timeout_seconds))
        except ImportError:
            return jsonify({"error": "Biblioteka satel_integra nie jest zainstalowana na serwerze"}), 500
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd wykrywania: {exc}"}), 502

        return jsonify(result)

    return META
