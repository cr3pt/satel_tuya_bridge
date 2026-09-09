from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger("bridge.weather")

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


async def fetch_forecast(latitude: float, longitude: float, timeout_seconds: float = 10.0) -> dict | None:
    """Pobiera dzisiejszą prognozę opadów dla lokalizacji (Open-Meteo -
    darmowe, bez klucza API). Zwraca {"precipitation_probability_percent":
    int, "precipitation_sum_mm": float} albo None przy błędzie (logowanym,
    nie podnoszonym - wywołujący traktuje to jak chwilową niedostępność,
    tak samo jak inne integracje sieciowe w tym projekcie)."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "precipitation_probability_max,precipitation_sum",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _FORECAST_URL, params=params, timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Pogoda: Open-Meteo zwróciło status %s", resp.status)
                    return None
                data = await resp.json()
    except Exception:
        logger.exception("Pogoda: błąd pobierania prognozy z Open-Meteo")
        return None

    daily = data.get("daily") or {}
    probabilities = daily.get("precipitation_probability_max") or []
    sums = daily.get("precipitation_sum") or []
    if not probabilities or not sums:
        logger.warning("Pogoda: odpowiedź Open-Meteo bez oczekiwanych pól dobowych")
        return None

    return {
        "precipitation_probability_percent": probabilities[0],
        "precipitation_sum_mm": sums[0],
    }
