# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause
from copy import deepcopy
from types import SimpleNamespace
import asyncio

import pytest
from quart import Quart
from bridge.config_store import ConfigStore
from dashboard.plugin_clear_config import (clearable_plugin_ids, effective_config,
    plugin_is_inactive, register_plugin_clear_config_routes, reset_documents)


def meta(name, key=None):
    key = key or name
    return SimpleNamespace(id=name, config_key=key, name=name, restart_required=False,
        describe_state=lambda config: {'enabled': bool(config.get(key, {}).get('enabled', False))})


@pytest.mark.parametrize('name,key', [('satel','satel'), ('tuya_local','tuya'),
    ('ebus','ebus'), ('presence_simulation','presence_simulation'),
    ('home_assistant','home_assistant'), ('mqtt_publish','mqtt_publish'),
    ('energy_cost','energy'), ('esphome','esphome'), ('wmbus','wmbus'),
    ('homekit','homekit'), ('mcp_server','mcp_server'), ('notifications','notifications'),
    ('homewhiz','homewhiz'), ('lg_thinq','lg_thinq'), ('iqua','iqua'),
    ('smartthings','smartthings'), ('unifi_protect','unifi_protect')])
def test_every_inactive_plugin_is_clearable(name, key):
    item = meta(name, key)
    assert clearable_plugin_ids([item], {key: {'enabled': False}}) == [name]
    assert clearable_plugin_ids([item], {key: {'enabled': True}}) == []


def test_missing_flag_uses_describe_state_and_errors_fail_closed():
    item = meta('home_assistant')
    assert plugin_is_inactive(item, {})
    item.describe_state = lambda config: 1 / 0
    assert not plugin_is_inactive(item, {})


def test_documents_are_isolated_and_required_sections_remain():
    original = {'config': {'satel': {'enabled': False, 'host': 'secret'},
        'tuya': {'enabled': True}, 'ebus': {'enabled': True}},
        'satel_fields': {'partitions': {'home': 1}, 'custom': 2},
        'automations': {'scenes': [{'name': 'keep'}]}}
    snapshot = deepcopy(original)
    result = reset_documents(meta('satel'), original)
    assert original == snapshot
    assert result['config']['satel'] == {'enabled': False}
    assert result['config']['tuya'] == original['config']['tuya']
    assert result['satel_fields']['partitions'] == {}
    assert result['satel_fields']['custom'] == 2
    assert result['automations'] == original['automations']


def test_tuya_devices_and_credentials_are_cleared():
    result = reset_documents(meta('tuya_local', 'tuya'), {
        'config': {'tuya': {'cloud_accounts': {'secret': 1}}},
        'tuya_devices': {'devices': {'x': 1}, 'cloud_devices': {'y': 2}, 'zigbee_gateways': {'z': 3}}})
    assert result['config']['tuya'] == {'enabled': False}
    assert all(not value for value in result['tuya_devices'].values())


def test_presence_overlay_and_other_automations_preserved():
    documents = {'config': {}, 'automations': {
        'presence_simulation': {'enabled': True, 'devices': ['x']}, 'scenes': [1]}}
    assert not plugin_is_inactive(meta('presence_simulation'), effective_config(documents))
    result = reset_documents(meta('presence_simulation'), documents)
    assert result['automations']['presence_simulation'] == {'enabled': False}
    assert result['automations']['scenes'] == [1]


def test_ebus_keeps_shared_broker_and_clears_sensors():
    result = reset_documents(meta('ebus'), {'config': {'ebus': {
        'mqtt_host': 'broker', 'mqtt_port': 1883, 'enabled': False}}, 'sensors': {'sensors': [1]}})
    assert result['config']['ebus']['mqtt_host'] == 'broker'
    assert result['config']['ebus']['enabled'] is False
    assert result['sensors']['sensors'] == []


def test_api_checks_current_state_and_persists_reset(tmp_path):
    store = ConfigStore(tmp_path / 'config.db')
    store.set('config', {'wmbus': {'enabled': True, 'meters': [1]}})
    calls = []
    app = Quart(__name__)
    register_plugin_clear_config_routes(app, metas=[meta('wmbus')],
        require_auth=lambda **kwargs: lambda fn: fn, get_auth_guard=lambda: None,
        get_config_store=lambda: store, reload_bridge_config=lambda: calls.append('reload'),
        enqueue_bridge_reload=lambda: calls.append('enqueue'),
        get_history=lambda: SimpleNamespace(write_event=lambda *args, **kwargs: None))
    async def scenario():
        client = app.test_client()
        assert (await client.post('/api/plugins/wmbus/clear_config')).status_code == 409
        assert calls == []
        store.set('config', {'wmbus': {'enabled': False, 'meters': [1]}})
        assert (await client.get('/api/plugins/clearable')).status_code == 200
        assert (await client.post('/api/plugins/wmbus/clear_config')).status_code == 200
        assert store.get('config')['wmbus'] == {'enabled': False}
        assert (await client.post('/api/plugins/unknown/clear_config')).status_code == 404
    asyncio.run(scenario())
    assert calls == ['reload', 'enqueue']
