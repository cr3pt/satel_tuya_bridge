# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from copy import deepcopy

import pytest

from bridge.wmbus_key_pairs import merge_key_pairs

KEY = "0123456789abcdef" * 2


@pytest.mark.parametrize("incoming,existing,expected", [
    ({"name": "new", "id": "00ab"}, [{"name": "old", "id": "00AB", "key": KEY}], KEY),
    ({"name": "same", "id": "2222"}, [{"name": "same", "id": "1111", "key": KEY}], ""),
    ({"name": "new", "id": "00AB", "clear_key": True}, [{"id": "00AB", "key": KEY}], ""),
    ({"id": "00AB", "key": "f" * 32}, [{"id": "00AB", "key": KEY}], "f" * 32),
    ({"name": "same", "id": "00AB"}, [{"name": "same", "key": KEY}], ""),
    ({"id": "0001"}, [{"id": "1", "key": KEY}], ""),
    ({"id": "00AB"}, [{"id": "00AB", "key": KEY}, {"id": "00ab", "key": KEY.upper()}], KEY.upper()),
])
def test_pair_identity(incoming, existing, expected):
    before = deepcopy((incoming, existing))
    result = merge_key_pairs([incoming], existing)
    assert result[0]["key"] == expected
    assert "clear_key" not in result[0]
    assert (incoming, existing) == before


def test_missing_serial_does_not_fall_back_to_name():
    with pytest.raises(ValueError, match="numer seryjny"):
        merge_key_pairs([{"name": "same"}], [{"name": "same", "key": KEY}])


def test_conflicting_stored_pairs_are_rejected_without_leaking_keys():
    with pytest.raises(ValueError) as exc:
        merge_key_pairs([{"id": "00AB"}], [{"id": "00AB", "key": KEY}, {"id": "00ab", "key": "f" * 32}])
    assert KEY not in str(exc.value)
    assert "f" * 32 not in str(exc.value)


def test_duplicate_incoming_serials_are_rejected():
    with pytest.raises(ValueError, match="Zduplikowany"):
        merge_key_pairs([{"id": "00AB"}, {"id": "00ab"}], [])
