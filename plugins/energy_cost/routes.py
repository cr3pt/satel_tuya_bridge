# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import time

from quart import Quart, jsonify, render_template, request

from bridge import energy_meters, tariffs
from plugins import PluginMeta

MAX_RANGE_SECONDS = 366 * 86400
CLEAR_CONFIRMATION = "CZYSC"
MAX_MONTHS = 24


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
            "periods": [tariffs.METER_STATE_PERIOD, *tariffs.PERIODS],
            "per_kwh_fees": [[k, v] for k, v in tariffs.PER_KWH_FEES.items()],
            "monthly_fees": [[k, v] for k, v in tariffs.MONTHLY_FEES.items()],
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
        energy_meters.sync_energy_sensors(get_history(), clean)
        reload_bridge_config()
        enqueue_bridge_reload()
        return jsonify({"message": "Zapisano.", "meters": len(clean["meters"]), "tariffs": len(clean["tariffs"])})

    @app.route("/api/plugins/energy_cost/clear_readings", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_energy_cost_clear_readings():
        """Czyści zapisane odczyty liczników energii - tylko po wpisaniu CZYSC."""
        body = await request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("confirm") != CLEAR_CONFIRMATION:
            return jsonify({"error": f"Aby wyczyścić, wpisz {CLEAR_CONFIRMATION}"}), 400
        history = get_history()
        deleted = history.clear_energy_readings()
        history.write_event("system", source="dashboard",
                            detail={"message": f"Wyczyszczono odczyty liczników energii ({deleted} wierszy)"})
        return jsonify({"deleted": deleted,
                        "message": f"Usunięto {deleted} odczytów. Nowe zapiszą się z kolejnych wiadomości MQTT."})

    def _find_meter(name: str) -> dict | None:
        return next((m for m in _get_energy().get("meters", []) if m.get("name") == name), None)

    @app.route("/api/plugins/energy_cost/meter_start", methods=["GET"])
    async def api_plugin_energy_cost_meter_start_get():
        """Rejestry licznika z zapisanym START LICZNIKA i najnowszym stanem."""
        meter = _find_meter(request.args.get("meter", ""))
        if meter is None:
            return jsonify({"error": "Nie znaleziono licznika"}), 404
        history = get_history()
        start = tariffs.load_meter_start(history, meter["name"])
        scale = float(meter.get("scale", 1) or 1)
        registers = []
        for zone, sensor in tariffs.meter_registers(meter):
            row = history.get_latest_energy_reading(sensor)
            registers.append({"key": tariffs.START_SINGLE_KEY if zone is None else zone, "zone": zone,
                              "sensor": sensor, "start": start.get(zone),
                              "latest": round(row["v"] * scale, 3) if row else None})
        return jsonify({"meter": meter["name"], "registers": registers})

    @app.route("/api/plugins/energy_cost/meter_start", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_energy_cost_meter_start_post():
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        meter = _find_meter(str(body.get("meter") or ""))
        if meter is None:
            return jsonify({"error": "Nie znaleziono licznika"}), 404
        try:
            saved = tariffs.save_meter_start(get_history(), meter, body.get("start"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"start": saved, "message": "Zapisano START LICZNIKA."})

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
        """Czujniki z historii, które mogą być licznikiem kWh (podpowiedź w formularzu).
        Jawnie oznaczone jako "licznik energii" (⚡ w ESPHome/wM-Bus) są
        priorytetowe - w odróżnieniu od dopasowania po nazwie pola nie
        zależą od tego, czy nazwa zawiera "kwh"/"energy" (np. polskie nazwy
        encji ESPHome zwykle nie zawierają)."""
        sensors = sorted(set(get_history().get_sensors()) | set(get_history().get_energy_sensors()))
        flagged = {m["sensor"] for m in energy_meters.list_energy_meter_sensors(get_history())}
        by_name = [s for s in sensors if "kwh" in s.lower() or "energy" in s.lower()]
        suggested = [s for s in sensors if s in flagged] + [s for s in by_name if s not in flagged]
        return jsonify({"sensors": sensors, "suggested": suggested})

    @app.route("/api/plugins/energy_cost/flagged_sensors", methods=["GET"])
    async def api_plugin_energy_cost_flagged_sensors():
        """Czujniki oznaczone gdzie indziej (ESPHome/wM-Bus) jako "licznik
        energii", a jeszcze NIE użyte jako licznik tutaj - do sekcji
        "gotowe liczniki" (jedno kliknięcie zamiast ręcznego wpisywania)."""
        used = {m.get("sensor") for m in _get_energy().get("meters", []) or []}
        flagged = energy_meters.list_energy_meter_sensors(get_history())
        return jsonify({"flagged": [f for f in flagged if f["sensor"] not in used]})

    def _readings_for(meter: dict, ts_from: float, ts_to: float, pad: float):
        zone_sensors = meter.get("zone_sensors") or {}
        if zone_sensors:
            return None, {zone: get_history().get_energy_history(sensor, ts_from - pad, ts_to + pad)
                          for zone, sensor in zone_sensors.items()}
        return get_history().get_energy_history(meter["sensor"], ts_from - pad, ts_to + pad), None

    @app.route("/api/plugins/energy_cost/monthly", methods=["GET"])
    async def api_plugin_energy_cost_monthly():
        """Miesięczne okresy rozliczeniowe (ostatnie N miesięcy, najnowszy pierwszy)."""
        energy = _get_energy()
        meter = _find_meter(request.args.get("meter", ""))
        if meter is None:
            return jsonify({"error": "Nie znaleziono licznika"}), 404
        try:
            count = int(request.args.get("months", 12))
        except ValueError:
            return jsonify({"error": "months: wymagana liczba"}), 400
        count = max(1, min(count, MAX_MONTHS))
        now = time.time()
        months = tariffs.recent_months(now, count)
        ts_from = tariffs.month_bounds(*months[-1])[0]
        pad = float(energy.get("max_gap_minutes", 360)) * 60
        readings, zone_readings = _readings_for(meter, ts_from, now, pad)
        try:
            rows = tariffs.monthly_report(energy, meter, readings, months, now, zone_readings=zone_readings)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"meter": meter["name"], "tariff": meter["tariff"], "months": rows})

    @app.route("/api/plugins/energy_cost/report", methods=["GET"])
    async def api_plugin_energy_cost_report():
        energy = _get_energy()
        name = request.args.get("meter", "")
        meter = next((m for m in energy.get("meters", []) if m.get("name") == name), None)
        if meter is None:
            return jsonify({"error": f"Nie znaleziono licznika '{name}'"}), 404

        if request.args.get("period") == tariffs.METER_STATE_PERIOD:
            history = get_history()
            latest = {zone: history.get_latest_energy_reading(sensor) for zone, sensor in tariffs.meter_registers(meter)}
            try:
                return jsonify(tariffs.build_meter_state_report(
                    energy, meter, latest, tariffs.load_meter_start(history, meter["name"])))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

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

        pad = float(energy.get("max_gap_minutes", 360)) * 60
        readings, zone_readings = _readings_for(meter, ts_from, ts_to, pad)
        try:
            return jsonify(tariffs.build_report(energy, meter, readings, ts_from, ts_to, zone_readings=zone_readings))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    return META
