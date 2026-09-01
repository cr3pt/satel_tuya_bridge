"""Sterowanie urządzeniami Tuya przez Tuya Cloud (REST OpenAPI, przez
`tinytuya.Cloud`) - fallback dla urządzeń, dla których nie udało się (albo
nie próbowano) skonfigurować sterowania lokalnego (patrz TuyaLocalController
w bridge/tuya_local.py) - typowo dlatego, że Tuya Cloud nie zwróciła
local_key, albo urządzenie nie jest w tej samej sieci LAN co bridge.

WOLNIEJSZE i zależne od internetu/dostępności chmury Tuya w odróżnieniu od
sterowania lokalnego - używane WYŁĄCZNIE gdy urządzenie nie ma wpisu w
tuya.devices (lokalne), patrz RulesEngine._build_tuya_controller.

Na start CELOWO ograniczone do prostych przełączników on/off (bez
ściemniania) - różne kategorie urządzeń Tuya mają różne kody DP (data
point), np. "switch_1" dla większości gniazdek/przełączników, ale inne dla
np. lamp - stąd `switch_code` konfigurowalny per urządzenie (domyślnie
"switch_1"), zamiast prób automatycznego wykrywania."""
from __future__ import annotations

import asyncio
import logging

import tinytuya

logger = logging.getLogger("bridge.tuya_cloud")


class TuyaCloudController:
    def __init__(self, devices_cfg: dict, credentials: dict, retries: int = 2, retry_delay: float = 1.5):
        """`devices_cfg`: nazwa -> {id, category, switch_code} (patrz
        dashboard/setup.py: _build_cloud_devices/cloud_devices_to_rows).
        `credentials`: {api_region, access_id, access_key} - TE SAME
        poświadczenia projektu Tuya IoT co import urządzeń/kamer Pulsar
        (zakładka Tuya Cloud, plugins/tuya_cloud/routes.py)."""
        self.devices_cfg = devices_cfg
        self.credentials = credentials
        self.retries = retries
        self.retry_delay = retry_delay
        self._cloud: tinytuya.Cloud | None = None

    def _client(self) -> tinytuya.Cloud:
        # Leniwie tworzony i CACHE'OWANY na obiekcie - logowanie do Tuya
        # Cloud (uzyskanie tokenu) to osobne zapytanie sieciowe, a
        # tinytuya.Cloud sam odświeża token przy wygaśnięciu (patrz
        # _tuyaplatform w bibliotece), więc nie ma powodu tworzyć nowego
        # klienta przy każdej komendzie.
        if self._cloud is None:
            # Domyślnie "eu" (nie ""), ten sam fallback co reszta configu
            # (dashboard/setup.py, plugins/tuya_cloud/routes.py) - pusty
            # region Tuya OpenAPI odrzuca błędem "cross-region access is
            # not allowed" niezależnie od faktycznego regionu konta, więc
            # brak tego pola w już zapisanym config.yaml (starsze instalacje
            # sprzed naprawienia tego bugu w kreatorze) samo się naprawia
            # po aktualizacji, bez wymuszania ręcznego zapisu w /plugins/tuya_cloud.
            self._cloud = tinytuya.Cloud(
                apiRegion=self.credentials.get("api_region") or "eu",
                apiKey=self.credentials.get("access_id", ""),
                apiSecret=self.credentials.get("access_key", ""),
            )
        return self._cloud

    async def _call_with_retry(self, device_name: str, func, *args):
        """Analogicznie do TuyaLocalController._call_with_retry - tinytuya
        (i tu, i tam) nie zawsze rzuca wyjątek przy błędzie, częściej zwraca
        słownik z kluczem 'Error' (patrz tinytuya.error_json)."""
        loop = asyncio.get_event_loop()
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                result = await loop.run_in_executor(None, func, *args)
                if isinstance(result, dict) and "Error" in result:
                    last_error = result.get("Payload") or result["Error"]
                    raise RuntimeError(last_error)
                return result
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    logger.warning(
                        "Tuya Cloud '%s': błąd komunikacji (próba %s/%s): %s - ponawiam",
                        device_name, attempt + 1, self.retries + 1, exc,
                    )
                    await asyncio.sleep(self.retry_delay)
        logger.error(
            "Tuya Cloud '%s': wyczerpano próby (%s), ostatni błąd: %s",
            device_name, self.retries + 1, last_error,
        )
        raise ConnectionError(f"Nie udało się wysłać komendy do Tuya Cloud dla '{device_name}': {last_error}")

    async def turn_on(self, device_name: str, dimmer_level_percent: int | None = None) -> None:
        # dimmer_level_percent celowo ignorowany - patrz docstring modułu.
        cfg = self.devices_cfg[device_name]
        client = self._client()
        commands = {"commands": [{"code": cfg.get("switch_code") or "switch_1", "value": True}]}
        await self._call_with_retry(device_name, client.sendcommand, cfg["id"], commands)
        logger.info("Włączono '%s' przez Tuya Cloud", device_name)

    async def turn_off(self, device_name: str) -> None:
        cfg = self.devices_cfg[device_name]
        client = self._client()
        commands = {"commands": [{"code": cfg.get("switch_code") or "switch_1", "value": False}]}
        await self._call_with_retry(device_name, client.sendcommand, cfg["id"], commands)
        logger.info("Wyłączono '%s' przez Tuya Cloud", device_name)

    async def get_status(self, device_name: str) -> dict:
        """Zwraca {'1': True/False} - CELOWO ten sam kształt klucza co
        TuyaLocalController.get_status (tam DP '1' to konwencjonalny stan
        on/off lokalnego protokołu Tuya) - dzięki temu
        RulesEngine._tuya_status_poll_loop (dps.get("1")) działa NIEZMIENIONE
        dla obu kontrolerów, mimo że Tuya OpenAPI faktycznie zwraca kod
        tekstowy (np. "switch_1"), nie numeryczny."""
        cfg = self.devices_cfg[device_name]
        client = self._client()
        result = await self._call_with_retry(device_name, client.getstatus, cfg["id"])
        items = result.get("result", []) if isinstance(result, dict) else []
        switch_code = cfg.get("switch_code") or "switch_1"
        for item in items:
            if item.get("code") == switch_code:
                return {"1": item.get("value")}
        return {}

    async def get_all_statuses(self) -> dict[str, dict | None]:
        """Odpytuje wszystkie skonfigurowane urządzenia RÓWNOLEGLE -
        analogicznie do TuyaLocalController.get_all_statuses."""
        names = list(self.devices_cfg)
        results_list = await asyncio.gather(
            *(self.get_status(name) for name in names), return_exceptions=True,
        )
        results: dict[str, dict | None] = {}
        for name, result in zip(names, results_list):
            results[name] = None if isinstance(result, Exception) else result
        return results

    async def turn_on_for_duration(
        self, device_name: str, duration_seconds: int, dimmer_level_percent: int | None = None
    ) -> None:
        try:
            await self.turn_on(device_name, dimmer_level_percent)
        except ConnectionError:
            pass
        await asyncio.sleep(duration_seconds)
        try:
            await self.turn_off(device_name)
        except ConnectionError:
            logger.error(
                "Nie udało się wyłączyć '%s' po czasie (Tuya Cloud) - sprawdź urządzenie ręcznie", device_name,
            )
