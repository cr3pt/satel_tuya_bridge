# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Taryfy energii elektrycznej (G11, G12, G12w, G13 i własne) oraz obliczanie
kosztu na podstawie narastającego licznika kWh zapisanego w historii.

Moduł jest CZYSTĄ LOGIKĄ (bez I/O, bez zależności od reszty bridge'a):
dostaje listę odczytów `[{"t": unix_ts, "v": kWh_narastająco}, ...]`
(dokładnie taki kształt zwraca `HistoryStore.get_history`) i konfigurację cen.

Jak liczymy:
 1. Każdy odcinek między kolejnymi odczytami dzielimy co 15 minut i przypisujemy
    do strefy taryfy wg czasu LOKALNEGO (Europe/Warsaw, z czasem letnim) oraz
    kalendarza dni wolnych (weekendy + polskie święta ustawowe).
    Zużycie w odcinku rozkładamy proporcjonalnie do czasu (zakładamy stały pobór
    między odczytami - im gęstsze odczyty, tym dokładniej).
 2. Odcinki dłuższe niż `max_gap_seconds` (np. licznik był offline) NIE są
    rozkładane na strefy - trafiają do `unallocated_kwh`, żeby nie zgadywać.
 3. Spadek licznika (reset/wymiana) pomija odcinek i jest zliczany w `resets`.
 4. Cena strefy = energia czynna + dystrybucja zmienna (+ `other_per_kwh_net`),
    opłaty stałe (`fixed_monthly_net`) naliczane proporcjonalnie do dni okresu,
    VAT na końcu.

UWAGA: godziny stref różnią się między operatorami (OSD) i mogą się zmieniać
z taryfą na kolejny rok - wbudowane warianty to wartości domyślne, zweryfikuj je
z taryfą swojego operatora albo zdefiniuj własną (`custom_tariffs`).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")
SLOT_SECONDS = 900

DAY_KINDS = ("all", "weekday", "free")
_HOURS_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def easter_sunday(year: int) -> date:
    """Wielkanoc (algorytm anonimowy gregoriański)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def polish_holidays(year: int) -> set[date]:
    easter = easter_sunday(year)
    days = {
        date(year, 1, 1), date(year, 1, 6), date(year, 5, 1), date(year, 5, 3),
        date(year, 8, 15), date(year, 11, 1), date(year, 11, 11),
        date(year, 12, 25), date(year, 12, 26),
        easter, easter + timedelta(days=1),
        easter + timedelta(days=49),
        easter + timedelta(days=60),
    }
    if year >= 2025:
        days.add(date(year, 12, 24))
    return days


def is_free_day(d: date) -> bool:
    """Weekend lub święto ustawowe - 'dzień wolny' w rozumieniu taryf G12w/G13."""
    return d.weekday() >= 5 or d in polish_holidays(d.year)


@dataclass(frozen=True)
class Rule:
    zone: str
    days: str = "all"
    months: tuple[int, ...] = ()
    ranges: tuple[tuple[int, int], ...] = ()

    def matches(self, d: date, minute_of_day: int, free: bool) -> bool:
        if self.days == "weekday" and free:
            return False
        if self.days == "free" and not free:
            return False
        if self.months and d.month not in self.months:
            return False
        return any(a <= minute_of_day < b for a, b in self.ranges)


@dataclass(frozen=True)
class Tariff:
    id: str
    label: str
    zones: tuple[str, ...]
    default_zone: str
    rules: tuple[Rule, ...] = ()

    def zone_at(self, dt_local: datetime) -> str:
        d = dt_local.date()
        minute = dt_local.hour * 60 + dt_local.minute
        free = is_free_day(d)
        for rule in self.rules:
            if rule.matches(d, minute, free):
                return rule.zone
        return self.default_zone


def parse_hours(spec: str) -> tuple[tuple[int, int], ...]:
    """'22:00-06:00' -> ((1320, 1440), (0, 360)); '00:00-24:00' = cała doba.
    Granice muszą wypadać co 15 minut (rozdzielczość obliczeń)."""
    m = _HOURS_RE.match(spec.strip())
    if not m:
        raise ValueError(f"Nieprawidłowy zakres godzin: {spec!r} (oczekiwano np. '22:00-06:00')")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if not (0 <= start <= 1440 and 0 <= end <= 1440) or m1 > 59 or m2 > 59:
        raise ValueError(f"Godziny poza zakresem 00:00-24:00: {spec!r}")
    if start % 15 or end % 15:
        raise ValueError(f"Granice stref muszą być co 15 minut: {spec!r}")
    if start == end:
        raise ValueError(f"Pusty zakres godzin: {spec!r}")
    if start < end:
        return ((start, end),)
    return ((start, 1440), (0, end))


def _rule(zone: str, hours: list[str], days: str = "all", months: tuple[int, ...] = ()) -> Rule:
    ranges: list[tuple[int, int]] = []
    for spec in hours:
        ranges.extend(parse_hours(spec))
    return Rule(zone=zone, days=days, months=months, ranges=tuple(ranges))


_SUMMER = (4, 5, 6, 7, 8, 9)
_WINTER = (10, 11, 12, 1, 2, 3)

_G12_VARIANTS: dict[str, tuple[str, list[Rule]]] = {
    "default": ("Tauron / Energa / PGE zimą: 13-15 i 22-6", [_rule("nocna", ["13:00-15:00", "22:00-06:00"])]),
    "pge_sezonowa": ("PGE: zimą 13-15, latem 15-17, zawsze 22-6", [
        _rule("nocna", ["15:00-17:00", "22:00-06:00"], months=_SUMMER),
        _rule("nocna", ["13:00-15:00", "22:00-06:00"], months=_WINTER),
    ]),
    "enea": ("Enea: 21-6", [_rule("nocna", ["21:00-06:00"])]),
}

G12_VARIANT_IDS = tuple(_G12_VARIANTS)


def _build_g11(_variant: str | None) -> Tariff:
    return Tariff("G11", "G11 - jedna strefa", ("całodobowa",), "całodobowa")


def _build_g12(variant: str | None) -> Tariff:
    key = variant or "default"
    if key not in _G12_VARIANTS:
        raise ValueError(f"Nieznany wariant G12: {key!r} (dostępne: {', '.join(G12_VARIANT_IDS)})")
    return Tariff("G12", "G12 - dzienna / nocna", ("dzienna", "nocna"), "dzienna", tuple(_G12_VARIANTS[key][1]))


def _build_g12w(variant: str | None) -> Tariff:
    key = variant or "default"
    if key not in _G12_VARIANTS:
        raise ValueError(f"Nieznany wariant G12w: {key!r} (dostępne: {', '.join(G12_VARIANT_IDS)})")
    rules = (_rule("nocna", ["00:00-24:00"], days="free"), *_G12_VARIANTS[key][1])
    return Tariff("G12w", "G12w - dzienna / nocna, weekendy tanio", ("dzienna", "nocna"), "dzienna", rules)


def _build_g13(_variant: str | None) -> Tariff:
    rules = (
        _rule("szczyt_przedpołudniowy", ["07:00-13:00"], days="weekday"),
        _rule("szczyt_popołudniowy", ["19:00-22:00"], days="weekday", months=_SUMMER),
        _rule("szczyt_popołudniowy", ["16:00-21:00"], days="weekday", months=_WINTER),
    )
    return Tariff(
        "G13", "G13 - trzy strefy (Tauron)",
        ("szczyt_przedpołudniowy", "szczyt_popołudniowy", "pozostałe"), "pozostałe", rules,
    )


BUILTIN_TARIFFS = {"G11": _build_g11, "G12": _build_g12, "G12w": _build_g12w, "G13": _build_g13}


def tariff_from_custom(tariff_id: str, spec: dict) -> Tariff:
    """Własna taryfa z konfiguracji:
        {label, zones: [...], default_zone, rules: [{zone, days, months, hours: ["22:00-06:00"]}]}"""
    zones = tuple(spec.get("zones") or ())
    if not zones or len(set(zones)) != len(zones) or not all(isinstance(z, str) and z for z in zones):
        raise ValueError(f"Taryfa '{tariff_id}': 'zones' musi być niepustą listą unikalnych nazw")
    default_zone = spec.get("default_zone") or zones[0]
    if default_zone not in zones:
        raise ValueError(f"Taryfa '{tariff_id}': default_zone '{default_zone}' nie jest na liście stref")
    rules = []
    for i, r in enumerate(spec.get("rules") or []):
        if r.get("zone") not in zones:
            raise ValueError(f"Taryfa '{tariff_id}', reguła {i + 1}: nieznana strefa {r.get('zone')!r}")
        days = r.get("days", "all")
        if days not in DAY_KINDS:
            raise ValueError(f"Taryfa '{tariff_id}', reguła {i + 1}: days musi być jednym z {DAY_KINDS}")
        months = tuple(int(m) for m in (r.get("months") or ()))
        if any(not 1 <= m <= 12 for m in months):
            raise ValueError(f"Taryfa '{tariff_id}', reguła {i + 1}: miesiące 1-12")
        hours = r.get("hours") or ["00:00-24:00"]
        rules.append(_rule(r["zone"], list(hours), days=days, months=months))
    return Tariff(tariff_id, spec.get("label") or tariff_id, zones, default_zone, tuple(rules))


def resolve_tariff(tariff_id: str, variant: str | None = None, custom: dict | None = None) -> Tariff:
    """Własne taryfy mają pierwszeństwo przed wbudowanymi o tej samej nazwie."""
    if custom and tariff_id in custom:
        return tariff_from_custom(tariff_id, custom[tariff_id])
    builder = BUILTIN_TARIFFS.get(tariff_id)
    if builder is None:
        raise ValueError(f"Nieznana taryfa: {tariff_id!r} (wbudowane: {', '.join(BUILTIN_TARIFFS)})")
    return builder(variant)


def zone_seconds(tariff: Tariff, t0: float, t1: float) -> dict[str, float]:
    """Ile sekund przedziału [t0, t1] przypada na każdą strefę."""
    out: dict[str, float] = {}
    cur = t0
    while cur < t1:
        nxt = min(t1, (math.floor(cur / SLOT_SECONDS) + 1) * SLOT_SECONDS)
        zone = tariff.zone_at(datetime.fromtimestamp((cur + nxt) / 2, TZ))
        out[zone] = out.get(zone, 0.0) + (nxt - cur)
        cur = nxt
    return out


@dataclass
class Consumption:
    kwh_by_zone: dict[str, float] = field(default_factory=dict)
    unallocated_kwh: float = 0.0
    resets: int = 0
    gaps: int = 0
    samples: int = 0

    @property
    def total_kwh(self) -> float:
        return sum(self.kwh_by_zone.values())


def consumption_by_zone(readings: list[dict], tariff: Tariff, ts_from: float, ts_to: float,
                        max_gap_seconds: float = 6 * 3600, scale: float = 1.0) -> Consumption:
    """`readings`: odczyty narastającego licznika [{"t":..., "v":...}]. Odczyty
    mogą wychodzić poza [ts_from, ts_to] - odcinki są przycinane do okresu
    (proporcjonalnie), więc warto pobrać odczyt tuż przed początkiem okresu."""
    result = Consumption(kwh_by_zone={z: 0.0 for z in tariff.zones})
    points = sorted(((r["t"], r["v"] * scale) for r in readings), key=lambda p: p[0])
    result.samples = len(points)
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        dv = v1 - v0
        if dv < 0:
            result.resets += 1
            continue
        a, b = max(t0, ts_from), min(t1, ts_to)
        if b <= a:
            continue
        dv *= (b - a) / dt
        if dt > max_gap_seconds:
            result.gaps += 1
            result.unallocated_kwh += dv
            continue
        for zone, secs in zone_seconds(tariff, a, b).items():
            result.kwh_by_zone[zone] = result.kwh_by_zone.get(zone, 0.0) + dv * secs / (b - a)
    return result


def _zone_price_net(prices: dict, zone: str, other: float) -> float | None:
    """Cena netto za kWh w strefie albo None, gdy cena nie jest ustawiona.
    Ceny można podać jako liczbę (suma) albo {energy_net, distribution_net}."""
    p = prices.get(zone)
    if p is None:
        return None
    if isinstance(p, (int, float)) and not isinstance(p, bool):
        return float(p) + other
    if isinstance(p, dict):
        return float(p.get("energy_net", 0) or 0) + float(p.get("distribution_net", 0) or 0) + other
    return None


def fixed_fee_net(monthly_net: float, ts_from: float, ts_to: float) -> float:
    """Opłaty stałe (zł/miesiąc netto) proporcjonalnie do doby (czas lokalny)
    - pełny dzień = 1/liczba_dni_miesiąca opłaty."""
    if monthly_net <= 0 or ts_to <= ts_from:
        return 0.0
    total = 0.0
    day = datetime.fromtimestamp(ts_from, TZ).date()
    last = datetime.fromtimestamp(ts_to, TZ).date()
    while day <= last:
        start = datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp()
        nxt = day + timedelta(days=1)
        end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ).timestamp()
        covered = max(0.0, min(end, ts_to) - max(start, ts_from))
        month_len = (date(day.year + (day.month == 12), day.month % 12 + 1, 1) - date(day.year, day.month, 1)).days
        total += monthly_net / month_len * covered / (end - start)
        day = nxt
    return total


def compute_cost(consumption: Consumption, pricing: dict, tariff: Tariff,
                 ts_from: float, ts_to: float, default_vat_percent: float = 23.0) -> dict:
    """Koszt okresu. `pricing`: {prices: {strefa: cena|{energy_net, distribution_net}},
    other_per_kwh_net, fixed_monthly_net, vat_percent}. Strefy bez ceny trafiają
    do `missing_prices` (koszt liczony jako 0 + ostrzeżenie), a nie są zgadywane."""
    prices = pricing.get("prices") or {}
    other = float(pricing.get("other_per_kwh_net") or 0)
    vat = float(pricing.get("vat_percent", default_vat_percent) or 0)

    zones = []
    missing = []
    variable_net = 0.0
    for zone in tariff.zones:
        kwh = consumption.kwh_by_zone.get(zone, 0.0)
        price = _zone_price_net(prices, zone, other)
        if price is None:
            if kwh > 0:
                missing.append(zone)
            price_used = 0.0
        else:
            price_used = price
        net = kwh * price_used
        variable_net += net
        zones.append({"zone": zone, "kwh": round(kwh, 3), "price_net_per_kwh": None if price is None else round(price, 4),
                      "net": round(net, 2)})

    fixed_net = fixed_fee_net(float(pricing.get("fixed_monthly_net") or 0), ts_from, ts_to)
    total_net = variable_net + fixed_net
    gross = total_net * (1 + vat / 100)
    return {
        "tariff": tariff.id,
        "zones": zones,
        "total_kwh": round(consumption.total_kwh, 3),
        "unallocated_kwh": round(consumption.unallocated_kwh, 3),
        "resets": consumption.resets,
        "gaps": consumption.gaps,
        "samples": consumption.samples,
        "variable_net": round(variable_net, 2),
        "fixed_net": round(fixed_net, 2),
        "total_net": round(total_net, 2),
        "vat_percent": vat,
        "total_gross": round(gross, 2),
        "missing_prices": missing,
    }


def describe_schedule(tariff: Tariff) -> dict:
    """Godziny stref dla typowego dnia roboczego i wolnego, osobno dla lata
    (lipiec) i zimy (styczeń): {"lato": {"roboczy": [{"od","do","strefa"}], ...}}.
    Służy do wzrokowej weryfikacji z taryfą operatora."""
    samples = {
        "lato": {"roboczy": date(2026, 7, 8), "wolny": date(2026, 7, 11)},
        "zima": {"roboczy": date(2026, 1, 14), "wolny": date(2026, 1, 17)},
    }
    out: dict = {}
    for season, kinds in samples.items():
        out[season] = {}
        for kind, d in kinds.items():
            blocks: list[dict] = []
            for minute in range(0, 1440, 15):
                zone = tariff.zone_at(datetime(d.year, d.month, d.day, minute // 60, minute % 60))
                if blocks and blocks[-1]["strefa"] == zone:
                    blocks[-1]["do"] = minute + 15
                else:
                    blocks.append({"od": minute, "do": minute + 15, "strefa": zone})
            out[season][kind] = [
                {"od": f"{b['od'] // 60:02d}:{b['od'] % 60:02d}", "do": f"{b['do'] // 60:02d}:{b['do'] % 60:02d}",
                 "strefa": b["strefa"]} for b in blocks
            ]
    return out


PERIODS = ("today", "yesterday", "7d", "30d", "this_month", "last_month")


def resolve_period(name: str, now: float) -> tuple[float, float]:
    """Nazwany okres -> (od, do) w sekundach unix, wg doby lokalnej."""
    if name not in PERIODS:
        raise ValueError(f"Nieznany okres: {name!r} (dostępne: {', '.join(PERIODS)})")
    now_local = datetime.fromtimestamp(now, TZ)

    def midnight(d: date) -> float:
        return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()

    today = now_local.date()
    if name == "today":
        return midnight(today), now
    if name == "yesterday":
        return midnight(today - timedelta(days=1)), midnight(today)
    if name == "7d":
        return now - 7 * 86400, now
    if name == "30d":
        return now - 30 * 86400, now
    first_this = today.replace(day=1)
    if name == "this_month":
        return midnight(first_this), now
    last_prev = first_this - timedelta(days=1)
    return midnight(last_prev.replace(day=1)), midnight(first_this)


_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def _num(value, what: str, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: wymagana liczba") from None
    if not math.isfinite(v) or v < minimum or (maximum is not None and v > maximum):
        raise ValueError(f"{what}: poza dozwolonym zakresem")
    return v


def validate_energy_config(data) -> tuple[dict | None, str | None]:
    """Zwraca (oczyszczona_konfiguracja, None) albo (None, komunikat_błędu)."""
    try:
        return _validate_energy_config(data), None
    except ValueError as exc:
        return None, str(exc)


def _validate_energy_config(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Nieprawidłowe dane")

    vat = _num(data.get("vat_percent", 23), "VAT [%]", 0, 100)
    max_gap = _num(data.get("max_gap_minutes", 360), "Maks. przerwa między odczytami [min]", 5, 10080)

    custom_in = data.get("custom_tariffs") or {}
    if not isinstance(custom_in, dict):
        raise ValueError("custom_tariffs: oczekiwano słownika")
    custom: dict = {}
    for tid, spec in custom_in.items():
        if not _ID_RE.match(str(tid)):
            raise ValueError(f"Nazwa taryfy '{tid}': litery, cyfry, _ i - (max 20 znaków)")
        if not isinstance(spec, dict):
            raise ValueError(f"Taryfa '{tid}': oczekiwano obiektu")
        tariff_from_custom(tid, spec)
        custom[tid] = spec

    tariffs_in = data.get("tariffs") or {}
    if not isinstance(tariffs_in, dict):
        raise ValueError("tariffs: oczekiwano słownika")
    tariffs: dict = {}
    for tid, spec in tariffs_in.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Taryfa '{tid}': oczekiwano obiektu")
        variant = spec.get("variant") or None
        tariff = resolve_tariff(tid, variant, custom)
        prices_in = spec.get("prices") or {}
        prices: dict = {}
        for zone, price in prices_in.items():
            if zone not in tariff.zones:
                raise ValueError(f"Taryfa '{tid}': nieznana strefa '{zone}' (strefy: {', '.join(tariff.zones)})")
            if isinstance(price, dict):
                prices[zone] = {
                    "energy_net": _num(price.get("energy_net", 0) or 0, f"{tid}/{zone}: cena energii"),
                    "distribution_net": _num(price.get("distribution_net", 0) or 0, f"{tid}/{zone}: dystrybucja"),
                }
            else:
                prices[zone] = _num(price, f"{tid}/{zone}: cena")
        entry = {
            "prices": prices,
            "other_per_kwh_net": _num(spec.get("other_per_kwh_net", 0) or 0, f"{tid}: pozostałe opłaty zmienne"),
            "fixed_monthly_net": _num(spec.get("fixed_monthly_net", 0) or 0, f"{tid}: opłaty stałe"),
        }
        if variant:
            entry["variant"] = variant
        if spec.get("vat_percent") not in (None, ""):
            entry["vat_percent"] = _num(spec["vat_percent"], f"{tid}: VAT", 0, 100)
        tariffs[tid] = entry

    meters_in = data.get("meters") or []
    if not isinstance(meters_in, list):
        raise ValueError("meters: oczekiwano listy")
    meters, seen = [], set()
    for i, m in enumerate(meters_in):
        if not isinstance(m, dict):
            raise ValueError(f"Licznik #{i + 1}: oczekiwano obiektu")
        name = str(m.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError(f"Licznik #{i + 1}: nazwa - litery, cyfry, _ i - (max 40 znaków)")
        if name in seen:
            raise ValueError(f"Zduplikowana nazwa licznika: '{name}'")
        seen.add(name)
        sensor = str(m.get("sensor") or "").strip()
        if not sensor:
            raise ValueError(f"Licznik '{name}': wskaż czujnik z historii (narastający licznik kWh)")
        tid = str(m.get("tariff") or "").strip()
        if tid not in tariffs:
            raise ValueError(f"Licznik '{name}': taryfa '{tid}' nie jest skonfigurowana (dodaj ją z cenami)")
        raw_scale = m.get("scale")
        scale = _num(1 if raw_scale in (None, "") else raw_scale, f"Licznik '{name}': mnożnik", 1e-9)
        meters.append({"name": name, "label": str(m.get("label") or name).strip(), "sensor": sensor,
                       "tariff": tid, "scale": scale})

    return {"vat_percent": vat, "max_gap_minutes": max_gap, "custom_tariffs": custom,
            "tariffs": tariffs, "meters": meters}


def daily_breakdown(readings: list[dict], tariff: Tariff, pricing: dict, ts_from: float, ts_to: float,
                    max_gap_seconds: float, scale: float, default_vat_percent: float = 23.0) -> list[dict]:
    """Zużycie (kWh wg stref) i koszt brutto per doba lokalna (Europe/Warsaw).
    Pierwsza i ostatnia doba bywają niepełne (okres przycięty). Opłata stała jest
    rozliczana proporcjonalnie, tak samo jak w całym raporcie."""
    days = []
    d = datetime.fromtimestamp(ts_from, TZ).date()
    last = datetime.fromtimestamp(ts_to, TZ).date()
    pts = sorted(readings, key=lambda r: r["t"])
    while d <= last:
        d0 = max(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp(), ts_from)
        nxt = d + timedelta(days=1)
        d1 = min(datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ).timestamp(), ts_to)
        if d1 > d0:
            sub = [r for r in pts if d0 - max_gap_seconds <= r["t"] <= d1 + max_gap_seconds]
            cons = consumption_by_zone(sub, tariff, d0, d1, max_gap_seconds=max_gap_seconds, scale=scale)
            res = compute_cost(cons, pricing, tariff, d0, d1, default_vat_percent=default_vat_percent)
            days.append({"date": d.isoformat(), "from": d0, "to": d1,
                         "kwh_by_zone": {z["zone"]: z["kwh"] for z in res["zones"]},
                         "total_kwh": res["total_kwh"], "cost_gross": res["total_gross"]})
        d = nxt
    return days


def build_report(energy_cfg: dict, meter: dict, readings: list[dict], ts_from: float, ts_to: float) -> dict:
    """Koszt wg taryfy przypisanej do licznika + porównanie ze WSZYSTKIMI
    skonfigurowanymi taryfami na tych samych odczytach ("ile bym zapłacił w G12w?")."""
    custom = energy_cfg.get("custom_tariffs") or {}
    vat = float(energy_cfg.get("vat_percent", 23))
    max_gap = float(energy_cfg.get("max_gap_minutes", 360)) * 60
    scale = float(meter.get("scale", 1) or 1)

    def one(tid: str) -> dict:
        pricing = energy_cfg["tariffs"][tid]
        tariff = resolve_tariff(tid, pricing.get("variant"), custom)
        cons = consumption_by_zone(readings, tariff, ts_from, ts_to, max_gap_seconds=max_gap, scale=scale)
        return compute_cost(cons, pricing, tariff, ts_from, ts_to, default_vat_percent=vat)

    report = one(meter["tariff"])
    pricing = energy_cfg["tariffs"][meter["tariff"]]
    daily = daily_breakdown(readings, resolve_tariff(meter["tariff"], pricing.get("variant"), custom), pricing,
                            ts_from, ts_to, max_gap_seconds=max_gap, scale=scale, default_vat_percent=vat)
    comparison = []
    for tid in energy_cfg["tariffs"]:
        try:
            r = one(tid) if tid != meter["tariff"] else report
            comparison.append({"tariff": tid, "total_gross": r["total_gross"], "total_net": r["total_net"],
                               "missing_prices": r["missing_prices"], "active": tid == meter["tariff"]})
        except ValueError as exc:
            comparison.append({"tariff": tid, "error": str(exc), "active": tid == meter["tariff"]})
    return {"meter": {k: meter[k] for k in ("name", "label", "sensor", "tariff")},
            "period": {"from": ts_from, "to": ts_to}, "result": report, "comparison": comparison, "daily": daily}
