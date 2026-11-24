# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from datetime import datetime

from bridge import energy_cycles


class FakeHistory:
    def __init__(self):
        self.rows = []

    def upsert_energy_cycle(self, **kwargs):
        self.rows.append(kwargs)


ZONES = [
    {"zone": "dzienna", "kwh": 2.5, "net": 10.0},
    {"zone": "nocna", "kwh": 1.0, "net": 2.0},
]


def test_cycle_inputs_reads_nested_result():
    zones, vat = energy_cycles.cycle_inputs({"result": {"zones": ZONES, "vat_percent": 8}}, 23.0)
    assert zones == ZONES
    assert vat == 8.0


def test_cycle_inputs_reads_flat_report():
    zones, vat = energy_cycles.cycle_inputs({"zones": ZONES, "vat_percent": 23}, 5.0)
    assert zones == ZONES
    assert vat == 23.0


def test_cycle_inputs_uses_default_vat_only_when_missing():
    assert energy_cycles.cycle_inputs({"zones": ZONES}, 23.0)[1] == 23.0
    assert energy_cycles.cycle_inputs({"zones": ZONES, "vat_percent": 0}, 23.0)[1] == 0.0


def test_cycle_inputs_drops_invalid_zone_entries():
    zones, _ = energy_cycles.cycle_inputs({"zones": [ZONES[0], "x", {"kwh": 1}]}, 23.0)
    assert zones == [ZONES[0]]


def test_cycle_inputs_empty_report():
    assert energy_cycles.cycle_inputs({}, 23.0) == ([], 23.0)


def test_persist_cycle_snapshot_writes_gross_with_vat():
    history = FakeHistory()
    energy_cycles.persist_cycle_snapshot(history, "12345678", "daily", "2026-11-22", ZONES, 23.0, 1700000000.0)
    assert len(history.rows) == 2
    first = history.rows[0]
    assert first["meter_id"] == "12345678"
    assert first["cycle_type"] == "daily"
    assert first["period_key"] == "2026-11-22"
    assert first["zone"] == "dzienna"
    assert first["cost_net"] == 10.0
    assert first["cost_gross"] == 12.3
    assert first["reading_ts"] == 1700000000.0


def test_period_keys_use_warsaw_time():
    ts = datetime(2026, 11, 22, 12, 0, tzinfo=energy_cycles.TZ).timestamp()
    assert energy_cycles.day_period_key(ts) == "2026-11-22"
    assert energy_cycles.week_period_key(ts) == "2026-W47"
    assert energy_cycles.month_period_key(ts) == "2026-11"


def test_effective_from_ts_clamps_to_start_date():
    start_ts = energy_cycles.parse_start_date_ts("2026-11-10")
    assert energy_cycles.effective_from_ts(start_ts - 86400, "2026-11-10") == start_ts
    assert energy_cycles.effective_from_ts(start_ts + 86400, "2026-11-10") == start_ts + 86400
    assert energy_cycles.effective_from_ts(123.0, None) == 123.0


def test_apply_start_baseline_drops_old_readings_and_adds_start_point():
    start_ts = energy_cycles.parse_start_date_ts("2026-11-10")
    rows = [{"t": start_ts - 3600, "v": 9000.0}, {"t": start_ts + 5 * 86400, "v": 120.0}]
    out = energy_cycles.apply_start_baseline(rows, start_ts, 100.0)
    assert out == [{"t": start_ts, "v": 100.0}, {"t": start_ts + 5 * 86400, "v": 120.0}]


def test_apply_start_baseline_respects_scale_and_missing_values():
    start_ts = energy_cycles.parse_start_date_ts("2026-11-10")
    rows = [{"t": start_ts + 60, "v": 5.0}]
    assert energy_cycles.apply_start_baseline(rows, start_ts, 10.0, scale=10.0)[0] == {"t": start_ts, "v": 1.0}
    assert energy_cycles.apply_start_baseline(rows, start_ts, None) == rows
    assert energy_cycles.apply_start_baseline(rows, None, 10.0) is rows
    assert energy_cycles.apply_start_baseline(None, start_ts, 10.0) is None


def test_check_start_date_rejects_future_and_bad_format():
    now = datetime(2026, 11, 24, 8, tzinfo=energy_cycles.TZ).timestamp()
    assert energy_cycles.check_start_date("2026-11-24", now) <= now
    for bad in ("2026-11-25", "24.11.2026", ""):
        try:
            energy_cycles.check_start_date(bad, now)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_recent_periods_calendar_bounds():
    now = datetime(2026, 11, 25, 12, tzinfo=energy_cycles.TZ).timestamp()  # środa
    daily = energy_cycles.recent_periods("daily", now, 2)
    assert [d[0] for d in daily] == ["2026-11-25", "2026-11-24"]
    assert daily[0][2] == now and daily[1][2] - daily[1][1] == 86400
    weekly = energy_cycles.recent_periods("weekly", now, 2)
    assert [w[0] for w in weekly] == ["2026-W48", "2026-W47"]
    assert weekly[0][1] == datetime(2026, 11, 23, tzinfo=energy_cycles.TZ).timestamp()
    monthly = energy_cycles.recent_periods("monthly", now, 3)
    assert [m[0] for m in monthly] == ["2026-11", "2026-10", "2026-09"]
    assert monthly[1][2] == datetime(2026, 11, 1, tzinfo=energy_cycles.TZ).timestamp()


def test_period_end_for_each_cycle_type():
    tz = energy_cycles.TZ
    assert energy_cycles.period_end("daily", "2026-03-29") == datetime(2026, 3, 30, tzinfo=tz).timestamp()
    assert energy_cycles.period_end("weekly", "2026-W48") == datetime(2026, 11, 30, tzinfo=tz).timestamp()
    assert energy_cycles.period_end("monthly", "2026-12") == datetime(2027, 1, 1, tzinfo=tz).timestamp()
