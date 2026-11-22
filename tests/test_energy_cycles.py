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
