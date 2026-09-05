import asyncio

from bridge.homekit_bridge import HomeKitBridge, _stable_aid, resolve_homekit_config


class FakeSatel:
    def __init__(self):
        self.calls = []

    async def arm(self, mode=0, partitions=None):
        self.calls.append(("arm", mode, tuple(partitions or [])))

    async def disarm(self, partitions=None):
        self.calls.append(("disarm", tuple(partitions or [])))


class FakeTuya:
    def __init__(self):
        self.calls = []

    async def turn_on(self, device, dimmer_level_percent=None):
        self.calls.append(("on", device, dimmer_level_percent))

    async def turn_off(self, device):
        self.calls.append(("off", device))


class FakeScenes:
    def __init__(self, scenes):
        self.scenes = scenes
        self.calls = []

    async def run(self, name):
        self.calls.append(name)


def _device(type_id, room="", is_dimmer=False):
    return {"type": type_id, "room": room, "is_dimmer": is_dimmer}


def _make_bridge(tmp_path, satel=None, tuya=None, scenes=None, partitions=None, devices=None,
                  scene_names=None, cfg=None):
    return HomeKitBridge(
        cfg or {"port": 51826},
        satel or FakeSatel(),
        tuya or FakeTuya(),
        scenes or FakeScenes({}),
        partitions or {},
        devices or {},
        scene_names or set(),
        persist_path=tmp_path / "homekit_state.json",
        loop=asyncio.get_event_loop(),
    )


class TestStableAid:

    async def test_deterministic_for_same_key(self):
        assert _stable_aid("tuya:gniazdko") == _stable_aid("tuya:gniazdko")

    async def test_differs_for_different_keys(self):
        assert _stable_aid("tuya:a") != _stable_aid("tuya:b")

    async def test_never_returns_reserved_values(self):
        for i in range(2000):
            assert _stable_aid(f"key{i}") not in (0, 1, 7)


class TestAccessoryConstruction:

    async def test_builds_one_accessory_per_partition_device_and_enabled_scene(self, tmp_path):
        scenes = FakeScenes({"wieczor": {"actions": []}, "wylaczona": {"actions": []}})
        hk = _make_bridge(
            tmp_path,
            scenes=scenes,
            partitions={"Dom": 1, "Garaz": 2},
            devices={"gniazdko": _device("switch"), "lampka": _device("lightbulb", is_dimmer=True)},
            scene_names={"wieczor"},
        )
        names = {acc.display_name for acc in hk.bridge.accessories.values()}
        assert names == {"Dom", "Garaz", "gniazdko", "lampka", "wieczor"}
        assert "wylaczona" not in names

    async def test_aids_are_stable_across_two_independent_builds(self, tmp_path):
        kwargs = dict(
            partitions={"Dom": 1},
            devices={"gniazdko": _device("switch")},
        )
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        hk1 = _make_bridge(dir_a, **kwargs)
        hk2 = _make_bridge(dir_b, **kwargs)
        aids1 = sorted(acc.aid for acc in hk1.bridge.accessories.values())
        aids2 = sorted(acc.aid for acc in hk2.bridge.accessories.values())
        assert aids1 == aids2

    async def test_device_with_room_prefixes_display_name(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"lampka": _device("lightbulb", room="Salon")})
        names = {acc.display_name for acc in hk.bridge.accessories.values()}
        assert "Salon lampka" in names

    async def test_device_without_room_uses_bare_name(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"lampka": _device("lightbulb")})
        names = {acc.display_name for acc in hk.bridge.accessories.values()}
        assert "lampka" in names

    async def test_unknown_type_is_skipped(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"x": _device("nie_istnieje")})
        assert hk.bridge.accessories == {}

    async def test_dimmer_lightbulb_gets_brightness(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"lampka": _device("lightbulb", is_dimmer=True)})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "lampka")
        service = acc.get_service("Lightbulb")
        assert service is not None
        assert service.get_characteristic("Brightness") is not None

    async def test_non_dimmer_switch_has_plain_switch_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"gniazdko": _device("switch")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "gniazdko")
        assert acc.get_service("Switch") is not None
        assert acc.get_service("Lightbulb") is None

    async def test_outlet_gets_outlet_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"gniazdko": _device("outlet")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "gniazdko")
        assert acc.get_service("Outlet") is not None

    async def test_fan_gets_fan_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"went": _device("fan")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "went")
        assert acc.get_service("Fan") is not None

    async def test_garage_door_gets_garage_door_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"brama": _device("garage_door")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "brama")
        assert acc.get_service("GarageDoorOpener") is not None

    async def test_window_covering_gets_window_covering_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"roleta": _device("window_covering")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "roleta")
        assert acc.get_service("WindowCovering") is not None

    async def test_valve_gets_valve_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"nawadnianie": _device("valve")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "nawadnianie")
        assert acc.get_service("Valve") is not None

    async def test_lock_gets_lock_mechanism_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"zamek": _device("lock")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "zamek")
        assert acc.get_service("LockMechanism") is not None


class TestSecuritySystemSetter:

    async def test_target_state_disarm_calls_satel_disarm(self, tmp_path):
        satel = FakeSatel()
        hk = _make_bridge(tmp_path, satel=satel, partitions={"Dom": 1})
        _, target_char = hk._partition_chars[1]
        target_char.client_update_value(3)
        await asyncio.sleep(0.05)
        assert satel.calls == [("disarm", (1,))]

    async def test_target_state_any_arm_value_calls_satel_arm_mode_0(self, tmp_path):
        satel = FakeSatel()
        hk = _make_bridge(tmp_path, satel=satel, partitions={"Dom": 1})
        _, target_char = hk._partition_chars[1]
        target_char.client_update_value(1)
        await asyncio.sleep(0.05)
        assert satel.calls == [("arm", 0, (1,))]


class TestTuyaSetters:

    async def test_switch_on_off(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"gniazdko": _device("switch")})
        on_char = hk._tuya_chars["gniazdko"]["on"]
        on_char.client_update_value(True)
        await asyncio.sleep(0.05)
        on_char.client_update_value(False)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "gniazdko", None), ("off", "gniazdko")]

    async def test_outlet_on_off(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"gniazdko": _device("outlet")})
        on_char = hk._tuya_chars["gniazdko"]["on"]
        on_char.client_update_value(True)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "gniazdko", None)]

    async def test_lightbulb_brightness_setter_calls_turn_on_with_percent(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"lampka": _device("lightbulb", is_dimmer=True)})
        level_char = hk._tuya_chars["lampka"]["level"]
        level_char.client_update_value(42)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "lampka", 42)]

    async def test_fan_rotation_speed_setter_calls_turn_on_with_rounded_percent(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"went": _device("fan", is_dimmer=True)})
        level_char = hk._tuya_chars["went"]["level"]
        level_char.client_update_value(42.6)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "went", 43)]

    async def test_garage_door_target_open_and_closed(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"brama": _device("garage_door")})
        target_char = hk._tuya_chars["brama"]["target"]
        target_char.client_update_value(0)
        await asyncio.sleep(0.05)
        target_char.client_update_value(1)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "brama", None), ("off", "brama")]

    async def test_window_covering_target_position(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"roleta": _device("window_covering")})
        target_char = hk._tuya_chars["roleta"]["target"]
        target_char.client_update_value(0)
        await asyncio.sleep(0.05)
        target_char.client_update_value(60)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("off", "roleta"), ("on", "roleta", 60)]

    async def test_valve_active_setter(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"nawadnianie": _device("valve")})
        active_char = hk._tuya_chars["nawadnianie"]["active"]
        active_char.client_update_value(1)
        await asyncio.sleep(0.05)
        active_char.client_update_value(0)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "nawadnianie", None), ("off", "nawadnianie")]

    async def test_lock_target_setter(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, devices={"zamek": _device("lock")})
        target_char = hk._tuya_chars["zamek"]["target"]
        target_char.client_update_value(1)
        await asyncio.sleep(0.05)
        target_char.client_update_value(0)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "zamek", None), ("off", "zamek")]


class TestSceneSetter:

    async def test_turning_on_runs_scene_and_resets_to_off(self, tmp_path, monkeypatch):
        import bridge.homekit_bridge as hk_module
        monkeypatch.setattr(hk_module, "_SCENE_RESET_DELAY_SECONDS", 0.01)

        scenes = FakeScenes({"wieczor": {"actions": []}})
        hk = _make_bridge(tmp_path, scenes=scenes, scene_names={"wieczor"})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "wieczor")
        on_char = acc.get_service("Switch").get_characteristic("On")
        on_char.client_update_value(True)
        await asyncio.sleep(0.1)
        assert scenes.calls == ["wieczor"]
        assert on_char.value is False

    async def test_turning_off_does_not_run_scene(self, tmp_path):
        scenes = FakeScenes({"wieczor": {"actions": []}})
        hk = _make_bridge(tmp_path, scenes=scenes, scene_names={"wieczor"})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "wieczor")
        on_char = acc.get_service("Switch").get_characteristic("On")
        on_char.client_update_value(False)
        await asyncio.sleep(0.05)
        assert scenes.calls == []


class TestUpdatePartitionState:

    async def test_armed(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(1, armed=True)
        current_char, target_char = hk._partition_chars[1]
        assert current_char.value == 1
        assert target_char.value == 1

    async def test_disarmed(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(1, armed=False)
        current_char, target_char = hk._partition_chars[1]
        assert current_char.value == 3
        assert target_char.value == 3

    async def test_triggered_overrides_current_only(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(1, armed=True, triggered=True)
        current_char, target_char = hk._partition_chars[1]
        assert current_char.value == 4
        assert target_char.value == 1

    async def test_unknown_partition_is_noop(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(999, armed=True)


class TestUpdateTuyaState:

    async def test_switch_on_off(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"gniazdko": _device("switch")})
        hk.update_tuya_state("gniazdko", True)
        assert hk._tuya_chars["gniazdko"]["on"].value is True
        hk.update_tuya_state("gniazdko", False)
        assert hk._tuya_chars["gniazdko"]["on"].value is False

    async def test_outlet_also_sets_inuse(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"gniazdko": _device("outlet")})
        hk.update_tuya_state("gniazdko", True)
        chars = hk._tuya_chars["gniazdko"]
        assert chars["on"].value is True
        assert chars["inuse"].value is True

    async def test_dimmer_lightbulb_sets_brightness_when_percent_given(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"lampka": _device("lightbulb", is_dimmer=True)})
        hk.update_tuya_state("lampka", True, 55)
        chars = hk._tuya_chars["lampka"]
        assert chars["on"].value is True
        assert chars["level"].value == 55

    async def test_non_dimmer_lightbulb_ignores_percent(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"lampka": _device("lightbulb")})
        hk.update_tuya_state("lampka", True, 55)
        chars = hk._tuya_chars["lampka"]
        assert chars["on"].value is True
        assert "level" not in chars

    async def test_garage_door_open_and_closed(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"brama": _device("garage_door")})
        hk.update_tuya_state("brama", True)
        chars = hk._tuya_chars["brama"]
        assert chars["current"].value == 0
        assert chars["target"].value == 0
        hk.update_tuya_state("brama", False)
        assert chars["current"].value == 1
        assert chars["target"].value == 1

    async def test_window_covering_uses_percent_when_given(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"roleta": _device("window_covering")})
        hk.update_tuya_state("roleta", True, 70)
        chars = hk._tuya_chars["roleta"]
        assert chars["current"].value == 70
        assert chars["target"].value == 70

    async def test_window_covering_falls_back_to_on_off_without_percent(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"roleta": _device("window_covering")})
        hk.update_tuya_state("roleta", True)
        chars = hk._tuya_chars["roleta"]
        assert chars["current"].value == 100
        hk.update_tuya_state("roleta", False)
        assert chars["current"].value == 0

    async def test_valve_on_off(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"nawadnianie": _device("valve")})
        hk.update_tuya_state("nawadnianie", True)
        chars = hk._tuya_chars["nawadnianie"]
        assert chars["active"].value == 1
        assert chars["inuse"].value == 1

    async def test_lock_on_off(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"zamek": _device("lock")})
        hk.update_tuya_state("zamek", True)
        chars = hk._tuya_chars["zamek"]
        assert chars["current"].value == 1
        assert chars["target"].value == 1

    async def test_update_unknown_device_is_noop(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"gniazdko": _device("switch")})
        hk.update_tuya_state("brak_takiego", True)


class TestResolveHomekitConfig:

    def _config_raw(self, homekit=None):
        return {
            "satel": {"partitions": {"Dom": 1, "Garaz": 2}},
            "tuya": {
                "cloud_devices": {"gniazdko": {"type": "socket"}},
                "devices": {"lampka": {"type": "dimmer"}},
            },
            "scenes": [{"name": "wieczor"}, {"name": "impreza"}],
            "homekit": homekit or {},
        }

    def test_partitions_default_to_enabled(self):
        resolved = resolve_homekit_config(self._config_raw())
        assert resolved["partitions"]["Dom"] == {"number": 1, "enabled": True}
        assert resolved["partitions"]["Garaz"] == {"number": 2, "enabled": True}

    def test_devices_default_to_not_exposed(self):
        resolved = resolve_homekit_config(self._config_raw())
        assert resolved["devices"]["gniazdko"]["type"] == ""
        assert resolved["devices"]["lampka"]["type"] == ""

    def test_devices_detect_is_dimmer_from_tuya_config(self):
        resolved = resolve_homekit_config(self._config_raw())
        assert resolved["devices"]["gniazdko"]["is_dimmer"] is False
        assert resolved["devices"]["lampka"]["is_dimmer"] is True

    def test_scenes_default_to_not_enabled(self):
        resolved = resolve_homekit_config(self._config_raw())
        assert resolved["scenes"]["wieczor"] == {"enabled": False}
        assert resolved["scenes"]["impreza"] == {"enabled": False}

    def test_rooms_pass_through(self):
        resolved = resolve_homekit_config(self._config_raw({"rooms": ["Salon", "Kuchnia"]}))
        assert resolved["rooms"] == ["Salon", "Kuchnia"]

    def test_saved_partition_disabled_overrides_default(self):
        resolved = resolve_homekit_config(self._config_raw({"partitions": {"Dom": {"enabled": False}}}))
        assert resolved["partitions"]["Dom"]["enabled"] is False
        assert resolved["partitions"]["Garaz"]["enabled"] is True

    def test_saved_device_type_and_room_are_applied(self):
        resolved = resolve_homekit_config(
            self._config_raw({"devices": {"lampka": {"type": "lightbulb", "room": "Salon"}}})
        )
        assert resolved["devices"]["lampka"]["type"] == "lightbulb"
        assert resolved["devices"]["lampka"]["room"] == "Salon"
        assert resolved["devices"]["gniazdko"]["type"] == ""

    def test_saved_scene_enabled_overrides_default(self):
        resolved = resolve_homekit_config(self._config_raw({"scenes": {"wieczor": {"enabled": True}}}))
        assert resolved["scenes"]["wieczor"]["enabled"] is True
        assert resolved["scenes"]["impreza"]["enabled"] is False

    def test_scenes_without_name_are_skipped(self):
        config_raw = self._config_raw()
        config_raw["scenes"].append({"actions": []})
        resolved = resolve_homekit_config(config_raw)
        assert len(resolved["scenes"]) == 2

    def test_detected_type_exposed_for_device_with_recognized_dp_schema(self):
        config_raw = self._config_raw()
        config_raw["tuya"]["devices"]["lampka"]["dp_schema"] = {
            "1": {"code": "va_temperature", "type": "Value"},
        }
        resolved = resolve_homekit_config(config_raw)
        assert resolved["devices"]["lampka"]["detected_type"] == "temperature_sensor"

    def test_auto_detected_type_used_as_default_when_nothing_saved(self):
        config_raw = self._config_raw()
        config_raw["tuya"]["devices"]["lampka"]["dp_schema"] = {
            "1": {"code": "va_temperature", "type": "Value"},
        }
        resolved = resolve_homekit_config(config_raw)
        assert resolved["devices"]["lampka"]["type"] == "temperature_sensor"

    def test_saved_type_overrides_auto_detected_type(self):
        config_raw = self._config_raw()
        config_raw["tuya"]["devices"]["lampka"]["dp_schema"] = {
            "1": {"code": "va_temperature", "type": "Value"},
        }
        config_raw["homekit"] = {"devices": {"lampka": {"type": "lightbulb"}}}
        resolved = resolve_homekit_config(config_raw)
        assert resolved["devices"]["lampka"]["type"] == "lightbulb"
        assert resolved["devices"]["lampka"]["detected_type"] == "temperature_sensor"

    def test_explicit_blank_type_suppresses_auto_detected_type(self):
        config_raw = self._config_raw()
        config_raw["tuya"]["devices"]["lampka"]["dp_schema"] = {
            "1": {"code": "va_temperature", "type": "Value"},
        }
        config_raw["homekit"] = {"devices": {"lampka": {"type": ""}}}
        resolved = resolve_homekit_config(config_raw)
        assert resolved["devices"]["lampka"]["type"] == ""
        assert resolved["devices"]["lampka"]["detected_type"] == "temperature_sensor"

    def test_cloud_device_category_drives_auto_detection(self):
        config_raw = self._config_raw()
        config_raw["tuya"]["cloud_devices"]["gniazdko"]["category"] = "sj"
        resolved = resolve_homekit_config(config_raw)
        assert resolved["devices"]["gniazdko"]["type"] == "leak_sensor"
        assert resolved["devices"]["gniazdko"]["detected_type"] == "leak_sensor"


def _sensor_device(type_id, room=""):
    return {"type": type_id, "room": room, "is_dimmer": False}


class TestSensorAccessoryConstruction:

    async def test_temperature_sensor_gets_temperature_sensor_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temperature_sensor")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "czujnik")
        assert acc.get_service("TemperatureSensor") is not None

    async def test_humidity_sensor_gets_humidity_sensor_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("humidity_sensor")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "czujnik")
        assert acc.get_service("HumiditySensor") is not None

    async def test_temp_humidity_sensor_gets_both_services_on_one_accessory(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temp_humidity_sensor")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "czujnik")
        assert acc.get_service("TemperatureSensor") is not None
        assert acc.get_service("HumiditySensor") is not None

    async def test_leak_sensor_gets_leak_sensor_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("leak_sensor")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "czujnik")
        assert acc.get_service("LeakSensor") is not None

    async def test_contact_sensor_gets_contact_sensor_service(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("contact_sensor")})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "czujnik")
        assert acc.get_service("ContactSensor") is not None

    async def test_sensor_accessories_are_not_stored_as_actuators(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temperature_sensor")})
        assert "czujnik" not in hk._tuya_chars
        assert "czujnik" in hk._tuya_sensor_chars


class TestUpdateTuyaSensorState:

    async def test_temperature_sensor_updates_current_temperature(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temperature_sensor")})
        hk.update_tuya_sensor_state("czujnik", {"temperature_celsius": 21.5})
        assert hk._tuya_sensor_chars["czujnik"]["temperature"].value == 21.5

    async def test_temperature_sensor_low_battery_flag(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temperature_sensor")})
        hk.update_tuya_sensor_state("czujnik", {"temperature_celsius": 21.5, "battery_percent": 15})
        assert hk._tuya_sensor_chars["czujnik"]["low_battery"].value == 1
        hk.update_tuya_sensor_state("czujnik", {"temperature_celsius": 21.5, "battery_percent": 80})
        assert hk._tuya_sensor_chars["czujnik"]["low_battery"].value == 0

    async def test_humidity_sensor_updates_current_relative_humidity(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("humidity_sensor")})
        hk.update_tuya_sensor_state("czujnik", {"humidity_percent": 62.0})
        assert hk._tuya_sensor_chars["czujnik"]["humidity"].value == 62.0

    async def test_temp_humidity_sensor_updates_both_services(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temp_humidity_sensor")})
        hk.update_tuya_sensor_state("czujnik", {"temperature_celsius": 21.2, "humidity_percent": 53.0})
        chars = hk._tuya_sensor_chars["czujnik"]
        assert chars["temperature"].value == 21.2
        assert chars["humidity"].value == 53.0

    async def test_leak_sensor_detected_and_not_detected(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("leak_sensor")})
        hk.update_tuya_sensor_state("czujnik", {"leak_detected": True})
        assert hk._tuya_sensor_chars["czujnik"]["leak"].value == 1
        hk.update_tuya_sensor_state("czujnik", {"leak_detected": False})
        assert hk._tuya_sensor_chars["czujnik"]["leak"].value == 0

    async def test_contact_sensor_open_and_closed(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("contact_sensor")})
        hk.update_tuya_sensor_state("czujnik", {"contact_open": True})
        assert hk._tuya_sensor_chars["czujnik"]["contact"].value == 1
        hk.update_tuya_sensor_state("czujnik", {"contact_open": False})
        assert hk._tuya_sensor_chars["czujnik"]["contact"].value == 0

    async def test_update_unknown_sensor_device_is_noop(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temperature_sensor")})
        hk.update_tuya_sensor_state("brak_takiego", {"temperature_celsius": 10})

    async def test_empty_readings_is_noop(self, tmp_path):
        hk = _make_bridge(tmp_path, devices={"czujnik": _sensor_device("temperature_sensor")})
        hk.update_tuya_sensor_state("czujnik", {})
        assert hk._tuya_sensor_chars["czujnik"]["temperature"].value == 20.0
