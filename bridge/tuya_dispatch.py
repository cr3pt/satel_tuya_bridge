from __future__ import annotations

import asyncio

from .tuya_cloud import TuyaCloudController
from .tuya_local import TuyaLocalController


class TuyaController:
    def __init__(self, local: TuyaLocalController, cloud: TuyaCloudController):
        self.local = local
        self.cloud = cloud

    @property
    def devices_cfg(self) -> dict:
        merged = dict(self.cloud.devices_cfg)
        merged.update(self.local.devices_cfg)
        return merged

    def _resolve(self, device_name: str) -> TuyaLocalController | TuyaCloudController:
        if device_name in self.local.devices_cfg:
            return self.local
        if device_name in self.cloud.devices_cfg:
            return self.cloud
        raise KeyError(f"Nieznane urządzenie Tuya: '{device_name}'")

    async def turn_on(self, device_name: str, dimmer_level_percent: int | None = None) -> None:
        await self._resolve(device_name).turn_on(device_name, dimmer_level_percent)

    async def turn_off(self, device_name: str) -> None:
        await self._resolve(device_name).turn_off(device_name)

    async def toggle(self, device_name: str) -> None:
        await self._resolve(device_name).toggle(device_name)

    async def set_dp(self, device_name: str, dp_code: str, value: object) -> None:
        await self._resolve(device_name).set_dp(device_name, dp_code, value)

    async def get_status(self, device_name: str) -> dict:
        return await self._resolve(device_name).get_status(device_name)

    async def get_all_statuses(self) -> dict[str, dict | None]:
        local_result, cloud_result = await asyncio.gather(
            self.local.get_all_statuses(), self.cloud.get_all_statuses(),
        )
        return {**cloud_result, **local_result}

    async def turn_on_for_duration(
        self, device_name: str, duration_seconds: int, dimmer_level_percent: int | None = None
    ) -> None:
        await self._resolve(device_name).turn_on_for_duration(device_name, duration_seconds, dimmer_level_percent)
