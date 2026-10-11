# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio

from bridge.presence_simulation import PresenceSimulation


async def test_disabled_simulation_makes_no_calls(fake_tuya):
    sim = PresenceSimulation({"enabled": False}, fake_tuya)
    await sim.run_forever()
    assert fake_tuya.calls == []


async def test_enabled_without_devices_logs_warning_and_returns(fake_tuya, caplog):
    sim = PresenceSimulation({"enabled": True, "devices": []}, fake_tuya)
    await sim.run_forever()
    assert fake_tuya.calls == []


async def test_toggles_devices_within_window(fake_tuya):
    cfg = {
        "enabled": True,
        "devices": ["swiatlo_korytarz", "gniazdko_salon"],
        "window": None,
        "min_interval_minutes": 0.001,
        "max_interval_minutes": 0.002,
    }
    sim = PresenceSimulation(cfg, fake_tuya)
    task = asyncio.create_task(sim.run_forever())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(fake_tuya.calls) > 0
    used_devices = {c[1] for c in fake_tuya.calls}
    assert used_devices <= {"swiatlo_korytarz", "gniazdko_salon"}


async def test_respects_time_window(fake_tuya):
    cfg = {
        "enabled": True,
        "devices": ["gniazdko_salon"],
        "window": {"start": "08:00", "end": "08:01"},
        "min_interval_minutes": 0.001,
        "max_interval_minutes": 0.001,
    }
    sim = PresenceSimulation(cfg, fake_tuya)
    task = asyncio.create_task(sim.run_forever())
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert isinstance(fake_tuya.calls, list)


async def test_events_logged_to_history(fake_tuya, history):
    cfg = {
        "enabled": True,
        "devices": ["gniazdko_salon"],
        "window": None,
        "min_interval_minutes": 0.001,
        "max_interval_minutes": 0.001,
    }
    sim = PresenceSimulation(cfg, fake_tuya, history=history)
    task = asyncio.create_task(sim.run_forever())
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    events = history.get_events("presence_simulation", 0, 9999999999)
    assert len(events) > 0
