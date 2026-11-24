# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import bisect
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
                    energy_cycles.check_start_date(raw_date, time.time())
                except ValueError as exc:
                    msg = str(exc) if "przyszło" in str(exc) else "Nieprawidłowa data startowa (oczekiwano RRRR-MM-DD)"
                    return jsonify({"error": msg}), 400
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

    def _start_ts(history, meter: dict) -> float | None:
        try:
            return energy_cycles.parse_start_date_ts(_get_start_date(history, meter["name"]))
        except ValueError:
            return None

    def _readings_for(meter: dict, ts_from: float, ts_to: float, pad: float):
        """Odczyty z zapasem `pad`; od daty startowej licznika z START LICZNIKA
        jako punktem bazowym (patrz `energy_cycles.apply_start_baseline`)."""
        history = get_history()
        start_ts = _start_ts(history, meter)
        start = tariffs.load_meter_start(history, meter["name"]) if start_ts is not None else {}
        scale = float(meter.get("scale", 1) or 1)
        zone_sensors = meter.get("zone_sensors") or {}
        if zone_sensors:
            return None, {zone: energy_cycles.apply_start_baseline(
                              history.get_energy_history(sensor, ts_from - pad, ts_to + pad),
                              start_ts, start.get(zone), scale)
                          for zone, sensor in zone_sensors.items()}
        return energy_cycles.apply_start_baseline(
            history.get_energy_history(meter["sensor"], ts_from - pad, ts_to + pad),
            start_ts, start.get(None), scale), None

    def _meter_key(meter: dict) -> str:
        return str(meter.get("serial") or meter.get("id") or meter["name"])

    def _refresh_cycles(history, energy: dict, meter: dict, cycle_type: str, count: int, now: float) -> None:
        """Przelicza i zapisuje w `energy_cycles` ostatnie `count` cykli. Okresy
        zamknięte, przeliczone już po swoim końcu (+ zapas na spóźnione odczyty),
        są pomijane - zestawienia czytają wtedy gotowe wartości z bazy."""
        pad = float(energy.get("max_gap_minutes", 360)) * 60
        key = _meter_key(meter)
        stored: dict[str, float] = {}
        for row in history.get_energy_cycles(meter_id=key, cycle_type=cycle_type, limit=count * 8):
            prev = stored.get(row["period_key"])
            stored[row["period_key"]] = row["updated_ts"] if prev is None else min(prev, row["updated_ts"])
        start_ts = _start_ts(history, meter)
        todo = []
        for period_key, ts_from, ts_to in energy_cycles.recent_periods(cycle_type, now, count):
            if start_ts is not None:
                if ts_to <= start_ts:
                    continue
                ts_from = max(ts_from, start_ts)
            full_end = energy_cycles.period_end(cycle_type, period_key)
            if full_end + pad <= now and stored.get(period_key, 0) >= full_end + pad:
                continue
            todo.append((period_key, ts_from, ts_to))
        if not todo:
            return
        readings, zone_readings = _readings_for(meter, min(t[1] for t in todo), max(t[2] for t in todo), pad)

        def window(rows, a: float, b: float):
            if rows is None:
                return None
            ts = [r["t"] for r in rows]
            return rows[bisect.bisect_left(ts, a - pad):bisect.bisect_right(ts, b + pad)]

        for period_key, ts_from, ts_to in todo:
            r = tariffs.period_cost(
                energy, meter, window(readings, ts_from, ts_to), ts_from, ts_to,
                zone_readings={z: window(rows, ts_from, ts_to) for z, rows in zone_readings.items()}
                if zone_readings is not None else None)
            if r["reading_ts"] is None and r["total_kwh"] == 0:
                continue
            energy_cycles.persist_cycle_snapshot(
                history, key, cycle_type, period_key, r["zones"], r["vat_percent"], r["reading_ts"])

    def _cycle_periods(rows: list[dict], cycle_type: str, now: float) -> list[dict]:
        """Wiersze per strefa -> jeden wpis na okres (najnowszy pierwszy)."""
        out: dict[str, dict] = {}
        for row in rows:
            p = out.setdefault(row["period_key"], {
                "period_key": row["period_key"], "kwh_by_zone": {}, "cost_gross_by_zone": {},
                "total_kwh": 0.0, "cost_net": 0.0, "cost_gross": 0.0, "reading_ts": None, "updated_ts": None,
            })
            p["kwh_by_zone"][row["zone"]] = row["kwh"]
            p["cost_gross_by_zone"][row["zone"]] = row["cost_gross"]
            p["total_kwh"] = round(p["total_kwh"] + row["kwh"], 3)
            p["cost_net"] = round(p["cost_net"] + row["cost_net"], 2)
            p["cost_gross"] = round(p["cost_gross"] + row["cost_gross"], 2)
            for f in ("reading_ts", "updated_ts"):
                if row[f] is not None and (p[f] is None or row[f] > p[f]):
                    p[f] = row[f]
        for p in out.values():
            p["complete"] = energy_cycles.period_end(cycle_type, p["period_key"]) <= now
        return sorted(out.values(), key=lambda p: p["period_key"], reverse=True)

    def _latest_reading_ts(history, meter: dict) -> float | None:
        timestamps = []
        for _zone, sensor in tariffs.meter_registers(meter):
            row = history.get_latest_energy_reading(sensor)
            if row and row.get("t"):
                timestamps.append(row["t"])
        return max(timestamps) if timestamps else None

    @app.route("/api/plugins/energy_cost/cycles", methods=["GET"])
    async def api_plugin_energy_cost_cycles():
        """Cykle zużycia (dzienne / tygodniowe ISO / miesięczne) zapisane w bazie
        do szybkich zestawień i porównań. Dla `meter` + `cycle_type` brakujące
        i bieżące okresy są najpierw przeliczane z odczytów i zapisywane."""
        history = get_history()
        energy = _get_energy()
        cycle_type = request.args.get("cycle_type") or None
        name = request.args.get("meter") or request.args.get("serial") or ""
        meter = _find_meter(name) if name else None
        if name and meter is None:
            return jsonify({"error": "Nie znaleziono licznika"}), 404
        try:
            limit = max(1, min(int(request.args.get("limit", 200)), 1000))
            count = int(request.args.get("count", 0))
        except ValueError:
            return jsonify({"error": "limit/count: wymagana liczba"}), 400
        now = time.time()
        if meter is not None and cycle_type in energy_cycles.CYCLE_TYPES and count > 0:
            count = min(count, energy_cycles.MAX_CYCLE_COUNT[cycle_type])
            try:
                _refresh_cycles(history, energy, meter, cycle_type, count, now)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            limit = max(limit, min(count * 8, 3000))
        meter_id = _meter_key(meter) if meter is not None else None
        rows = history.get_energy_cycles(meter_id=meter_id, cycle_type=cycle_type, limit=limit)
        resp = {"cycles": rows}
        if cycle_type in energy_cycles.CYCLE_TYPES:
            periods = _cycle_periods(rows, cycle_type, now)
            resp["periods"] = periods[:count] if count > 0 else periods
        return jsonify(resp)

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
            rows = tariffs.monthly_report(energy, meter, readings, months, now, zone_readings=zone_readings,
                                          start_ts=_start_ts(get_history(), meter))
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

        meter_key = _meter_key(meter)
        zones_list, vat = energy_cycles.cycle_inputs(report, float(energy.get("vat_percent", 23)))
        target = energy_cycles.cycle_target(period, now, tariffs.MONTH_PERIOD_PREFIX) if zones_list else None
        if target:
            cycle_type, period_key = target
            energy_cycles.persist_cycle_snapshot(
                history, meter_key, cycle_type, period_key, zones_list, vat, reading_ts,
            )
        return jsonify(report)

    return META
