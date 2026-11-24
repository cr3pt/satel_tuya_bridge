# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Pomocniki dla cykli zużycia energii (dziennych, tygodniowych, miesięcznych),
daty wartości startowej licznika oraz reguł dni wolnych G12w."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .tariffs import is_free_day

TZ = ZoneInfo("Europe/Warsaw")
ROLLING_7D_CYCLE = "rolling_7d"
CYCLE_TYPES = ("daily", "weekly", "monthly")
MAX_CYCLE_COUNT = {"daily": 366, "weekly": 104, "monthly": 36}


def parse_start_date_ts(start_date_iso: str | None) -> float | None:
    """Konwertuje datę startową 'YYYY-MM-DD' na początek doby (unix timestamp, Europe/Warsaw)."""
    if not start_date_iso:
        return None
    raw = str(start_date_iso).strip()
    if not raw:
        return None
    d = date.fromisoformat(raw)
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


def check_start_date(start_date_iso: str, now: float) -> float:
    """Waliduje datę startową z formularza: format RRRR-MM-DD i nie z przyszłości."""
    start_ts = parse_start_date_ts(start_date_iso)
    if start_ts is None:
        raise ValueError("Pusta data startowa")
    if start_ts > now:
        raise ValueError("Data startowa nie może być z przyszłości")
    return start_ts


def apply_start_baseline(readings: list[dict] | None, start_ts: float | None,
                         start_value: float | None, scale: float = 1.0) -> list[dict] | None:
    """Odczyty licznika od daty startowej: odczyty sprzed daty startowej (np.
    stary licznik przed wymianą) są pomijane, a START LICZNIKA wpisany przez
    użytkownika staje się punktem bazowym o północy dnia startowego. Dzięki
    temu "Ostatnie 30 dni" przy starcie 10 dni temu = stan dzisiaj − START,
    nawet jeśli pierwsze odczyty w bazie pojawiły się później niż data startu."""
    if readings is None or start_ts is None:
        return readings
    out = [r for r in readings if r["t"] >= start_ts]
    if start_value is not None and not any(r["t"] == start_ts for r in out):
        out.insert(0, {"t": start_ts, "v": float(start_value) / (scale or 1.0)})
    return out


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


def _midnight(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


def recent_periods(cycle_type: str, now: float, count: int) -> list[tuple[str, float, float]]:
    """Ostatnie `count` okresów kalendarzowych (doba, tydzień ISO pn-nd,
    miesiąc) wg czasu lokalnego, od bieżącego wstecz: [(klucz, od, do)].
    Bieżący okres kończy się na `now`."""
    if cycle_type not in CYCLE_TYPES:
        raise ValueError(f"Nieznany cykl: {cycle_type!r} (dostępne: {', '.join(CYCLE_TYPES)})")
    today = datetime.fromtimestamp(now, TZ).date()
    out = []
    if cycle_type == "daily":
        for i in range(count):
            d = today - timedelta(days=i)
            out.append((d.isoformat(), _midnight(d), min(_midnight(d + timedelta(days=1)), now)))
    elif cycle_type == "weekly":
        monday = today - timedelta(days=today.weekday())
        for i in range(count):
            d = monday - timedelta(weeks=i)
            iso = d.isocalendar()
            out.append((f"{iso.year}-W{iso.week:02d}", _midnight(d), min(_midnight(d + timedelta(days=7)), now)))
    else:
        d = today.replace(day=1)
        for _ in range(count):
            nxt = (d + timedelta(days=32)).replace(day=1)
            out.append((d.strftime("%Y-%m"), _midnight(d), min(_midnight(nxt), now)))
            d = (d - timedelta(days=1)).replace(day=1)
    return out


def period_end(cycle_type: str, period_key: str) -> float:
    """Koniec pełnego okresu (bez przycinania do teraz) - do oceny, czy jest zamknięty."""
    if cycle_type == "daily":
        return _midnight(date.fromisoformat(period_key) + timedelta(days=1))
    if cycle_type == "weekly":
        year, week = period_key.split("-W")
        return _midnight(date.fromisocalendar(int(year), int(week), 1) + timedelta(days=7))
    year, month = (int(x) for x in period_key.split("-"))
    return _midnight(date(year + (month == 12), month % 12 + 1, 1))


def is_g12w_daytime_disabled(d: date) -> bool:
    """W taryfie G12w w weekendy i polskie święta ustawowe nie ma strefy dziennej."""
    return is_free_day(d)


def cycle_inputs(report: dict, default_vat: float) -> tuple[list[dict], float]:
    """Strefy i VAT do zapisu cyklu. Raport może mieć wynik w `report["result"]`
    albo (płasko) na najwyższym poziomie - obsługujemy oba kształty."""
    block = report.get("result") if isinstance(report.get("result"), dict) else report
    zones = [z for z in (block.get("zones") or []) if isinstance(z, dict) and "zone" in z]
    vat = block.get("vat_percent")
    return zones, float(default_vat if vat is None else vat)


def cycle_target(period: str | None, now: float, month_prefix: str = "month:") -> tuple[str, str] | None:
    """Typ i klucz cyklu do zapisu dla nazwanego okresu raportu albo None.
    `7d` to okno kroczące (nie tydzień kalendarzowy), więc zapisujemy je jako
    `rolling_7d` z datą końcową - jeden wiersz na dobę, bez mieszania z tygodniami ISO."""
    if period == "today":
        return "daily", day_period_key(now)
    if period == "yesterday":
        return "daily", (datetime.fromtimestamp(now, TZ).date() - timedelta(days=1)).isoformat()
    if period == "7d":
        return ROLLING_7D_CYCLE, day_period_key(now)
    if period == "this_month":
        return "monthly", month_period_key(now)
    if period == "last_month":
        first = datetime.fromtimestamp(now, TZ).date().replace(day=1)
        return "monthly", (first - timedelta(days=1)).strftime("%Y-%m")
    if period and period.startswith(month_prefix) and period[len(month_prefix):]:
        return "monthly", period[len(month_prefix):]
    return None


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
