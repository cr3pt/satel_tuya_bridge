from __future__ import annotations

import asyncio
import logging
import zlib

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import (
    CATEGORY_ALARM_SYSTEM, CATEGORY_DOOR_LOCK, CATEGORY_FAN, CATEGORY_GARAGE_DOOR_OPENER,
    CATEGORY_LIGHTBULB, CATEGORY_OUTLET, CATEGORY_SENSOR, CATEGORY_SPRINKLER, CATEGORY_SWITCH,
    CATEGORY_WINDOW_COVERING,
)

from .tuya_local import detect_tuya_sensor_type

logger = logging.getLogger("bridge.homekit")

SECURITY_STATE_AWAY_ARM = 1
SECURITY_STATE_DISARMED = 3
SECURITY_STATE_TRIGGERED = 4

_DOOR_OPEN = 0
_DOOR_CLOSED = 1
_POSITION_STOPPED = 2

_CONTACT_DETECTED = 0
_CONTACT_NOT_DETECTED = 1
_LOW_BATTERY_THRESHOLD = 20

_DEFAULT_PORT = 51826
_SCENE_RESET_DELAY_SECONDS = 1.0


def _stable_aid(key: str) -> int:
    aid = 2 + (zlib.crc32(key.encode()) % 900000)
    return aid + 1 if aid == 7 else aid


def _make_on_off_setter(tuya, loop, name: str):
    def _setter(value: bool) -> None:
        coro = tuya.turn_on(name, reason="HomeKit") if value else tuya.turn_off(name, reason="HomeKit")
        loop.create_task(coro)
    return _setter


def _build_switch(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("Switch")
    on_char = service.get_characteristic("On")
    on_char.set_value(False)
    on_char.setter_callback = _make_on_off_setter(tuya, loop, name)
    return {"on": on_char}


def _apply_switch(chars: dict, is_on: bool, percent: int | None) -> None:
    chars["on"].set_value(is_on)


def _build_outlet(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("Outlet")
    on_char = service.get_characteristic("On")
    inuse_char = service.get_characteristic("OutletInUse")
    on_char.set_value(False)
    inuse_char.set_value(False)
    on_char.setter_callback = _make_on_off_setter(tuya, loop, name)
    return {"on": on_char, "inuse": inuse_char}


def _apply_outlet(chars: dict, is_on: bool, percent: int | None) -> None:
    chars["on"].set_value(is_on)
    chars["inuse"].set_value(is_on)


def _build_lightbulb(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("Lightbulb", chars=["Brightness"] if is_dimmer else None)
    on_char = service.get_characteristic("On")
    on_char.set_value(False)
    on_char.setter_callback = _make_on_off_setter(tuya, loop, name)
    chars = {"on": on_char}
    if is_dimmer:
        level_char = service.get_characteristic("Brightness")
        level_char.set_value(100)

        def _level_setter(value: int) -> None:
            loop.create_task(tuya.turn_on(name, dimmer_level_percent=value, reason="HomeKit"))

        level_char.setter_callback = _level_setter
        chars["level"] = level_char
    return chars


def _apply_lightbulb(chars: dict, is_on: bool, percent: int | None) -> None:
    chars["on"].set_value(is_on)
    if percent is not None and "level" in chars:
        chars["level"].set_value(percent)


def _build_fan(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("Fan", chars=["RotationSpeed"] if is_dimmer else None)
    on_char = service.get_characteristic("On")
    on_char.set_value(False)
    on_char.setter_callback = _make_on_off_setter(tuya, loop, name)
    chars = {"on": on_char}
    if is_dimmer:
        level_char = service.get_characteristic("RotationSpeed")
        level_char.set_value(100)

        def _level_setter(value: float) -> None:
            loop.create_task(tuya.turn_on(name, dimmer_level_percent=round(value), reason="HomeKit"))

        level_char.setter_callback = _level_setter
        chars["level"] = level_char
    return chars


_apply_fan = _apply_lightbulb


def _build_garage_door(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("GarageDoorOpener")
    current_char = service.get_characteristic("CurrentDoorState")
    target_char = service.get_characteristic("TargetDoorState")
    obstruction_char = service.get_characteristic("ObstructionDetected")
    current_char.set_value(_DOOR_CLOSED)
    target_char.set_value(_DOOR_CLOSED)
    obstruction_char.set_value(False)

    def _setter(value: int) -> None:
        coro = tuya.turn_on(name, reason="HomeKit") if value == _DOOR_OPEN else tuya.turn_off(name, reason="HomeKit")
        loop.create_task(coro)

    target_char.setter_callback = _setter
    return {"current": current_char, "target": target_char}


def _apply_garage_door(chars: dict, is_on: bool, percent: int | None) -> None:
    state = _DOOR_OPEN if is_on else _DOOR_CLOSED
    chars["current"].set_value(state)
    chars["target"].set_value(state)


def _build_window_covering(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("WindowCovering")
    current_char = service.get_characteristic("CurrentPosition")
    target_char = service.get_characteristic("TargetPosition")
    state_char = service.get_characteristic("PositionState")
    current_char.set_value(0)
    target_char.set_value(0)
    state_char.set_value(_POSITION_STOPPED)

    def _setter(value: int) -> None:
        coro = tuya.turn_off(name, reason="HomeKit") if value <= 0 else tuya.turn_on(name, dimmer_level_percent=value, reason="HomeKit")
        loop.create_task(coro)

    target_char.setter_callback = _setter
    return {"current": current_char, "target": target_char, "state": state_char}


def _apply_window_covering(chars: dict, is_on: bool, percent: int | None) -> None:
    position = percent if percent is not None else (100 if is_on else 0)
    chars["current"].set_value(position)
    chars["target"].set_value(position)
    chars["state"].set_value(_POSITION_STOPPED)


def _build_valve(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("Valve")
    active_char = service.get_characteristic("Active")
    inuse_char = service.get_characteristic("InUse")
    type_char = service.get_characteristic("ValveType")
    active_char.set_value(0)
    inuse_char.set_value(0)
    type_char.set_value(0)

    def _setter(value: int) -> None:
        coro = tuya.turn_on(name, reason="HomeKit") if value else tuya.turn_off(name, reason="HomeKit")
        loop.create_task(coro)

    active_char.setter_callback = _setter
    return {"active": active_char, "inuse": inuse_char}


def _apply_valve(chars: dict, is_on: bool, percent: int | None) -> None:
    value = 1 if is_on else 0
    chars["active"].set_value(value)
    chars["inuse"].set_value(value)


def _build_lock(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("LockMechanism")
    current_char = service.get_characteristic("LockCurrentState")
    target_char = service.get_characteristic("LockTargetState")
    current_char.set_value(1)
    target_char.set_value(1)

    def _setter(value: int) -> None:
        coro = tuya.turn_on(name, reason="HomeKit") if value == 1 else tuya.turn_off(name, reason="HomeKit")
        loop.create_task(coro)

    target_char.setter_callback = _setter
    return {"current": current_char, "target": target_char}


def _apply_lock(chars: dict, is_on: bool, percent: int | None) -> None:
    state = 1 if is_on else 0
    chars["current"].set_value(state)
    chars["target"].set_value(state)


def _build_temperature_sensor(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("TemperatureSensor", chars=["StatusLowBattery"])
    temp_char = service.get_characteristic("CurrentTemperature")
    battery_char = service.get_characteristic("StatusLowBattery")
    temp_char.set_value(20.0)
    battery_char.set_value(0)
    return {"temperature": temp_char, "low_battery": battery_char}


def _apply_temperature_sensor(chars: dict, readings: dict) -> None:
    if "temperature_celsius" in readings:
        chars["temperature"].set_value(readings["temperature_celsius"])
    if "battery_percent" in readings:
        chars["low_battery"].set_value(1 if readings["battery_percent"] < _LOW_BATTERY_THRESHOLD else 0)


def _build_humidity_sensor(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("HumiditySensor", chars=["StatusLowBattery"])
    humidity_char = service.get_characteristic("CurrentRelativeHumidity")
    battery_char = service.get_characteristic("StatusLowBattery")
    humidity_char.set_value(50.0)
    battery_char.set_value(0)
    return {"humidity": humidity_char, "low_battery": battery_char}


def _apply_humidity_sensor(chars: dict, readings: dict) -> None:
    if "humidity_percent" in readings:
        chars["humidity"].set_value(readings["humidity_percent"])
    if "battery_percent" in readings:
        chars["low_battery"].set_value(1 if readings["battery_percent"] < _LOW_BATTERY_THRESHOLD else 0)


def _build_temp_humidity_sensor(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    temp_service = acc.add_preload_service("TemperatureSensor", chars=["StatusLowBattery"])
    humidity_service = acc.add_preload_service("HumiditySensor", chars=["StatusLowBattery"])
    temp_char = temp_service.get_characteristic("CurrentTemperature")
    humidity_char = humidity_service.get_characteristic("CurrentRelativeHumidity")
    temp_low_battery_char = temp_service.get_characteristic("StatusLowBattery")
    humidity_low_battery_char = humidity_service.get_characteristic("StatusLowBattery")
    temp_char.set_value(20.0)
    humidity_char.set_value(50.0)
    temp_low_battery_char.set_value(0)
    humidity_low_battery_char.set_value(0)
    return {
        "temperature": temp_char, "humidity": humidity_char,
        "low_battery": temp_low_battery_char, "low_battery_2": humidity_low_battery_char,
    }


def _apply_temp_humidity_sensor(chars: dict, readings: dict) -> None:
    if "temperature_celsius" in readings:
        chars["temperature"].set_value(readings["temperature_celsius"])
    if "humidity_percent" in readings:
        chars["humidity"].set_value(readings["humidity_percent"])
    if "battery_percent" in readings:
        low = 1 if readings["battery_percent"] < _LOW_BATTERY_THRESHOLD else 0
        chars["low_battery"].set_value(low)
        chars["low_battery_2"].set_value(low)


def _build_leak_sensor(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("LeakSensor", chars=["StatusLowBattery"])
    leak_char = service.get_characteristic("LeakDetected")
    battery_char = service.get_characteristic("StatusLowBattery")
    leak_char.set_value(0)
    battery_char.set_value(0)
    return {"leak": leak_char, "low_battery": battery_char}


def _apply_leak_sensor(chars: dict, readings: dict) -> None:
    if "leak_detected" in readings:
        chars["leak"].set_value(1 if readings["leak_detected"] else 0)
    if "battery_percent" in readings:
        chars["low_battery"].set_value(1 if readings["battery_percent"] < _LOW_BATTERY_THRESHOLD else 0)


def _build_contact_sensor(acc, tuya, loop, name: str, is_dimmer: bool) -> dict:
    service = acc.add_preload_service("ContactSensor", chars=["StatusLowBattery"])
    contact_char = service.get_characteristic("ContactSensorState")
    battery_char = service.get_characteristic("StatusLowBattery")
    contact_char.set_value(_CONTACT_DETECTED)
    battery_char.set_value(0)
    return {"contact": contact_char, "low_battery": battery_char}


def _apply_contact_sensor(chars: dict, readings: dict) -> None:
    if "contact_open" in readings:
        chars["contact"].set_value(_CONTACT_NOT_DETECTED if readings["contact_open"] else _CONTACT_DETECTED)
    if "battery_percent" in readings:
        chars["low_battery"].set_value(1 if readings["battery_percent"] < _LOW_BATTERY_THRESHOLD else 0)


TUYA_ACCESSORY_TYPES: dict[str, dict] = {
    "switch": {"label": "Włącznik", "category": CATEGORY_SWITCH, "build": _build_switch, "apply": _apply_switch},
    "outlet": {"label": "Gniazdko", "category": CATEGORY_OUTLET, "build": _build_outlet, "apply": _apply_outlet},
    "lightbulb": {"label": "Światło", "category": CATEGORY_LIGHTBULB, "build": _build_lightbulb, "apply": _apply_lightbulb},
    "fan": {"label": "Wentylator", "category": CATEGORY_FAN, "build": _build_fan, "apply": _apply_fan},
    "garage_door": {"label": "Brama garażowa", "category": CATEGORY_GARAGE_DOOR_OPENER, "build": _build_garage_door, "apply": _apply_garage_door},
    "window_covering": {"label": "Roleta / markiza", "category": CATEGORY_WINDOW_COVERING, "build": _build_window_covering, "apply": _apply_window_covering},
    "valve": {"label": "Zawór (nawadnianie)", "category": CATEGORY_SPRINKLER, "build": _build_valve, "apply": _apply_valve},
    "lock": {"label": "Zamek", "category": CATEGORY_DOOR_LOCK, "build": _build_lock, "apply": _apply_lock},
    "temperature_sensor": {
        "label": "Czujnik temperatury", "category": CATEGORY_SENSOR, "is_sensor": True,
        "build": _build_temperature_sensor, "apply": _apply_temperature_sensor,
    },
    "humidity_sensor": {
        "label": "Czujnik wilgotności", "category": CATEGORY_SENSOR, "is_sensor": True,
        "build": _build_humidity_sensor, "apply": _apply_humidity_sensor,
    },
    "temp_humidity_sensor": {
        "label": "Czujnik temperatury i wilgotności", "category": CATEGORY_SENSOR, "is_sensor": True,
        "build": _build_temp_humidity_sensor, "apply": _apply_temp_humidity_sensor,
    },
    "leak_sensor": {
        "label": "Czujnik zalania", "category": CATEGORY_SENSOR, "is_sensor": True,
        "build": _build_leak_sensor, "apply": _apply_leak_sensor,
    },
    "contact_sensor": {
        "label": "Kontaktron (drzwi/okno)", "category": CATEGORY_SENSOR, "is_sensor": True,
        "build": _build_contact_sensor, "apply": _apply_contact_sensor,
    },
}


def resolve_homekit_config(config_raw: dict) -> dict:
    homekit_cfg = config_raw.get("homekit", {}) or {}
    saved_partitions = homekit_cfg.get("partitions", {}) or {}
    saved_devices = homekit_cfg.get("devices", {}) or {}
    saved_scenes = homekit_cfg.get("scenes", {}) or {}
    rooms = list(homekit_cfg.get("rooms", []) or [])

    partitions = {}
    for name, number in (config_raw.get("satel", {}) or {}).get("partitions", {}).items():
        partitions[name] = {
            "number": number,
            "enabled": bool((saved_partitions.get(name) or {}).get("enabled", True)),
        }

    tuya_cfg = config_raw.get("tuya", {}) or {}
    all_tuya_devices = {**(tuya_cfg.get("cloud_devices", {}) or {}), **(tuya_cfg.get("devices", {}) or {})}
    devices = {}
    for name, device_cfg in all_tuya_devices.items():
        saved = saved_devices.get(name) or {}
        detected_type = detect_tuya_sensor_type(device_cfg)
        type_id = saved["type"] if "type" in saved else (detected_type or "")
        devices[name] = {
            "type": type_id,
            "room": saved.get("room") or "",
            "is_dimmer": (device_cfg or {}).get("type") == "dimmer",
            "detected_type": detected_type,
        }

    scenes = {}
    for scene in config_raw.get("scenes", []) or []:
        name = scene.get("name")
        if not name:
            continue
        scenes[name] = {"enabled": bool((saved_scenes.get(name) or {}).get("enabled", False))}

    return {"rooms": rooms, "partitions": partitions, "devices": devices, "scenes": scenes}


class HomeKitBridge:
    def __init__(self, cfg: dict, satel, tuya, scenes, partitions: dict, devices: dict, scene_names,
                 persist_path, loop=None, history=None):
        self.satel = satel
        self.tuya = tuya
        self.scenes = scenes
        self.history = history
        self._loop = loop or asyncio.get_event_loop()

        self._partition_chars: dict[int, tuple] = {}
        self._tuya_chars: dict[str, dict] = {}
        self._tuya_apply: dict[str, object] = {}
        self._tuya_sensor_chars: dict[str, dict] = {}
        self._tuya_sensor_apply: dict[str, object] = {}

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

        for device_name, device_cfg in devices.items():
            type_id = device_cfg.get("type")
            type_def = TUYA_ACCESSORY_TYPES.get(type_id)
            if type_def is None:
                continue
            room = (device_cfg.get("room") or "").strip()
            display_name = f"{room} {device_name}" if room else device_name
            acc = Accessory(self.driver, display_name)
            acc.category = type_def["category"]
            chars = type_def["build"](acc, self.tuya, self._loop, device_name, bool(device_cfg.get("is_dimmer")))
            acc.aid = _stable_aid(f"tuya:{device_name}")
            bridge.add_accessory(acc)
            if type_def.get("is_sensor"):
                self._tuya_sensor_chars[device_name] = chars
                self._tuya_sensor_apply[device_name] = type_def["apply"]
            else:
                self._tuya_chars[device_name] = chars
                self._tuya_apply[device_name] = type_def["apply"]

        for scene_name in scene_names:
            if scene_name not in scenes.scenes:
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

    def update_tuya_state(self, device_name: str, is_on: bool, percent: int | None = None) -> None:
        apply_fn = self._tuya_apply.get(device_name)
        chars = self._tuya_chars.get(device_name)
        if apply_fn is not None and chars is not None:
            apply_fn(chars, is_on, percent)

    def update_tuya_sensor_state(self, device_name: str, readings: dict) -> None:
        apply_fn = self._tuya_sensor_apply.get(device_name)
        chars = self._tuya_sensor_chars.get(device_name)
        if apply_fn is not None and chars is not None and readings:
            apply_fn(chars, readings)

    async def async_start(self) -> None:
        await self.driver.async_start()
        logger.info(
            "Serwer HomeKit uruchomiony (kod parowania: %s)",
            self.driver.state.pincode.decode(),
        )

    async def async_stop(self) -> None:
        await self.driver.async_stop()
