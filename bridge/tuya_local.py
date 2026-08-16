"""Lokalne sterowanie przełącznikami/gniazdkami/ściemniaczami Tuya (tinytuya).

Działa całkowicie w sieci LAN, bez zależności od chmury Tuya - dobre dla
RPi Zero W (mało zasobów, nie chcemy zależności od internetu dla
podstawowego sterowania światłem/gniazdkiem).
"""
from __future__ import annotations

import asyncio
import logging

import tinytuya

logger = logging.getLogger("bridge.tuya_local")


def scan_network(maxretry: int = 1) -> list[dict]:
    """Skanuje sieć LAN (broadcast UDP) w poszukiwaniu urządzeń Tuya i
    zwraca listę {id, ip, version} dla znalezionych urządzeń - podpowiedź
    do panelu (/config), żeby nie trzeba było ręcznie szukać adresu IP.

    WAŻNE: broadcast Tuya NIE zawiera `local_key` (nie jest rozgłaszany ze
    względów bezpieczeństwa) ani nazwy urządzenia - to wciąż trzeba
    wyciągnąć osobno narzędziem `python -m tinytuya wizard` (patrz README).
    Skan trwa ok. `tinytuya.SCANTIME` sekund na każde `maxretry` (domyślnie
    ~18s) - synchroniczne i blokujące, celowo (wywoływane z endpointu
    Flask, nie z głównej pętli bridge'a)."""
    raw = tinytuya.deviceScan(verbose=False, maxretry=maxretry, poll=False)
    devices = []
    for ip, info in raw.items():
        devices.append({
            "id": info.get("gwId") or info.get("id") or "",
            "ip": ip,
            "version": info.get("version", ""),
        })
    return devices


class TuyaLocalController:
    def __init__(self, devices_cfg: dict, retries: int = 2, retry_delay: float = 1.5):
        self.devices_cfg = devices_cfg
        self.retries = retries
        self.retry_delay = retry_delay
        self._devices: dict[str, tinytuya.Device] = {}
        for name, cfg in devices_cfg.items():
            dev = tinytuya.OutletDevice(cfg["id"], cfg["ip"], cfg["local_key"])
            dev.set_version(cfg.get("version", 3.4))
            self._devices[name] = dev

    async def _call_with_retry(self, device_name: str, func, *args):
        """Lokalne urządzenia Tuya po WiFi bywają chwilowo nieosiągalne
        (słaby zasięg, przeciążenie sieci) - tinytuya często nie rzuca
        wyjątku w takiej sytuacji, tylko zwraca słownik z kluczem 'Error'.
        Dlatego sprawdzamy oba przypadki i ponawiamy próbę."""
        loop = asyncio.get_event_loop()
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                result = await loop.run_in_executor(None, func, *args)
                if isinstance(result, dict) and "Error" in result:
                    last_error = result["Error"]
                    raise RuntimeError(last_error)
                return result
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    logger.warning(
                        "Tuya '%s': błąd komunikacji (próba %s/%s): %s - ponawiam",
                        device_name, attempt + 1, self.retries + 1, exc,
                    )
                    await asyncio.sleep(self.retry_delay)
        logger.error("Tuya '%s': wyczerpano próby (%s), ostatni błąd: %s", device_name, self.retries + 1, last_error)
        raise ConnectionError(f"Nie udało się skomunikować z urządzeniem Tuya '{device_name}': {last_error}")

    async def turn_on(self, device_name: str, dimmer_level_percent: int | None = None) -> None:
        cfg = self.devices_cfg[device_name]
        dev = self._devices[device_name]

        if cfg.get("type") == "dimmer" and dimmer_level_percent is not None:
            dp = cfg["dimmer_dp"]
            lo, hi = cfg.get("dimmer_range", [10, 1000])
            value = int(lo + (hi - lo) * (dimmer_level_percent / 100))
            await self._call_with_retry(device_name, dev.set_status, True, "1")  # włącz
            await self._call_with_retry(device_name, dev.set_value, dp, value)
            logger.info("Ustawiono '%s' na %s%% jasności (DP=%s)", device_name, dimmer_level_percent, value)
        else:
            await self._call_with_retry(device_name, dev.turn_on)
            logger.info("Włączono '%s'", device_name)

    async def turn_off(self, device_name: str) -> None:
        dev = self._devices[device_name]
        await self._call_with_retry(device_name, dev.turn_off)
        logger.info("Wyłączono '%s'", device_name)

    async def get_status(self, device_name: str) -> dict:
        """Zwraca surowy słownik DP (data points) urządzenia, np.
        {'1': True, '3': 850} - '1' to zwykle stan on/off, reszta zależy
        od typu urządzenia (jasność ściemniacza itp.)."""
        dev = self._devices[device_name]
        result = await self._call_with_retry(device_name, dev.status)
        return result.get("dps", {}) if isinstance(result, dict) else {}

    async def get_all_statuses(self) -> dict[str, dict]:
        """Odpytuje wszystkie skonfigurowane urządzenia po kolei. Sekwencyjnie
        (nie równolegle) - RPi Zero W ma jeden rdzeń, a tinytuya i tak działa
        blokująco w wątku wykonawcy; równoległe odpytanie nie przyspieszy,
        za to zwiększy szczyt zużycia pamięci."""
        results = {}
        for name in self.devices_cfg:
            try:
                results[name] = await self.get_status(name)
            except Exception:
                # get_status już zalogował szczegóły przez _call_with_retry -
                # tu tylko zaznaczamy urządzenie jako niedostępne, nie
                # przerywamy pętli dla pozostałych urządzeń.
                results[name] = None
        return results

    async def turn_on_for_duration(
        self, device_name: str, duration_seconds: int, dimmer_level_percent: int | None = None
    ) -> None:
        """Włącza urządzenie i automatycznie wyłącza po zadanym czasie.
        Uwaga: jeśli w międzyczasie ruch zostanie wykryty ponownie, warto
        w rules_engine.py resetować timer zamiast dopuszczać nakładanie się
        - patrz obsługa w RulesEngine._handle_motion."""
        try:
            await self.turn_on(device_name, dimmer_level_percent)
        except ConnectionError:
            # Nie przerywamy - spróbujemy przynajmniej wyłączyć po czasie,
            # gdyby urządzenie odpowiedziało tylko na pierwszą komendę.
            pass
        await asyncio.sleep(duration_seconds)
        try:
            await self.turn_off(device_name)
        except ConnectionError:
            logger.error("Nie udało się wyłączyć '%s' po czasie - sprawdź urządzenie ręcznie", device_name)
