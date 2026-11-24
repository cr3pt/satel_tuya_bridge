# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from datetime import datetime

from bridge import energy_cycles

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=energy_cycles.TZ).timestamp()


def test_today_is_daily_with_date_key():
    assert energy_cycles.cycle_target("today", NOW) == ("daily", "2026-11-22")


def test_rolling_7d_is_not_stored_as_iso_week():
    assert energy_cycles.cycle_target("7d", NOW) == ("rolling_7d", "2026-11-22")
    assert energy_cycles.ROLLING_7D_CYCLE == "rolling_7d"


def test_rolling_7d_key_changes_with_end_date():
    next_day = datetime(2026, 11, 23, 12, 0, tzinfo=energy_cycles.TZ).timestamp()
    assert energy_cycles.cycle_target("7d", next_day) == ("rolling_7d", "2026-11-23")


def test_this_month_and_explicit_month_share_key():
    assert energy_cycles.cycle_target("this_month", NOW) == ("monthly", "2026-11")
    assert energy_cycles.cycle_target("month:2026-11", NOW) == ("monthly", "2026-11")
    assert energy_cycles.cycle_target("month:2026-09", NOW) == ("monthly", "2026-09")


def test_other_periods_are_not_persisted():
    for period in (None, "", "30d", "month:"):
        assert energy_cycles.cycle_target(period, NOW) is None


def test_yesterday_and_last_month_are_complete_calendar_cycles():
    assert energy_cycles.cycle_target("yesterday", NOW) == ("daily", "2026-11-21")
    assert energy_cycles.cycle_target("last_month", NOW) == ("monthly", "2026-10")
