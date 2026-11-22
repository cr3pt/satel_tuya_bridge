# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio
from types import SimpleNamespace

from bridge import plugin_lifecycle as pl


class FakeHistory:
    def __init__(self):
        self.statuses = {}

    def set_status(self, key, category, value, label=None):
        self.statuses[key] = (category, value, label)


class FakeListener:
    def __init__(self, cfg):
        self.cfg = cfg
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeDevice:
    def __init__(self, name):
        self.name = name
        self.stopped = 0

    async def stop(self):
        self.stopped += 1


class FakeManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.devices = {n: FakeDevice(n) for n in (cfg.get("devices") or {})}

    async def run_forever(self):
        try:
            await asyncio.Event().wait()
        finally:
            for device in self.devices.values():
                await device.stop()


def make_engine():
    return SimpleNamespace(
        wmbus_listener=None, esphome_manager=None, history=FakeHistory(), _wmbus_meters={},
        _prune_calls=0,
        _prune_esphome_health=lambda: None,
    )


def make_config(wmbus=None, esphome=None, ebus=None):
    return SimpleNamespace(raw={"wmbus": wmbus or {}}, esphome=esphome, ebus=ebus or {})


def patch_factories(monkeypatch):
    monkeypatch.setattr(pl, "_make_wmbus_listener", lambda engine, cfg: FakeListener(cfg))
    monkeypatch.setattr(pl, "_make_esphome_manager", lambda engine, cfg: FakeManager(cfg))


def test_wmbus_disabled_stops_running_listener(monkeypatch):
    patch_factories(monkeypatch)
    engine = make_engine()
    engine.wmbus_listener = FakeListener({})
    listener = engine.wmbus_listener
    assert pl.sync_wmbus(engine, make_config(wmbus={"enabled": False})) == "stopped"
    assert listener.stopped is True
    assert engine.wmbus_listener is None
    assert engine.history.statuses[pl.WMBUS_HEALTH_KEY][1] == "wyłączono (wtyczka)"


def test_wmbus_enabled_starts_listener_with_ebus_fallback(monkeypatch):
    patch_factories(monkeypatch)
    engine = make_engine()
    config = make_config(wmbus={"enabled": True}, ebus={"mqtt_host": "10.0.0.5", "mqtt_port": 1884})
    assert pl.sync_wmbus(engine, config) == "started"
    assert engine.wmbus_listener.started is True
    assert engine.wmbus_listener.cfg == {"mqtt_host": "10.0.0.5", "mqtt_port": 1884, "topic_prefix": "wmbusmeters"}


def test_wmbus_unchanged_keeps_listener(monkeypatch):
    patch_factories(monkeypatch)
    engine = make_engine()
    config = make_config(wmbus={"enabled": True})
    pl.sync_wmbus(engine, config)
    listener = engine.wmbus_listener
    assert pl.sync_wmbus(engine, config) == "unchanged"
    assert engine.wmbus_listener is listener and listener.stopped is False


def test_wmbus_changed_connection_restarts_listener(monkeypatch):
    patch_factories(monkeypatch)
    engine = make_engine()
    pl.sync_wmbus(engine, make_config(wmbus={"enabled": True}))
    old = engine.wmbus_listener
    assert pl.sync_wmbus(engine, make_config(wmbus={"enabled": True, "topic_prefix": "inne"})) == "restarted"
    assert old.stopped is True
    assert engine.wmbus_listener is not old and engine.wmbus_listener.started is True


def test_wmbus_missing_section_means_disabled(monkeypatch):
    patch_factories(monkeypatch)
    engine = make_engine()
    assert pl.sync_wmbus(engine, make_config()) == "unchanged"
    assert engine.wmbus_listener is None


def test_esphome_start_stop_and_restart(monkeypatch):
    patch_factories(monkeypatch)

    async def scenario():
        engine = make_engine()
        cfg_a = {"enabled": True, "devices": {"licznik": {"host": "1.2.3.4"}}}
        assert await pl.sync_esphome(engine, make_config(esphome=cfg_a)) == "started"
        await asyncio.sleep(0)
        first = engine.esphome_manager
        assert await pl.sync_esphome(engine, make_config(esphome=cfg_a)) == "unchanged"

        cfg_b = {"enabled": True, "devices": {"licznik": {"host": "1.2.3.4"}, "nowy": {"host": "1.2.3.5"}}}
        assert await pl.sync_esphome(engine, make_config(esphome=cfg_b)) == "restarted"
        await asyncio.sleep(0)
        assert first.devices["licznik"].stopped == 1
        assert engine.esphome_manager is not first

        second = engine.esphome_manager
        assert await pl.sync_esphome(engine, make_config(esphome={**cfg_b, "enabled": False})) == "stopped"
        assert engine.esphome_manager is None
        assert all(d.stopped == 1 for d in second.devices.values())

    asyncio.run(scenario())


def test_esphome_disabled_stops_manager_started_elsewhere(monkeypatch):
    patch_factories(monkeypatch)

    async def scenario():
        engine = make_engine()
        manager = FakeManager({"enabled": True, "devices": {"x": {"host": "h"}}})
        task = asyncio.create_task(manager.run_forever())
        await asyncio.sleep(0)
        engine.esphome_manager = manager
        assert await pl.sync_esphome(engine, make_config(esphome={"enabled": False})) == "stopped"
        assert task.done()
        assert manager.devices["x"].stopped == 1

    asyncio.run(scenario())


def test_esphome_empty_config_means_disabled(monkeypatch):
    patch_factories(monkeypatch)

    async def scenario():
        engine = make_engine()
        assert await pl.sync_esphome(engine, make_config(esphome=None)) == "unchanged"
        assert engine.esphome_manager is None

    asyncio.run(scenario())


def test_sync_plugin_lifecycle_isolates_failures(monkeypatch):
    patch_factories(monkeypatch)

    def boom(engine, new_config):
        raise RuntimeError("x")

    monkeypatch.setattr(pl, "sync_wmbus", boom)

    async def scenario():
        engine = make_engine()
        result = await pl.sync_plugin_lifecycle(engine, make_config(esphome=None))
        assert result == {"wmbus": "error", "esphome": "unchanged"}

    asyncio.run(scenario())
