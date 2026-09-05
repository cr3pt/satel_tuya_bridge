from __future__ import annotations

import asyncio
import logging
import zlib

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_ALARM_SYSTEM, CATEGORY_LIGHTBULB, CATEGORY_SWITCH

logger = logging.getLogger("bridge.homekit")

SECURITY_STATE_AWAY_ARM = 1
SECURITY_STATE_DISARMED = 3
SECURITY_STATE_TRIGGERED = 4

_DEFAULT_PORT = 51826
_SCENE_RESET_DELAY_SECONDS = 1.0


def _stable_aid(key: str) -> int:
    aid = 2 + (zlib.crc32(key.encode()) % 900000)
    return aid + 1 if aid == 7 else aid


class HomeKitBridge:
    def __init__(self, cfg: dict, satel, tuya, scenes, partitions: dict, tuya_devices_cfg: dict,
                 persist_path, loop=None, history=None):
        self.satel = satel
        self.tuya = tuya
        self.scenes = scenes
        self.history = history
        self._loop = loop or asyncio.get_event_loop()

        self._partition_chars: dict[int, tuple] = {}
        self._tuya_on_chars: dict[str, object] = {}
        self._tuya_brightness_chars: dict[str, object] = {}

        pincode = cfg.get("pincode")
        self.driver = AccessoryDriver(
            port=cfg.get("port", _DEFAULT_PORT),
            persist_file=str(persist_path),
            pincode=pincode.encode() if pincode else None,
            loop=self._loop,
        )
        bridge = Bridge(self.driver, cfg.get("bridge_name", "Satel-Tuya-eBUS"))

        for partition_name, partition_number in partitions.items():
            acc = self._build_security_accessory(partition_name, partition_number)
            acc.aid = _stable_aid(f"partition:{partition_number}")
            bridge.add_accessory(acc)

        for device_name, device_cfg in tuya_devices_cfg.items():
            acc = self._build_tuya_accessory(device_name, device_cfg)
            acc.aid = _stable_aid(f"tuya:{device_name}")
            bridge.add_accessory(acc)

        for scene_name, scene in scenes.scenes.items():
            if not scene.get("enabled", True):
                continue
            acc = self._build_scene_accessory(scene_name)
            acc.aid = _stable_aid(f"scene:{scene_name}")
            bridge.add_accessory(acc)

        self.driver.add_accessory(accessory=bridge)
        self.bridge = bridge

    def _build_security_accessory(self, name: str, number: int) -> Accessory:
        acc = Accessory(self.driver, name)
        acc.category = CATEGORY_ALARM_SYSTEM
        service = acc.add_preload_service("SecuritySystem")
        current_char = service.get_characteristic("SecuritySystemCurrentState")
        target_char = service.get_characteristic("SecuritySystemTargetState")
        current_char.set_value(SECURITY_STATE_DISARMED)
        target_char.set_value(SECURITY_STATE_DISARMED)

        def _setter(value: int, partition_number: int = number) -> None:
            if value == SECURITY_STATE_DISARMED:
                coro = self.satel.disarm(partitions=[partition_number])
            else:
                coro = self.satel.arm(mode=0, partitions=[partition_number])
            self._loop.create_task(coro)

        target_char.setter_callback = _setter
        self._partition_chars[number] = (current_char, target_char)
        return acc

    def _build_tuya_accessory(self, name: str, device_cfg: dict) -> Accessory:
        acc = Accessory(self.driver, name)
        is_dimmer = (device_cfg or {}).get("type") == "dimmer"

        if is_dimmer:
            acc.category = CATEGORY_LIGHTBULB
            service = acc.add_preload_service("Lightbulb", chars=["Brightness"])
            on_char = service.get_characteristic("On")
            brightness_char = service.get_characteristic("Brightness")
            brightness_char.set_value(100)

            def _brightness_setter(value: int, device_name: str = name) -> None:
                self._loop.create_task(self.tuya.turn_on(device_name, dimmer_level_percent=value))

            brightness_char.setter_callback = _brightness_setter
            self._tuya_brightness_chars[name] = brightness_char
        else:
            acc.category = CATEGORY_SWITCH
            service = acc.add_preload_service("Switch")
            on_char = service.get_characteristic("On")

        on_char.set_value(False)

        def _on_setter(value: bool, device_name: str = name) -> None:
            coro = self.tuya.turn_on(device_name) if value else self.tuya.turn_off(device_name)
            self._loop.create_task(coro)

        on_char.setter_callback = _on_setter
        self._tuya_on_chars[name] = on_char
        return acc

    def _build_scene_accessory(self, scene_name: str) -> Accessory:
        acc = Accessory(self.driver, scene_name)
        acc.category = CATEGORY_SWITCH
        service = acc.add_preload_service("Switch")
        on_char = service.get_characteristic("On")
        on_char.set_value(False)

        def _setter(value: bool, name: str = scene_name) -> None:
            if not value:
                return

            async def _run() -> None:
                await self.scenes.run(name)
                await asyncio.sleep(_SCENE_RESET_DELAY_SECONDS)
                on_char.set_value(False)

            self._loop.create_task(_run())

        on_char.setter_callback = _setter
        return acc

    def update_partition_state(self, partition_number: int, armed: bool, triggered: bool = False) -> None:
        chars = self._partition_chars.get(partition_number)
        if chars is None:
            return
        current_char, target_char = chars
        current_char.set_value(
            SECURITY_STATE_TRIGGERED if triggered else
            (SECURITY_STATE_AWAY_ARM if armed else SECURITY_STATE_DISARMED)
        )
        target_char.set_value(SECURITY_STATE_AWAY_ARM if armed else SECURITY_STATE_DISARMED)

    def update_tuya_on(self, device_name: str, is_on: bool) -> None:
        char = self._tuya_on_chars.get(device_name)
        if char is not None:
            char.set_value(is_on)

    def update_tuya_brightness(self, device_name: str, percent: int) -> None:
        char = self._tuya_brightness_chars.get(device_name)
        if char is not None:
            char.set_value(percent)

    async def async_start(self) -> None:
        await self.driver.async_start()
        logger.info(
            "Serwer HomeKit uruchomiony (kod parowania: %s)",
            self.driver.state.pincode.decode(),
        )

    async def async_stop(self) -> None:
        await self.driver.async_stop()
