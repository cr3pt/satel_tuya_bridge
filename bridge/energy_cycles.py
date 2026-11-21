# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Pomocniki dla cykli zużycia energii (dziennych, tygodniowych, miesięcznych),
daty wartości startowej licznika oraz reguł dni wolnych G12w."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .tariffs import is_free_day

TZ = ZoneInfo("Europe/Warsaw")


def parse_start_date_ts(start_date_iso: str | None) -> float | None:
    """Konwertuje datę startową 'YYYY-MM-DD' na początek doby (unix timestamp, Europe/Warsaw)."""
    if not start_date_iso:
        return None
    raw = str(start_date_iso).strip()
    if not raw:
        return None
    d = date.fromisoformat(raw)
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


def effective_from_ts(requested_from: float, start_date_iso: str | None) -> float:
    """Dla obliczeń wstecz za X dni (np. Ostatnie 30 dni): jeśli data startowa
    licznika wypada wewnątrz okna, obliczenia zaczynają się od daty startowej."""
    start_ts = parse_start_date_ts(start_date_iso)
    if start_ts is not None and start_ts > requested_from:
        return start_ts
    return requested_from


def day_period_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def week_period_key(ts: float) -> str:
    iso = datetime.fromtimestamp(ts, TZ).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def month_period_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m")


def is_g12w_daytime_disabled(d: date) -> bool:
    """W taryfie G12w w weekendy i polskie święta ustawowe nie ma strefy dziennej."""
    return is_free_day(d)


def persist_cycle_snapshot(history, meter_id: str, cycle_type: str, period_key: str,
                           zones: list[dict], vat_percent: float, reading_ts: float | None = None) -> None:
    """Zapisuje podsumowanie zużycia i kosztów w bazie (`energy_cycles`) dla każdej strefy."""
    factor = 1.0 + float(vat_percent or 0.0) / 100.0
    for z in zones:
        net = float(z.get("net") or 0.0)
        history.upsert_energy_cycle(
            meter_id=meter_id,
            cycle_type=cycle_type,
            period_key=period_key,
            zone=str(z["zone"]),
            kwh=float(z.get("kwh") or 0.0),
            cost_net=round(net, 2),
            cost_gross=round(net * factor, 2),
            reading_ts=reading_ts,
        )
