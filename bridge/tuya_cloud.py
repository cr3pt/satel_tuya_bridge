from __future__ import annotations

import asyncio
import logging

import tinytuya

from .tuya_local import coerce_dp_value, find_switch_codes_from_functions_result, is_switch_code

logger = logging.getLogger("bridge.tuya_cloud")

_FALLBACK_SWITCH_CODES = ["switch_1"]


def _reason_suffix(reason: str | None) -> str:
    return f" - {reason}" if reason else ""


class TuyaCloudController:
    def __init__(self, devices_cfg: dict, credentials: dict, retries: int = 2, retry_delay: float = 1.5):
        self.devices_cfg = devices_cfg
        self.credentials = credentials
        self.retries = retries
        self.retry_delay = retry_delay
        self._cloud: tinytuya.Cloud | None = None
        self._switch_codes_cache: dict[str, list[str]] = {}

    def _client(self) -> tinytuya.Cloud:
        if self._cloud is None:
            self._cloud = tinytuya.Cloud(
                apiRegion=self.credentials.get("api_region") or "eu",
                apiKey=self.credentials.get("access_id", ""),
                apiSecret=self.credentials.get("access_key", ""),
            )
        return self._cloud

    async def _call_with_retry(self, device_name: str, func, *args):
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

    async def _detect_switch_codes_from_functions(self, device_name: str) -> list[str] | None:
        cfg = self.devices_cfg[device_name]
        client = self._client()
        getdps = getattr(client, "getdps", None)
        if getdps is None:
            return None
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, getdps, cfg["id"])
        except Exception:
            return None
        codes = find_switch_codes_from_functions_result(result)
        return codes or None

    async def _switch_codes(self, device_name: str) -> list[str]:
        cfg = self.devices_cfg[device_name]
        explicit = (cfg.get("switch_code") or "").strip()
        if explicit:
            return [explicit]
        if device_name in self._switch_codes_cache:
            return self._switch_codes_cache[device_name]
        # Wykrywamy kody wyłącznie z listy "functions" (gwarantowanie zapisywalne DP) -
        # sam status potrafi raportować też kody tylko-do-odczytu, których wysłanie
        # w tej samej komendzie co prawdziwy przełącznik odrzuca całą paczkę (Tuya Cloud
        # zwraca wtedy "command or value not support").
        codes = await self._detect_switch_codes_from_functions(device_name)
        if not codes:
            try:
                status = await self.get_status(device_name)
            except ConnectionError:
                status = {}
            codes = [code for code in status if is_switch_code(code)]
        codes = codes or _FALLBACK_SWITCH_CODES
        self._switch_codes_cache[device_name] = codes
        return codes

    async def turn_on(
        self, device_name: str, dimmer_level_percent: int | None = None, reason: str | None = None,
    ) -> None:
        cfg = self.devices_cfg[device_name]
        client = self._client()
        codes = await self._switch_codes(device_name)
        commands = {"commands": [{"code": code, "value": True} for code in codes]}
        await self._call_with_retry(device_name, client.sendcommand, cfg["id"], commands)
        logger.info(
            "Włączono '%s' przez Tuya Cloud (%s)%s", device_name, ", ".join(codes), _reason_suffix(reason),
        )

    async def turn_off(self, device_name: str, reason: str | None = None) -> None:
        cfg = self.devices_cfg[device_name]
        client = self._client()
        codes = await self._switch_codes(device_name)
        commands = {"commands": [{"code": code, "value": False} for code in codes]}
        await self._call_with_retry(device_name, client.sendcommand, cfg["id"], commands)
        logger.info(
            "Wyłączono '%s' przez Tuya Cloud (%s)%s", device_name, ", ".join(codes), _reason_suffix(reason),
        )

    async def set_dp(self, device_name: str, dp_code: str, value: object, reason: str | None = None) -> None:
        cfg = self.devices_cfg[device_name]
        client = self._client()
        coerced = coerce_dp_value(value)
        commands = {"commands": [{"code": dp_code, "value": coerced}]}
        await self._call_with_retry(device_name, client.sendcommand, cfg["id"], commands)
        logger.info(
            "Ustawiono pole '%s' urządzenia '%s' na %r przez Tuya Cloud%s",
            dp_code, device_name, coerced, _reason_suffix(reason),
        )

    async def get_status(self, device_name: str) -> dict:
        cfg = self.devices_cfg[device_name]
        client = self._client()
        result = await self._call_with_retry(device_name, client.getstatus, cfg["id"])
        items = result.get("result", []) if isinstance(result, dict) else []
        return {item["code"]: item.get("value") for item in items if item.get("code")}

    async def toggle(self, device_name: str, reason: str | None = None) -> None:
        cfg = self.devices_cfg[device_name]
        client = self._client()
        codes = await self._switch_codes(device_name)
        status = await self.get_status(device_name)
        new_value = not bool(status.get(codes[0]))
        commands = {"commands": [{"code": code, "value": new_value} for code in codes]}
        await self._call_with_retry(device_name, client.sendcommand, cfg["id"], commands)
        logger.info(
            "Zmieniono stan '%s' na %s przez Tuya Cloud (%s)%s",
            device_name, new_value, ", ".join(codes), _reason_suffix(reason),
        )

    async def get_all_statuses(self) -> dict[str, dict | None]:
        names = list(self.devices_cfg)
        results_list = await asyncio.gather(
            *(self.get_status(name) for name in names), return_exceptions=True,
        )
        results: dict[str, dict | None] = {}
        for name, result in zip(names, results_list):
            results[name] = None if isinstance(result, Exception) else result
        return results

    async def turn_on_for_duration(
        self, device_name: str, duration_seconds: int, dimmer_level_percent: int | None = None,
        reason: str | None = None,
    ) -> None:
        try:
            await self.turn_on(device_name, dimmer_level_percent, reason=reason)
        except ConnectionError:
            pass
        await asyncio.sleep(duration_seconds)
        try:
            await self.turn_off(device_name, reason=reason)
        except ConnectionError:
            logger.error(
                "Nie udało się wyłączyć '%s' po czasie (Tuya Cloud) - sprawdź urządzenie ręcznie", device_name,
            )
