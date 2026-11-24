# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Testy walidacji własnych wykresów (dashboard/charts_config)."""


def test_duplicate_chart_ids_are_made_unique():
    from dashboard.charts_config import validate_charts
    charts, err = validate_charts([{"id": "a", "sensors": ["x"]}, {"id": "a", "sensors": ["y"]}])
    assert err is None
    assert len({c["id"] for c in charts}) == 2
