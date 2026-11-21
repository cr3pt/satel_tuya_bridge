# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Serial/AES pair handling, independent of MQTT topics and display names.

Integration with bridge.wmbus.merge_keys is a separate change.
"""
from __future__ import annotations


def serial_identity(meter: dict) -> str:
    value = meter.get("id")
    return "" if value is None else str(value).strip().upper()


def merge_key_pairs(new_meters: list[dict], existing: list[dict]) -> list[dict]:
    """Keep AES keys only for the same serial, preserving leading zeroes.

    Missing serials and conflicting stored pairs must be resolved explicitly.
    Never include secret keys in validation errors.
    """
    old_pairs: dict[str, str] = {}
    for meter in existing:
        identity = serial_identity(meter)
        if not identity:
            continue
        key = (meter.get("key") or "").strip()
        if identity in old_pairs and old_pairs[identity].upper() != key.upper():
            raise ValueError("Sprzeczne klucze dla numeru seryjnego " + identity)
        old_pairs[identity] = key

    seen: set[str] = set()
    merged = []
    for incoming in new_meters:
        meter = dict(incoming)
        identity = serial_identity(meter)
        if not identity:
            raise ValueError("Wymagany numer seryjny do przypisania klucza licznika")
        if identity in seen:
            raise ValueError("Zduplikowany numer seryjny " + identity)
        seen.add(identity)
        if meter.pop("clear_key", False):
            meter["key"] = ""
        elif not (meter.get("key") or "").strip():
            meter["key"] = old_pairs.get(identity, "")
        else:
            meter["key"] = meter["key"].strip()
        merged.append(meter)
    return merged
