# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Generyczne oznaczenie "to jest licznik energii" dla DOWOLNEGO czujnika z
historii (`readings`), niezależnie od tego, skąd pochodzi (wM-Bus, ESPHome,
w przyszłości inne) - jeden wspólny mechanizm, żeby raz oznaczony licznik
był widoczny tak samo wszędzie (wtyczka 💰 Koszty energii), zamiast
zgadywania po nazwie pola (co nie działa dla np. polskich nazw encji
ESPHome, które nie zawierają "kwh"/"energy").

Przechowywane jako zwykły status (kategoria "energy_meter", klucz
`energy_meter:<sensor>`) - bez osobnej tabeli, bez migracji."""
from __future__ import annotations

_CATEGORY = "energy_meter"
_KEY_PREFIX = "energy_meter:"


def mark_energy_meter(history, sensor: str, label: str) -> None:
    history.set_status(f"{_KEY_PREFIX}{sensor}", _CATEGORY, "1", label=label)


def unmark_energy_meter(history, sensor: str) -> None:
    history.delete_status(f"{_KEY_PREFIX}{sensor}")


def is_energy_meter(history, sensor: str) -> bool:
    return history.get_status_value(f"{_KEY_PREFIX}{sensor}") is not None


def list_energy_meter_sensors(history) -> list[dict]:
    """Wszystkie oznaczone czujniki: `[{"sensor": ..., "label": ...}, ...]`."""
    return [
        {"sensor": s["key"][len(_KEY_PREFIX):], "label": s["label"]}
        for s in history.get_status(_CATEGORY)
    ]


def configured_meter_sensors(energy_cfg: dict | None) -> set[str]:
    """Czujniki użyte w licznikach wtyczki Koszty energii (pojedynczy i per strefa)."""
    sensors: set[str] = set()
    for meter in (energy_cfg or {}).get("meters") or []:
        if meter.get("sensor"):
            sensors.add(meter["sensor"])
        sensors.update(s for s in (meter.get("zone_sensors") or {}).values() if s)
    return sensors


def sync_energy_sensors(history, energy_cfg: dict | None) -> None:
    """Odczyty oznaczonych (⚡) i użytych w Kosztach energii czujników są
    przechowywane bezterminowo (`energy_readings`)."""
    flagged = {m["sensor"] for m in list_energy_meter_sensors(history)}
    history.set_energy_sensors(flagged | configured_meter_sensors(energy_cfg))
