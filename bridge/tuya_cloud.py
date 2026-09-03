from __future__ import annotations

import asyncio
import logging

import tinytuya

logger = logging.getLogger("bridge.tuya_cloud")


class TuyaCloudController:
    def __init__(self, devices_cfg: dict, credentials: dict, retries: int = 2, retry_delay: float = 1.5):
        self.devices_cfg = devices_cfg
        self.credentials = credentials
        self.retries = retries
        self.retry_delay = retry_delay
        self._cloud: tinytuya.Cloud | None = None

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

    async def turn_on(self, device_name: str, dimmer_level_percent: int | None = None) -> None:
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
        cfg = self.devices_cfg[device_name]
        client = self._client()
        result = await self._call_with_retry(device_name, client.getstatus, cfg["id"])
        items = result.get("result", []) if isinstance(result, dict) else []
        return {item["code"]: item.get("value") for item in items if item.get("code")}

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
