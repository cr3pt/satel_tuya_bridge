# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import time
from datetime import datetime

from quart import Quart, jsonify, render_template, request

from bridge import energy_cycles, energy_meters, tariffs, wmbus
from dashboard.charts_config import validate_charts
from plugins import PluginMeta

MAX_RANGE_SECONDS = 366 * 86400
CLEAR_CONFIRMATION = "CZYSC"
MAX_MONTHS = 24
START_DATE_PREFIX = "energy_start_date:"


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

    def _get_wmbus_meters() -> list[dict]:
        return (get_config_store().get("config").get("wmbus", {}) or {}).get("meters", []) or []

    def _find_meter(name_or_serial: str) -> dict | None:
        key = (name_or_serial or "").strip()
        if not key:
            return None
        serial_norm = wmbus.normalize_serial(key)
        for m in _get_energy().get("meters", []) or []:
            if m.get("name") == key or (serial_norm and wmbus.normalize_serial(m.get("serial") or m.get("id")) == serial_norm):
                return m
        return None

    def _get_start_date(history, meter_name: str) -> str | None:
        return history.get_status_value(f"{START_DATE_PREFIX}{meter_name}")

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
                "charts": energy.get("charts", []),
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
        body = await request.get_json(silent=True)
        clean, error = tariffs.validate_energy_config(body)
        if error:
            return jsonify({"error": error}), 400
        current = get_config_store().get("config")
        old_energy = current.get("energy", {}) or {}
        if isinstance(body, dict) and "charts" in body:
            charts, chart_err = validate_charts(body.get("charts"))
            if chart_err:
                return jsonify({"error": chart_err}), 400
            clean["charts"] = charts
        elif "charts" in old_energy:
            clean["charts"] = old_energy["charts"]
        current["energy"] = clean
        get_config_store().set("config", current)
        energy_meters.sync_energy_sensors(get_history(), clean)
        reload_bridge_config()
        enqueue_bridge_reload()
        return jsonify({"message": "Zapisano.", "meters": len(clean["meters"]), "tariffs": len(clean["tariffs"])})

    @app.route("/api/plugins/energy_cost/available_meters", methods=["GET"])
    async def api_plugin_energy_cost_available_meters():
        """Liczniki wM-Bus filtrowane po numerze seryjnym wraz z dostępnymi polami do wyboru."""
        history = get_history()
        meters = _get_wmbus_meters()
        serial_filter = wmbus.normalize_serial(request.args.get("serial"))
        out = []
        for m in meters:
            serial = wmbus.normalize_serial(m.get("id"))
            if serial_filter and serial != serial_filter:
                continue
            out.append({
                "serial": m.get("id", ""),
                "name": m.get("name", ""),
                "label": m.get("label") or m.get("name", ""),
                "fields": wmbus.meter_fields_for_serial(history, meters, serial),
            })
        return jsonify({"meters": out})

    @app.route("/api/plugins/energy_cost/charts", methods=["GET"])
    async def api_plugin_energy_cost_charts_get():
        return jsonify({"charts": _get_energy().get("charts", []) or []})

    @app.route("/api/plugins/energy_cost/charts", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_energy_cost_charts_post():
        body = await request.get_json(silent=True)
        raw_charts = body.get("charts") if isinstance(body, dict) else body
        charts, error = validate_charts(raw_charts)
        if error:
            return jsonify({"error": error}), 400
        current = get_config_store().get("config")
        energy = dict(current.get("energy", {}) or {})
        energy["charts"] = charts
        current["energy"] = energy
        get_config_store().set("config", current)
        return jsonify({"charts": charts, "message": "Zapisano wykresy kosztów."})

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

    @app.route("/api/plugins/energy_cost/clear_all", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_energy_cost_clear_all():
        """Pełne czyszczenie wszystkiego co związane z energią i licznikami (confirm='CZYSC')."""
        body = await request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("confirm") != CLEAR_CONFIRMATION:
            return jsonify({"error": f"Aby wyczyścić, wpisz {CLEAR_CONFIRMATION}"}), 400
        history = get_history()
        counts = history.clear_all_energy_data()
        if body.get("clear_config"):
            current = get_config_store().get("config")
            if isinstance(current.get("wmbus"), dict):
                current["wmbus"]["meters"] = []
            if isinstance(current.get("energy"), dict):
                current["energy"]["meters"] = []
            get_config_store().set("config", current)
            reload_bridge_config()
            enqueue_bridge_reload()
        history.write_event("system", source="dashboard",
                            detail={"message": "Wyczyszczono wszystkie dane energii i liczników", "counts": counts})
        return jsonify({"cleared": counts, "message": "Wyczyszczono wszystkie dane energii i liczników."})

    @app.route("/api/plugins/energy_cost/meter_start", methods=["GET"])
    async def api_plugin_energy_cost_meter_start_get():
        """Rejestry licznika z zapisanym START LICZNIKA, datą startową i najnowszym stanem wraz z czasem odczytu."""
        meter = _find_meter(request.args.get("meter", "") or request.args.get("serial", ""))
        if meter is None:
            return jsonify({"error": "Nie znaleziono licznika"}), 404
        history = get_history()
        start = tariffs.load_meter_start(history, meter["name"])
        start_date = _get_start_date(history, meter["name"])
        scale = float(meter.get("scale", 1) or 1)
        registers = []
        for zone, sensor in tariffs.meter_registers(meter):
            row = history.get_latest_energy_reading(sensor)
            registers.append({
                "key": tariffs.START_SINGLE_KEY if zone is None else zone,
                "zone": zone,
                "sensor": sensor,
                "start": start.get(zone),
                "latest": round(row["v"] * scale, 3) if row else None,
                "latest_ts": row["t"] if row else None,
            })
        return jsonify({"meter": meter["name"], "start_date": start_date, "registers": registers})

    @app.route("/api/plugins/energy_cost/meter_start", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_energy_cost_meter_start_post():
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400
        meter = _find_meter(str(body.get("meter") or body.get("serial") or ""))
        if meter is None:
            return jsonify({"error": "Nie znaleziono licznika"}), 404
        history = get_history()
        start_date = body.get("start_date")
        if start_date is not None:
            raw_date = str(start_date).strip()
            if raw_date:
                try:
                    energy_cycles.parse_start_date_ts(raw_date)
                except ValueError:
                    return jsonify({"error": "Nieprawidłowa data startowa (oczekiwano RRRR-MM-DD)"}), 400
                history.set_status(f"{START_DATE_PREFIX}{meter['name']}", "energy_start", raw_date, label=meter["name"])
            else:
                history.delete_status(f"{START_DATE_PREFIX}{meter['name']}")
                raw_date = None
        else:
            raw_date = _get_start_date(history, meter["name"])
        try:
            saved = tariffs.save_meter_start(history, meter, body.get("start"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"start": saved, "start_date": raw_date, "message": "Zapisano START LICZNIKA."})

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
        sensors = sorted(set(get_history().get_sensors()) | set(get_history().get_energy_sensors()))
        flagged = {m["sensor"] for m in energy_meters.list_energy_meter_sensors(get_history())}
        by_name = [s for s in sensors if "kwh" in s.lower() or "energy" in s.lower()]
        suggested = [s for s in sensors if s in flagged] + [s for s in by_name if s not in flagged]
        return jsonify({"sensors": sensors, "suggested": suggested})

    @app.route("/api/plugins/energy_cost/flagged_sensors", methods=["GET"])
    async def api_plugin_energy_cost_flagged_sensors():
        used = {m.get("sensor") for m in _get_energy().get("meters", []) or []}
        flagged = energy_meters.list_energy_meter_sensors(get_history())
        return jsonify({"flagged": [f for f in flagged if f["sensor"] not in used]})

    @app.route("/api/plugins/energy_cost/cycles", methods=["GET"])
    async def api_plugin_energy_cost_cycles():
        """Zapisane cykle zużycia (dzienne/tygodniowe/miesięczne) do szybkich zestawień i porównań."""
        meter_id = request.args.get("meter") or request.args.get("serial") or None
        cycle_type = request.args.get("cycle_type") or None
        try:
            limit = max(1, min(int(request.args.get("limit", 200)), 1000))
        except ValueError:
            return jsonify({"error": "limit: wymagana liczba"}), 400
        rows = get_history().get_energy_cycles(meter_id=meter_id, cycle_type=cycle_type, limit=limit)
        return jsonify({"cycles": rows})

    def _readings_for(meter: dict, ts_from: float, ts_to: float, pad: float):
        zone_sensors = meter.get("zone_sensors") or {}
        if zone_sensors:
            return None, {zone: get_history().get_energy_history(sensor, ts_from - pad, ts_to + pad)
                          for zone, sensor in zone_sensors.items()}
        return get_history().get_energy_history(meter["sensor"], ts_from - pad, ts_to + pad), None

    def _latest_reading_ts(history, meter: dict) -> float | None:
        timestamps = []
        for _zone, sensor in tariffs.meter_registers(meter):
            row = history.get_latest_energy_reading(sensor)
            if row and row.get("t"):
                timestamps.append(row["t"])
        return max(timestamps) if timestamps else None

    @app.route("/api/plugins/energy_cost/monthly", methods=["GET"])
    async def api_plugin_energy_cost_monthly():
        """Miesięczne okresy rozliczeniowe (ostatnie N miesięcy, najnowszy pierwszy)."""
        energy = _get_energy()
        meter = _find_meter(request.args.get("meter", "") or request.args.get("serial", ""))
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
        name = request.args.get("meter", "") or request.args.get("serial", "")
        meter = _find_meter(name)
        if meter is None:
            return jsonify({"error": f"Nie znaleziono licznika '{name}'"}), 404

        history = get_history()
        reading_ts = _latest_reading_ts(history, meter)
        start_date = _get_start_date(history, meter["name"])

        if request.args.get("period") == tariffs.METER_STATE_PERIOD:
            latest = {zone: history.get_latest_energy_reading(sensor) for zone, sensor in tariffs.meter_registers(meter)}
            try:
                report = tariffs.build_meter_state_report(
                    energy, meter, latest, tariffs.load_meter_start(history, meter["name"]))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            report["reading_ts"] = reading_ts
            report["start_date"] = start_date
            return jsonify(report)

        now = time.time()
        period = request.args.get("period")
        try:
            if period:
                ts_from, ts_to = tariffs.resolve_period(period, now)
                ts_from = energy_cycles.effective_from_ts(ts_from, start_date)
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
            report = tariffs.build_report(energy, meter, readings, ts_from, ts_to, zone_readings=zone_readings)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        report["reading_ts"] = reading_ts
        report["start_date"] = start_date
        if meter.get("tariff") == "G12w":
            today_local = datetime.fromtimestamp(now, energy_cycles.TZ).date()
            report["g12w_free_day_today"] = energy_cycles.is_g12w_daytime_disabled(today_local)

        meter_key = str(meter.get("serial") or meter.get("id") or meter["name"])
        zones_list, vat = energy_cycles.cycle_inputs(report, float(energy.get("vat_percent", 23)))
        if zones_list:
            if period == "today":
                energy_cycles.persist_cycle_snapshot(
                    history, meter_key, "daily", energy_cycles.day_period_key(now), zones_list, vat, reading_ts,
                )
            elif period == "7d":
                energy_cycles.persist_cycle_snapshot(
                    history, meter_key, "weekly", energy_cycles.week_period_key(now), zones_list, vat, reading_ts,
                )
            elif period == "this_month":
                energy_cycles.persist_cycle_snapshot(
                    history, meter_key, "monthly", energy_cycles.month_period_key(now), zones_list, vat, reading_ts,
                )
            elif period and period.startswith(tariffs.MONTH_PERIOD_PREFIX):
                energy_cycles.persist_cycle_snapshot(
                    history, meter_key, "monthly", period[len(tariffs.MONTH_PERIOD_PREFIX):], zones_list, vat, reading_ts,
                )
        return jsonify(report)

    return META
