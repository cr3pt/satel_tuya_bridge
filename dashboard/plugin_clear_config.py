# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause
"""Atomic configuration reset for inactive plugins; history is never deleted."""
from copy import deepcopy
import json
import time

from quart import jsonify


def config_key(meta):
    return meta.config_key or meta.id


def is_clearable_meta(meta):
    return bool(config_key(meta))


def plugin_is_inactive(meta, current):
    try:
        if meta.describe_state is not None:
            state = meta.describe_state(current)
            return isinstance(state, dict) and state.get('enabled') is False
        return (current.get(config_key(meta)) or {}).get('enabled') is False
    except Exception:
        return False


def clearable_plugin_ids(metas, current):
    return [m.id for m in metas if plugin_is_inactive(m, current)]


def effective_config(documents):
    current = deepcopy(documents.get('config', {}))
    if 'presence_simulation' in documents.get('automations', {}):
        current['presence_simulation'] = documents['automations']['presence_simulation']
    current.setdefault('tuya', {}).update({
        k: v for k, v in documents.get('tuya_devices', {}).items()
        if k in ('devices', 'zigbee_gateways', 'cloud_devices')
    })
    current.setdefault('ebus', {})['sensors'] = documents.get('sensors', {}).get('sensors', [])
    return current


def reset_documents(meta, documents):
    result = deepcopy(documents)
    current = result.setdefault('config', {})
    key = config_key(meta)
    current[key] = {'enabled': False}
    if meta.id == 'presence_simulation':
        result.setdefault('automations', {})['presence_simulation'] = {'enabled': False}
    elif meta.id == 'tuya_local':
        devices = result.setdefault('tuya_devices', {})
        for name in ('devices', 'zigbee_gateways', 'cloud_devices'):
            devices[name] = {}
    elif meta.id == 'satel':
        fields = result.setdefault('satel_fields', {})
        for name in ('monitored_zones', 'monitored_outputs', 'partitions',
                     'zone_types', 'output_types', 'virtual_inputs'):
            fields[name] = {}
    elif meta.id == 'ebus':
        result.setdefault('sensors', {})['sensors'] = []
        # Other integrations may use this shared MQTT broker, even with eBUS off.
        original = documents.get('config', {}).get('ebus', {})
        for name, value in original.items():
            if name.startswith('mqtt_'):
                current[key][name] = deepcopy(value)
    return result


def register_plugin_clear_config_routes(app, *, metas, require_auth, get_auth_guard,
        get_config_store, reload_bridge_config, enqueue_bridge_reload, get_history):
    by_id = {m.id: m for m in metas}

    def read_documents(connection):
        return {name: json.loads(data) for name, data in
                connection.execute('SELECT name, data FROM documents')}

    @app.route('/api/plugins/clearable', methods=['GET'], endpoint='plugins_clearable_get')
    @require_auth(get_guard=get_auth_guard)
    async def clearable():
        store = get_config_store()
        with store._lock:
            documents = read_documents(store._conn)
        return jsonify(clearable_plugin_ids(metas, effective_config(documents)))

    @app.route('/api/plugins/<plugin_id>/clear_config', methods=['POST'], endpoint='plugins_clear_config_post')
    @require_auth(get_guard=get_auth_guard)
    async def clear(plugin_id):
        meta = by_id.get(plugin_id)
        if meta is None:
            return jsonify({'error': 'Nieznana wtyczka'}), 404
        store = get_config_store()
        with store._lock:
            connection = store._conn
            try:
                connection.execute('BEGIN IMMEDIATE')
                documents = read_documents(connection)
                if not plugin_is_inactive(meta, effective_config(documents)):
                    connection.rollback()
                    return jsonify({'error': 'Najpierw wyłącz wtyczkę i zapisz zmiany.'}), 409
                updated = reset_documents(meta, documents)
                for name, data in updated.items():
                    if name in documents and data == documents[name]:
                        continue
                    connection.execute(
                        'INSERT INTO documents(name,data,updated_ts) VALUES(?,?,?) '
                        'ON CONFLICT(name) DO UPDATE SET data=excluded.data, updated_ts=excluded.updated_ts',
                        (name, json.dumps(data, ensure_ascii=False), time.time()))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        reload_bridge_config()
        enqueue_bridge_reload()
        get_history().write_event('system', source='dashboard',
            detail={'message': f'Wyczyszczono konfigurację wtyczki {plugin_id}'})
        message = f'Wyczyszczono konfigurację wtyczki {meta.name}.'
        if plugin_id == 'ebus':
            message += ' Zachowano współdzielone ustawienia brokera MQTT.'
        if meta.restart_required:
            message += ' Wymagany pełny restart procesów, aby zwolnić stare połączenia.'
        return jsonify({'cleared': plugin_id, 'message': message})
