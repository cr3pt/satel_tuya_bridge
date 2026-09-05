import asyncio

from bridge.homekit_bridge import HomeKitBridge, _stable_aid


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


def _make_bridge(tmp_path, satel=None, tuya=None, scenes=None, partitions=None, tuya_devices_cfg=None, cfg=None):
    return HomeKitBridge(
        cfg or {"port": 51826},
        satel or FakeSatel(),
        tuya or FakeTuya(),
        scenes or FakeScenes({}),
        partitions or {},
        tuya_devices_cfg or {},
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

    async def test_builds_one_accessory_per_partition_tuya_device_and_enabled_scene(self, tmp_path):
        scenes = FakeScenes({
            "wieczor": {"enabled": True, "actions": []},
            "wylaczona": {"enabled": False, "actions": []},
        })
        hk = _make_bridge(
            tmp_path,
            scenes=scenes,
            partitions={"Dom": 1, "Garaz": 2},
            tuya_devices_cfg={"gniazdko": {"type": "socket"}, "lampka": {"type": "dimmer"}},
        )
        names = {acc.display_name for acc in hk.bridge.accessories.values()}
        assert names == {"Dom", "Garaz", "gniazdko", "lampka", "wieczor"}
        assert "wylaczona" not in names

    async def test_aids_are_stable_across_two_independent_builds(self, tmp_path):
        kwargs = dict(
            partitions={"Dom": 1},
            tuya_devices_cfg={"gniazdko": {"type": "socket"}},
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

    async def test_dimmer_gets_lightbulb_with_brightness(self, tmp_path):
        hk = _make_bridge(tmp_path, tuya_devices_cfg={"lampka": {"type": "dimmer"}})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "lampka")
        service = acc.get_service("Lightbulb")
        assert service is not None
        assert service.get_characteristic("Brightness") is not None

    async def test_non_dimmer_gets_plain_switch(self, tmp_path):
        hk = _make_bridge(tmp_path, tuya_devices_cfg={"gniazdko": {"type": "socket"}})
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "gniazdko")
        assert acc.get_service("Switch") is not None
        assert acc.get_service("Lightbulb") is None


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

    async def test_switch_on_calls_turn_on(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, tuya_devices_cfg={"gniazdko": {"type": "socket"}})
        on_char = hk._tuya_on_chars["gniazdko"]
        on_char.client_update_value(True)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "gniazdko", None)]

    async def test_switch_off_calls_turn_off(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, tuya_devices_cfg={"gniazdko": {"type": "socket"}})
        on_char = hk._tuya_on_chars["gniazdko"]
        on_char.client_update_value(False)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("off", "gniazdko")]

    async def test_brightness_setter_calls_turn_on_with_percent(self, tmp_path):
        tuya = FakeTuya()
        hk = _make_bridge(tmp_path, tuya=tuya, tuya_devices_cfg={"lampka": {"type": "dimmer"}})
        brightness_char = hk._tuya_brightness_chars["lampka"]
        brightness_char.client_update_value(42)
        await asyncio.sleep(0.05)
        assert tuya.calls == [("on", "lampka", 42)]


class TestSceneSetter:

    async def test_turning_on_runs_scene_and_resets_to_off(self, tmp_path, monkeypatch):
        import bridge.homekit_bridge as hk_module
        monkeypatch.setattr(hk_module, "_SCENE_RESET_DELAY_SECONDS", 0.01)

        scenes = FakeScenes({"wieczor": {"enabled": True, "actions": []}})
        hk = _make_bridge(tmp_path, scenes=scenes)
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "wieczor")
        on_char = acc.get_service("Switch").get_characteristic("On")
        on_char.client_update_value(True)
        await asyncio.sleep(0.1)
        assert scenes.calls == ["wieczor"]
        assert on_char.value is False

    async def test_turning_off_does_not_run_scene(self, tmp_path):
        scenes = FakeScenes({"wieczor": {"enabled": True, "actions": []}})
        hk = _make_bridge(tmp_path, scenes=scenes)
        acc = next(a for a in hk.bridge.accessories.values() if a.display_name == "wieczor")
        on_char = acc.get_service("Switch").get_characteristic("On")
        on_char.client_update_value(False)
        await asyncio.sleep(0.05)
        assert scenes.calls == []


class TestUpdateMethods:

    async def test_update_partition_state_armed(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(1, armed=True)
        current_char, target_char = hk._partition_chars[1]
        assert current_char.value == 1
        assert target_char.value == 1

    async def test_update_partition_state_disarmed(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(1, armed=False)
        current_char, target_char = hk._partition_chars[1]
        assert current_char.value == 3
        assert target_char.value == 3

    async def test_update_partition_state_triggered_overrides_current_only(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(1, armed=True, triggered=True)
        current_char, target_char = hk._partition_chars[1]
        assert current_char.value == 4
        assert target_char.value == 1

    async def test_update_unknown_partition_is_noop(self, tmp_path):
        hk = _make_bridge(tmp_path, partitions={"Dom": 1})
        hk.update_partition_state(999, armed=True)

    async def test_update_tuya_on(self, tmp_path):
        hk = _make_bridge(tmp_path, tuya_devices_cfg={"gniazdko": {"type": "socket"}})
        hk.update_tuya_on("gniazdko", True)
        assert hk._tuya_on_chars["gniazdko"].value is True

    async def test_update_tuya_brightness(self, tmp_path):
        hk = _make_bridge(tmp_path, tuya_devices_cfg={"lampka": {"type": "dimmer"}})
        hk.update_tuya_brightness("lampka", 55)
        assert hk._tuya_brightness_chars["lampka"].value == 55

    async def test_update_unknown_tuya_device_is_noop(self, tmp_path):
        hk = _make_bridge(tmp_path, tuya_devices_cfg={"gniazdko": {"type": "socket"}})
        hk.update_tuya_on("brak_takiego", True)
        hk.update_tuya_brightness("brak_takiego", 10)
