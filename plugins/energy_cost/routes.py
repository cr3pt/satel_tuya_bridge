from __future__ import annotations

import time

from quart import Quart, jsonify, render_template, request

from bridge import tariffs
from plugins import PluginMeta

MAX_RANGE_SECONDS = 366 * 86400


def describe_state(config_raw: dict) -> dict:
    energy = config_raw.get("energy", {}) or {}
    meters = energy.get("meters", []) or []
    return {
        "enabled": bool(meters),
        "configured": bool(meters),
        "detail": f"liczników: {len(meters)}" if meters else "brak liczników",
    }


META = PluginMeta(
    id="energy_cost",
    name="Koszty energii (taryfy G11/G12/G12w/G13)",
    icon="💰",
    description="Koszt zużycia prądu wg taryfy (strefy, święta, opłaty stałe, VAT) liczony z historii "
                "narastającego licznika kWh, plus porównanie taryf.",
    config_url="/plugins/energy_cost",
    config_key="energy",
    toggleable=False,
    restart_required=False,
    has_log_level=False,
    describe_state=describe_state,
    category="Dom",
)


def register(app: Quart, *, require_auth, get_auth_guard, get_config_store, get_history,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:

    def _get_energy() -> dict:
        return get_config_store().get("config").get("energy", {}) or {}

    @app.route("/plugins/energy_cost")
    async def energy_cost_plugin_page():
        return await render_template("energy_cost/index.html")

    @app.route("/api/plugins/energy_cost/config", methods=["GET"])
    async def api_plugin_energy_cost_config_get():
        energy = _get_energy()
        return jsonify({
            "config": {
                "vat_percent": energy.get("vat_percent", 23),
                "max_gap_minutes": energy.get("max_gap_minutes", 360),
                "custom_tariffs": energy.get("custom_tariffs", {}),
                "tariffs": energy.get("tariffs", {}),
                "meters": energy.get("meters", []),
            },
            "builtin": {tid: {"label": build(None).label, "zones": list(build(None).zones)}
                        for tid, build in tariffs.BUILTIN_TARIFFS.items()},
            "g12_variants": list(tariffs.G12_VARIANT_IDS),
            "periods": list(tariffs.PERIODS),
        })

    @app.route("/api/plugins/energy_cost/config", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_energy_cost_config_post():
        clean, error = tariffs.validate_energy_config(await request.get_json(silent=True))
        if error:
            return jsonify({"error": error}), 400
        current = get_config_store().get("config")
        current["energy"] = clean
        get_config_store().set("config", current)
        reload_bridge_config()
        enqueue_bridge_reload()
        return jsonify({"message": "Zapisano.", "meters": len(clean["meters"]), "tariffs": len(clean["tariffs"])})

    @app.route("/api/plugins/energy_cost/schedule", methods=["GET"])
    async def api_plugin_energy_cost_schedule():
        """Godziny stref wybranej taryfy - do porównania z taryfą operatora."""
        tid = request.args.get("tariff", "")
        energy = _get_energy()
        try:
            tariff = tariffs.resolve_tariff(tid, request.args.get("variant") or None, energy.get("custom_tariffs"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"tariff": tariff.id, "zones": list(tariff.zones),
                        "schedule": tariffs.describe_schedule(tariff)})

    @app.route("/api/plugins/energy_cost/sensors", methods=["GET"])
    async def api_plugin_energy_cost_sensors():
        """Czujniki z historii, które mogą być licznikiem kWh (podpowiedź w formularzu)."""
        sensors = get_history().get_sensors()
        return jsonify({"sensors": sensors,
                        "suggested": [s for s in sensors if "kwh" in s.lower() or "energy" in s.lower()]})

    @app.route("/api/plugins/energy_cost/report", methods=["GET"])
    async def api_plugin_energy_cost_report():
        energy = _get_energy()
        name = request.args.get("meter", "")
        meter = next((m for m in energy.get("meters", []) if m.get("name") == name), None)
        if meter is None:
            return jsonify({"error": f"Nie znaleziono licznika '{name}'"}), 404

        now = time.time()
        try:
            period = request.args.get("period")
            if period:
                ts_from, ts_to = tariffs.resolve_period(period, now)
            else:
                ts_from = float(request.args["from"])
                ts_to = float(request.args.get("to", now))
        except KeyError:
            return jsonify({"error": "Podaj 'period' albo 'from' (unix timestamp)"}), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if ts_to <= ts_from:
            return jsonify({"error": "Koniec okresu musi być późniejszy niż początek"}), 400
        if ts_to - ts_from > MAX_RANGE_SECONDS:
            return jsonify({"error": "Maksymalny okres to 366 dni"}), 400

        # odczyty sprzed początku/po końcu okresu, żeby przyciąć skrajne odcinki
        # proporcjonalnie, zamiast gubić zużycie na granicach okresu
        pad = float(energy.get("max_gap_minutes", 360)) * 60
        readings = get_history().get_history(meter["sensor"], ts_from - pad, ts_to + pad)
        try:
            return jsonify(tariffs.build_report(energy, meter, readings, ts_from, ts_to))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    return META
