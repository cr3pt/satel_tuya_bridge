# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from copy import deepcopy

import pytest

from bridge.wmbus import merge_keys

KEY = '0123456789abcdef' * 2
OTHER_KEY = 'f' * 32


@pytest.mark.parametrize('incoming,existing,expected', [
    ([{'name': 'new', 'id': '00ab', 'key': ''}], [{'name': 'old', 'id': '00AB', 'key': KEY}], KEY),
    ([{'name': 'same', 'id': '2222', 'key': ''}], [{'name': 'same', 'id': '1111', 'key': KEY}], ''),
    ([{'name': 'same', 'key': ''}], [{'name': 'same', 'key': KEY}], KEY),
    ([{'name': 'new', 'id': '00AB', 'clear_key': True}], [{'name': 'old', 'id': '00AB', 'key': KEY}], ''),
    ([{'name': 'new', 'id': '00AB', 'key': OTHER_KEY}], [{'name': 'old', 'id': '00AB', 'key': KEY}], OTHER_KEY),
    ([{'name': 'same', 'id': '00AB', 'key': ''}], [{'name': 'same', 'key': KEY}], ''),
    ([{'name': 'same', 'key': ''}], [{'name': 'same', 'id': '00AB', 'key': KEY}], ''),
    ([{'name': 'new', 'id': '00AB', 'key': ''}], [{'name': 'a', 'id': '00AB', 'key': KEY}, {'name': 'b', 'id': '00ab', 'key': OTHER_KEY}], ''),
], ids=['rename-and-case', 'replacement-meter', 'legacy-without-serials', 'explicit-clear', 'explicit-replacement', 'no-legacy-key-to-new-serial', 'no-key-from-serial-to-legacy', 'ambiguous-serial'])
def test_key_follows_serial_not_display_name(incoming, existing, expected):
    before = deepcopy((incoming, existing))
    result = merge_keys(incoming, existing)
    assert result[0]['key'] == expected
    assert 'clear_key' not in result[0]
    assert (incoming, existing) == before
