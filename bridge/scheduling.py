"""Warunki czasowe ("okna") dla reguł automatyki.

Dwa formaty czasu w oknie:
  - stały: "22:00" (HH:MM)
  - względem słońca: "sunset" / "sunrise" (opcjonalnie z przesunięciem
    w minutach, np. "sunset+30" = 30 minut po zachodzie, "sunrise-15" =
    15 minut przed wschodem)

Obliczenia wschodu/zachodu robi biblioteka `astral` - w pełni lokalnie,
bez żadnego zewnętrznego API pogodowego (ważne, bo RPi Zero W nie zawsze
ma stabilny dostęp do internetu, a to nie powinno blokować podstawowej
automatyki domu).

Przykład w config.yaml:
  location:
    latitude: 52.23
    longitude: 21.01

  reguły_ruchu:
    - ...
      time_window:
        start: "sunset-15"
        end: "23:30"
"""
from __future__ import annotations

import datetime as dt
import logging
import re

logger = logging.getLogger("bridge.scheduling")

_SUN_SPEC_RE = re.compile(r"^(sunset|sunrise)([+-]\d+)?$")


def is_within_window(window: dict | None, location_cfg: dict | None, now: dt.datetime | None = None) -> bool:
    """Zwraca True, jeśli `now` (domyślnie bieżący czas) mieści się w oknie.
    Brak okna (None) = zawsze aktywne (reguła bez ograniczenia czasowego)."""
    if not window:
        return True
    now = now or dt.datetime.now()
    try:
        start = _resolve_time(window.get("start"), location_cfg, now)
        end = _resolve_time(window.get("end"), location_cfg, now)
    except Exception:
        logger.exception("Błąd obliczania okna czasowego %s - traktuję regułę jako zawsze aktywną", window)
        return True
    if start is None or end is None:
        return True

    current = now.time()
    if start <= end:
        return start <= current <= end
    else:
        # okno przechodzące przez północ, np. 22:00 -> 06:00
        return current >= start or current <= end


def _resolve_time(spec: str | None, location_cfg: dict | None, now: dt.datetime) -> dt.time | None:
    if spec is None:
        return None

    match = _SUN_SPEC_RE.match(spec)
    if match:
        if not location_cfg or "latitude" not in location_cfg or "longitude" not in location_cfg:
            raise ValueError(
                "Okno czasowe używa 'sunset'/'sunrise', ale w config.yaml brakuje "
                "sekcji 'location' (latitude/longitude)"
            )
        from astral import LocationInfo
        from astral.sun import sun

        event, offset_str = match.group(1), match.group(2)
        loc = LocationInfo(latitude=location_cfg["latitude"], longitude=location_cfg["longitude"])
        sun_times = sun(loc.observer, date=now.date())
        event_dt = sun_times[event]
        if offset_str:
            event_dt += dt.timedelta(minutes=int(offset_str))
        # astral zwraca czas w UTC - konwertujemy do lokalnej strefy systemu
        local_dt = event_dt.astimezone()
        return local_dt.time()

    # format stały "HH:MM"
    h, m = spec.split(":")
    return dt.time(hour=int(h), minute=int(m))
