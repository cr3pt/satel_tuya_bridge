# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import time

from bridge.alarms import AlarmEngine


RULES = [
    {"name": "Zbyt wysoka temp", "sensor": "kociol_temp", "condition": "above", "threshold": 85, "hysteresis": 2},
    {"name": "Zbyt niska temp", "sensor": "kociol_cwu", "condition": "below", "threshold": 35, "hysteresis": 2},
    {"name": "Brak danych", "sensor": "kociol_temp", "condition": "stale", "max_age_seconds": 0.2},
]


def test_threshold_above_opens_alert(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", 90)
    assert len(history.get_active_alerts()) == 1


def test_threshold_below_hysteresis_does_not_reopen(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", 90)
    engine.on_value("kociol_temp", 84)
    assert len(history.get_active_alerts()) == 1


def test_threshold_recovery_beyond_hysteresis_closes_alert(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", 90)
    engine.on_value("kociol_temp", 80)
    assert history.get_active_alerts() == []


def test_below_condition(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_cwu", 20)
    assert len(history.get_active_alerts()) == 1


def test_value_within_normal_range_no_alert(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", 50)
    assert history.get_active_alerts() == []


def test_text_value_does_not_crash(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", "błąd")
    assert history.get_active_alerts() == []


async def test_stale_sensor_triggers_alert(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", 50)
    import asyncio
    await asyncio.sleep(0.25)
    await engine.check_stale_sensors()
    active = history.get_active_alerts()
    assert any(a["rule_name"] == "Brak danych" for a in active)


async def test_stale_sensor_recovers_when_data_resumes(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", 50)
    import asyncio
    await asyncio.sleep(0.25)
    await engine.check_stale_sensors()
    assert len(history.get_active_alerts()) == 1

    engine.on_value("kociol_temp", 51)
    await engine.check_stale_sensors()
    assert history.get_active_alerts() == []


def test_alert_repeated_breach_does_not_duplicate(history):
    engine = AlarmEngine(RULES, history)
    engine.on_value("kociol_temp", 90)
    engine.on_value("kociol_temp", 95)
    engine.on_value("kociol_temp", 92)
    assert len(history.get_active_alerts()) == 1


async def test_never_seen_sensor_is_not_stale_right_after_start(history):
    engine = AlarmEngine([{"name": "Cisza", "sensor": "s", "condition": "stale", "max_age_seconds": 60}], history)
    await engine.check_stale_sensors()
    assert history.get_active_alerts() == []


async def test_restart_adopts_open_alert_instead_of_opening_new(history):
    rules = [{"name": "Cisza", "sensor": "s", "condition": "stale", "max_age_seconds": 0.1}]
    import asyncio
    first = AlarmEngine(rules, history)
    await asyncio.sleep(0.15)
    await first.check_stale_sensors()
    assert len(history.get_active_alerts()) == 1

    second = AlarmEngine(rules, history)
    await asyncio.sleep(0.15)
    await second.check_stale_sensors()
    assert len(history.get_active_alerts()) == 1

    second.on_value("s", 1)
    await second.check_stale_sensors()
    assert history.get_active_alerts() == []


def test_duplicate_open_alerts_are_collapsed_to_oldest(history):
    old = history.open_alert("Cisza", "s", {})
    history.open_alert("Cisza", "s", {})
    AlarmEngine([{"name": "Cisza", "sensor": "s", "condition": "stale", "max_age_seconds": 60}], history)
    assert [a["id"] for a in history.get_active_alerts()] == [old]


def test_rule_added_later_adopts_its_open_alert(history):
    existing = history.open_alert("Brak telegramów wM-Bus: gama350", "wmbus_gama350", {})
    engine = AlarmEngine([], history)
    engine.add_rule({"name": "Brak telegramów wM-Bus: gama350", "sensor": "wmbus_gama350",
                     "condition": "stale", "max_age_seconds": 60})
    engine.on_value("wmbus_gama350", "telegram")
    import asyncio
    asyncio.run(engine.check_stale_sensors())
    assert all(a["id"] != existing for a in history.get_active_alerts())


def test_orphaned_alerts_of_removed_rules_are_resolved(history):
    history.open_alert("Brak telegramów wM-Bus: dom", "wmbus_liczni", {})
    kept = history.open_alert("Zbyt wysoka temp", "kociol_temp", {})
    engine = AlarmEngine(RULES, history)
    assert engine.resolve_orphaned_alerts() == 1
    assert [a["id"] for a in history.get_active_alerts()] == [kept]
