# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from bridge.config_store import ConfigStore
from bridge.wmbus_key_pairs import merge_key_pairs


def test_serial_key_pair_survives_database_reopen_and_rename(tmp_path):
    path = tmp_path / 'config.db'
    key = '0123456789abcdef' * 2
    store = ConfigStore(path)
    try:
        store.set('config', {'wmbus': {'meters': [{'name': 'old', 'id': '0012AB34', 'key': key}]}})
    finally:
        store._conn.close()
    reopened = ConfigStore(path)
    try:
        config = reopened.get('config')
        existing = config['wmbus']['meters']
        merged = merge_key_pairs([{'name': 'renamed', 'id': '0012ab34', 'key': ''}], existing)
        assert merged[0]['key'] == key
        config['wmbus']['meters'] = merged
        reopened.set('config', config)
    finally:
        reopened._conn.close()
    final = ConfigStore(path)
    try:
        meter = final.get('config')['wmbus']['meters'][0]
        assert meter['name'] == 'renamed'
        assert meter['id'] == '0012ab34'
        assert meter['key'] == key
        replacement = merge_key_pairs([{'name': 'renamed', 'id': '99887766', 'key': ''}], [meter])
        assert replacement[0]['key'] == ''
    finally:
        final._conn.close()
