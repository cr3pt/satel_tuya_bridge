"""Rozdziela sterowanie urządzeniami Tuya między TuyaLocalController
(bridge/tuya_local.py) i TuyaCloudController (bridge/tuya_cloud.py), w
zależności od tego, gdzie dane urządzenie (po NAZWIE) jest skonfigurowane -
patrz RulesEngine._build_tuya_controller.

LOKALNE ZAWSZE ma pierwszeństwo (szybsze, nie zależy od internetu/chmury
Tuya) - sterowanie chmurowe to fallback TYLKO dla urządzeń, których nie ma
w ogóle w konfiguracji lokalnej (`tuya.devices`). To samo urządzenie pod tą
samą nazwą skonfigurowane w OBU miejscach naraz "wygrywa" lokalnie -
dokładnie tak, jak opisano w zgłoszeniu: "sterowanie przez Tuya Cloud tak
długo, jak nie uda się skonfigurować Tuya Local dla danego urządzenia".

Implementuje TEN SAM publiczny interfejs co oba kontrolery (turn_on/
turn_off/get_status/get_all_statuses/turn_on_for_duration/devices_cfg), więc
RulesEngine/SceneRunner/BoilerAutomation używają go bez żadnych zmian, jak
gdyby to był zwykły TuyaLocalController."""
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
        """Suma nazw z OBU kontrolerów - potrzebne przez
        RulesEngine._seed_tuya_status_labels()/_tuya_status_poll_loop, żeby
        panel pokazywał WSZYSTKIE skonfigurowane urządzenia (lokalne i
        chmurowe), nie tylko lokalne. Lokalne nadpisuje chmurowe przy
        kolizji nazw - ten sam porządek pierwszeństwa co w _resolve()."""
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

    async def get_status(self, device_name: str) -> dict:
        return await self._resolve(device_name).get_status(device_name)

    async def get_all_statuses(self) -> dict[str, dict | None]:
        """Łączy wyniki z OBU kontrolerów (odpytywanych RÓWNOLEGLE)."""
        local_result, cloud_result = await asyncio.gather(
            self.local.get_all_statuses(), self.cloud.get_all_statuses(),
        )
        return {**cloud_result, **local_result}

    async def turn_on_for_duration(
        self, device_name: str, duration_seconds: int, dimmer_level_percent: int | None = None
    ) -> None:
        await self._resolve(device_name).turn_on_for_duration(device_name, duration_seconds, dimmer_level_percent)
