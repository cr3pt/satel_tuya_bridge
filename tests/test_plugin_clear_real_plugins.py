# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause
import asyncio
from types import SimpleNamespace

import pytest
from quart import Quart

import plugins
from bridge.config_store import ConfigStore
from dashboard.plugin_clear_config import (clearable_plugin_ids, config_key, effective_config,
    register_plugin_clear_config_routes, remove_wmbusmeters_files)

METAS, ERRORS = plugins.discover_plugin_metas()
KEY = '0123456789ABCDEF0123456789ABCDEF'


def test_all_real_plugins_are_discovered():
    assert ERRORS == []
    assert len(METAS) == 17


@pytest.mark.parametrize('meta', METAS, ids=lambda meta: meta.id)
def test_every_real_plugin_with_enabled_false_is_clearable(meta):
    documents = {'config': {config_key(meta): {'enabled': False}}}
    assert meta.id in clearable_plugin_ids(METAS, effective_config(documents))


def make_client(tmp_path, config):
    store = ConfigStore(tmp_path / 'config.db')
    store.set('config', config)
    app = Quart(__name__)
    register_plugin_clear_config_routes(app, metas=METAS,
        require_auth=lambda **kwargs: lambda fn: fn, get_auth_guard=lambda: None,
        get_config_store=lambda: store, reload_bridge_config=lambda: None,
        enqueue_bridge_reload=lambda: None,
        get_history=lambda: SimpleNamespace(write_event=lambda *args, **kwargs: None))
    return app.test_client(), store


def make_wmbusmeters_files(tmp_path):
    etc = tmp_path / 'wmbusmeters_conf' / 'etc'
    (etc / 'wmbusmeters.d').mkdir(parents=True)
    (etc / 'wmbusmeters.conf').write_text('device=auto\n', encoding='utf-8')
    (etc / 'wmbusmeters.d' / 'licznik').write_text(f'key={KEY}\n', encoding='utf-8')
    (etc / 'inny_plik.txt').write_text('zostaje\n', encoding='utf-8')
    return etc


def test_clearing_wmbus_removes_generated_files_with_aes_keys(tmp_path):
    etc = make_wmbusmeters_files(tmp_path)
    client, store = make_client(tmp_path, {'wmbus': {'enabled': False, 'meters': [{'name': 'licznik', 'id': '1', 'key': KEY}]}})

    async def scenario():
        response = await client.post('/api/plugins/wmbus/clear_config')
        assert response.status_code == 200
    asyncio.run(scenario())
    assert store.get('config')['wmbus'] == {'enabled': False}
    assert not (etc / 'wmbusmeters.conf').exists()
    assert not (etc / 'wmbusmeters.d' / 'licznik').exists()
    assert (etc / 'inny_plik.txt').exists()


def test_clearing_other_plugin_keeps_wmbusmeters_files(tmp_path):
    etc = make_wmbusmeters_files(tmp_path)
    client, _store = make_client(tmp_path, {'esphome': {'enabled': False, 'devices': {'x': {'host': 'h'}}}})

    async def scenario():
        response = await client.post('/api/plugins/esphome/clear_config')
        assert response.status_code == 200
    asyncio.run(scenario())
    assert (etc / 'wmbusmeters.conf').exists()
    assert (etc / 'wmbusmeters.d' / 'licznik').exists()


def test_enabled_wmbus_keeps_files(tmp_path):
    etc = make_wmbusmeters_files(tmp_path)
    client, _store = make_client(tmp_path, {'wmbus': {'enabled': True, 'meters': [{'name': 'licznik', 'id': '1'}]}})

    async def scenario():
        response = await client.post('/api/plugins/wmbus/clear_config')
        assert response.status_code == 409
    asyncio.run(scenario())
    assert (etc / 'wmbusmeters.d' / 'licznik').exists()


def test_removing_missing_files_is_safe(tmp_path):
    assert remove_wmbusmeters_files(tmp_path / 'config.db') == 0
