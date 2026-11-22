# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio
from types import SimpleNamespace

from quart import Quart

from dashboard import plugin_clear_config as pcc
from plugins import PluginMeta


class FakeStore:
    def __init__(self, config):
        self.config = config

    def get(self, name):
        return self.config

    def set(self, name, value):
        self.config = value


class FakeHistory:
    def __init__(self):
        self.events = []

    def write_event(self, kind, source=None, detail=None):
        self.events.append((kind, detail))


def passthrough(get_guard=None):
    return lambda fn: fn


def make_meta(plugin_id="wmbus", toggle_route="toggle_prereq", describe=None, toggleable=True):
    return PluginMeta(id=plugin_id, name=plugin_id.upper(), icon="x", description="d", config_key=plugin_id,
                      toggle_route=toggle_route, toggleable=toggleable, describe_state=describe)


def make_client(config, metas):
    store, history, calls = FakeStore(config), FakeHistory(), []
    app = Quart(__name__)
    pcc.register_plugin_clear_config_routes(
        app, metas=metas, require_auth=passthrough, get_auth_guard=lambda: None,
        get_config_store=lambda: store, reload_bridge_config=lambda: calls.append("reload"),
        enqueue_bridge_reload=lambda: calls.append("enqueue"), get_history=lambda: history,
    )
    return app.test_client(), store, history, calls


def run(coro):
    return asyncio.run(coro)


def test_inactive_plugin_config_is_removed():
    client, store, history, calls = make_client({"wmbus": {"enabled": False, "meters": [{"id": "1"}]}}, [make_meta()])

    async def scenario():
        resp = await client.post("/api/plugins/wmbus/clear_config")
        assert resp.status_code == 200
    run(scenario())
    assert "wmbus" not in store.config
    assert calls == ["reload", "enqueue"]
    assert history.events


def test_enabled_plugin_is_refused_and_untouched():
    client, store, _, calls = make_client({"wmbus": {"enabled": True, "meters": [{"id": "1"}]}}, [make_meta()])

    async def scenario():
        resp = await client.post("/api/plugins/wmbus/clear_config")
        assert resp.status_code == 409
    run(scenario())
    assert store.config["wmbus"]["meters"] and calls == []


def test_describe_state_enabled_blocks_clearing():
    meta = make_meta(describe=lambda cfg: {"enabled": True})
    client, store, _, _ = make_client({"wmbus": {"enabled": False, "meters": [1]}}, [meta])

    async def scenario():
        resp = await client.post("/api/plugins/wmbus/clear_config")
        assert resp.status_code == 409
    run(scenario())
    assert "wmbus" in store.config


def test_missing_enabled_flag_is_treated_as_active():
    client, store, _, _ = make_client({"wmbus": {"meters": [1]}}, [make_meta()])

    async def scenario():
        assert (await client.post("/api/plugins/wmbus/clear_config")).status_code == 409
    run(scenario())
    assert "wmbus" in store.config


def test_unknown_and_non_clearable_plugins():
    client, store, _, _ = make_client(
        {"tuya": {"enabled": False, "x": 1}}, [make_meta("tuya", toggle_route="toggle_prereq_connections_live")])

    async def scenario():
        assert (await client.post("/api/plugins/nope/clear_config")).status_code == 404
        assert (await client.post("/api/plugins/tuya/clear_config")).status_code == 400
    run(scenario())
    assert "tuya" in store.config


def test_empty_section_returns_404():
    client, _, _, _ = make_client({"wmbus": {}}, [make_meta()])

    async def scenario():
        assert (await client.post("/api/plugins/wmbus/clear_config")).status_code == 404
    run(scenario())


def test_clearable_list_only_inactive_with_config():
    metas = [make_meta("wmbus"), make_meta("esphome"), make_meta("iqua")]
    config = {"wmbus": {"enabled": False, "meters": [1]}, "esphome": {"enabled": True, "devices": {"a": 1}},
              "iqua": {"enabled": False}}
    assert pcc.clearable_plugin_ids(metas, config) == ["wmbus"]
