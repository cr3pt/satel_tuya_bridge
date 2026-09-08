import pytest

from bridge.tuya_cloud import TuyaCloudController

DEVICES_CFG = {
    "lampa_taras": {"id": "cloud1", "category": "kg", "switch_code": "switch_1"},
    "wentylator": {"id": "cloud2", "category": "fs", "switch_code": "switch"},
}

CREDENTIALS = {"api_region": "eu", "access_id": "aid", "access_key": "akey"}


class FakeTinytuyaCloud:

    def __init__(self, fail_times: int = 0, **kwargs):
        self.fail_times = fail_times
        self.call_count = 0
        self.sent_commands: list[tuple] = []
        self.status_by_device: dict[str, list[dict]] = {}

    def sendcommand(self, deviceid=None, commands=None, uri=None):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            return {"Error": "Timeout", "Err": "1", "Payload": None}
        self.sent_commands.append((deviceid, commands))
        return {"success": True, "result": True}

    def getstatus(self, deviceid=None):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            return {"Error": "Timeout", "Err": "1", "Payload": None}
        return {"success": True, "result": self.status_by_device.get(deviceid, [])}


@pytest.fixture
def controller():
    return TuyaCloudController(DEVICES_CFG, CREDENTIALS, retries=2, retry_delay=0.01)


def test_client_defaults_missing_api_region_to_eu(monkeypatch):
    captured = {}

    def fake_cloud_ctor(apiRegion=None, apiKey=None, apiSecret=None):
        captured["apiRegion"] = apiRegion
        return FakeTinytuyaCloud()

    import bridge.tuya_cloud as tuya_cloud_module
    monkeypatch.setattr(tuya_cloud_module.tinytuya, "Cloud", fake_cloud_ctor)

    controller = TuyaCloudController(DEVICES_CFG, {"access_id": "aid", "access_key": "akey"})
    controller._client()
    assert captured["apiRegion"] == "eu"


def test_client_uses_configured_api_region(monkeypatch):
    captured = {}

    def fake_cloud_ctor(apiRegion=None, apiKey=None, apiSecret=None):
        captured["apiRegion"] = apiRegion
        return FakeTinytuyaCloud()

    import bridge.tuya_cloud as tuya_cloud_module
    monkeypatch.setattr(tuya_cloud_module.tinytuya, "Cloud", fake_cloud_ctor)

    controller = TuyaCloudController(DEVICES_CFG, CREDENTIALS)
    controller._client()
    assert captured["apiRegion"] == "eu"

    controller2 = TuyaCloudController(DEVICES_CFG, {**CREDENTIALS, "api_region": "us"})
    controller2._client()
    assert captured["apiRegion"] == "us"


async def test_turn_on_sends_switch_code_true(controller):
    fake = FakeTinytuyaCloud()
    controller._cloud = fake
    await controller.turn_on("lampa_taras")
    assert fake.sent_commands == [("cloud1", {"commands": [{"code": "switch_1", "value": True}]})]


async def test_turn_off_sends_switch_code_false(controller):
    fake = FakeTinytuyaCloud()
    controller._cloud = fake
    await controller.turn_off("lampa_taras")
    assert fake.sent_commands == [("cloud1", {"commands": [{"code": "switch_1", "value": False}]})]


async def test_uses_per_device_switch_code(controller):
    fake = FakeTinytuyaCloud()
    controller._cloud = fake
    await controller.turn_on("wentylator")
    assert fake.sent_commands == [("cloud2", {"commands": [{"code": "switch", "value": True}]})]


async def test_retries_after_transient_failure(controller):
    fake = FakeTinytuyaCloud(fail_times=1)
    controller._cloud = fake
    await controller.turn_on("lampa_taras")
    assert fake.sent_commands == [("cloud1", {"commands": [{"code": "switch_1", "value": True}]})]


async def test_raises_connection_error_after_exhausting_retries(controller):
    fake = FakeTinytuyaCloud(fail_times=99)
    controller._cloud = fake
    with pytest.raises(ConnectionError):
        await controller.turn_on("lampa_taras")


async def test_get_status_returns_all_fields_keyed_by_code(controller):
    fake = FakeTinytuyaCloud()
    fake.status_by_device["cloud1"] = [{"code": "switch_1", "value": True}, {"code": "other", "value": 5}]
    controller._cloud = fake
    status = await controller.get_status("lampa_taras")
    assert status == {"switch_1": True, "other": 5}


async def test_get_status_empty_when_no_fields_reported(controller):
    fake = FakeTinytuyaCloud()
    fake.status_by_device["cloud1"] = []
    controller._cloud = fake
    status = await controller.get_status("lampa_taras")
    assert status == {}


async def test_get_all_statuses_queries_all_devices(controller):
    fake = FakeTinytuyaCloud()
    fake.status_by_device["cloud1"] = [{"code": "switch_1", "value": True}]
    fake.status_by_device["cloud2"] = [{"code": "switch", "value": False}]
    controller._cloud = fake
    statuses = await controller.get_all_statuses()
    assert statuses == {"lampa_taras": {"switch_1": True}, "wentylator": {"switch": False}}


async def test_get_all_statuses_marks_failed_device_as_none(controller):
    fake = FakeTinytuyaCloud(fail_times=99)
    controller._cloud = fake
    statuses = await controller.get_all_statuses()
    assert statuses == {"lampa_taras": None, "wentylator": None}


async def test_turn_on_for_duration_turns_off_after_delay(controller):
    fake = FakeTinytuyaCloud()
    controller._cloud = fake
    await controller.turn_on_for_duration("lampa_taras", duration_seconds=0.01)
    codes = [c[1]["commands"][0]["value"] for c in fake.sent_commands]
    assert codes == [True, False]


class TestAutoDetectedSwitchCodes:

    DEVICES_CFG_NO_SWITCH_CODE = {
        "kuchnia_2obwody": {"id": "cloud3"},
    }

    async def test_turns_on_all_detected_switch_codes_together(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloud()
        fake.status_by_device["cloud3"] = [
            {"code": "switch_1", "value": False}, {"code": "switch_2", "value": False},
            {"code": "countdown_1", "value": 0},
        ]
        controller._cloud = fake
        await controller.turn_on("kuchnia_2obwody")
        assert fake.sent_commands == [
            ("cloud3", {"commands": [{"code": "switch_1", "value": True}, {"code": "switch_2", "value": True}]}),
        ]

    async def test_turns_off_all_detected_switch_codes_together(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloud()
        fake.status_by_device["cloud3"] = [
            {"code": "switch_1", "value": True}, {"code": "switch_2", "value": True},
        ]
        controller._cloud = fake
        await controller.turn_off("kuchnia_2obwody")
        assert fake.sent_commands == [
            ("cloud3", {"commands": [{"code": "switch_1", "value": False}, {"code": "switch_2", "value": False}]}),
        ]

    async def test_detects_switch_led_for_dimmer_switch(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloud()
        fake.status_by_device["cloud3"] = [
            {"code": "switch_led", "value": False}, {"code": "bright_value", "value": 500},
        ]
        controller._cloud = fake
        await controller.turn_on("kuchnia_2obwody")
        assert fake.sent_commands == [("cloud3", {"commands": [{"code": "switch_led", "value": True}]})]

    async def test_detected_codes_are_cached_across_calls(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloud()
        fake.status_by_device["cloud3"] = [{"code": "switch_1", "value": False}]
        controller._cloud = fake
        await controller.turn_on("kuchnia_2obwody")
        await controller.turn_off("kuchnia_2obwody")
        assert controller._switch_codes_cache["kuchnia_2obwody"] == ["switch_1"]
        assert [cmd for _dev, cmd in fake.sent_commands] == [
            {"commands": [{"code": "switch_1", "value": True}]},
            {"commands": [{"code": "switch_1", "value": False}]},
        ]

    async def test_falls_back_to_switch_1_when_no_switch_like_code_found(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloud()
        fake.status_by_device["cloud3"] = [{"code": "bright_value", "value": 500}]
        controller._cloud = fake
        await controller.turn_on("kuchnia_2obwody")
        assert fake.sent_commands == [("cloud3", {"commands": [{"code": "switch_1", "value": True}]})]

    async def test_falls_back_to_switch_1_when_status_query_fails(self):
        class GetstatusAlwaysFails(FakeTinytuyaCloud):
            def getstatus(self, deviceid=None):
                return {"Error": "Timeout", "Err": "1", "Payload": None}

        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=0, retry_delay=0.01)
        fake = GetstatusAlwaysFails()
        controller._cloud = fake
        await controller.turn_on("kuchnia_2obwody")
        assert fake.sent_commands == [("cloud3", {"commands": [{"code": "switch_1", "value": True}]})]

    async def test_explicit_switch_code_skips_auto_detection(self):
        controller = TuyaCloudController(DEVICES_CFG, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloud()
        controller._cloud = fake
        await controller.turn_on("lampa_taras")
        assert fake.sent_commands == [("cloud1", {"commands": [{"code": "switch_1", "value": True}]})]
        assert "lampa_taras" not in controller._switch_codes_cache


class FakeTinytuyaCloudWithFunctions(FakeTinytuyaCloud):

    def __init__(self, functions_by_device: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self.functions_by_device = functions_by_device or {}

    def getdps(self, deviceid=None):
        return {"success": True, "result": {"functions": self.functions_by_device.get(deviceid, []), "status": []}}


class TestFunctionsSchemaSwitchDetection:
    """Ściemniacz zgłoszony przez użytkownika: status raportował dodatkowy kod
    podobny do przełącznika, który nie był faktycznie zapisywalny - wysłanie go
    razem z prawdziwym przełącznikiem w jednej komendzie Tuya Cloud odrzucało
    całą paczkę błędem 'command or value not support'. Detekcja musi korzystać
    ze schematu 'functions' (gwarantowanie zapisywalne DP), nie z samego statusu.
    """

    DEVICES_CFG_NO_SWITCH_CODE = {
        "sciemniacz": {"id": "cloud4"},
    }

    async def test_ignores_status_only_code_not_present_in_functions(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloudWithFunctions(functions_by_device={
            "cloud4": [{"code": "switch_led", "type": "Boolean", "dp_id": 1}],
        })
        fake.status_by_device["cloud4"] = [
            {"code": "switch_1", "value": False},
            {"code": "switch_led", "value": False},
        ]
        controller._cloud = fake
        await controller.turn_on("sciemniacz")
        assert fake.sent_commands == [("cloud4", {"commands": [{"code": "switch_led", "value": True}]})]

    async def test_multi_gang_still_uses_all_writable_codes(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloudWithFunctions(functions_by_device={
            "cloud4": [
                {"code": "switch_1", "type": "Boolean", "dp_id": 1},
                {"code": "switch_2", "type": "Boolean", "dp_id": 2},
            ],
        })
        controller._cloud = fake
        await controller.turn_on("sciemniacz")
        assert fake.sent_commands == [
            ("cloud4", {"commands": [{"code": "switch_1", "value": True}, {"code": "switch_2", "value": True}]}),
        ]

    async def test_falls_back_to_status_when_functions_schema_unavailable(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloudWithFunctions(functions_by_device={"cloud4": []})
        fake.status_by_device["cloud4"] = [{"code": "switch_1", "value": False}]
        controller._cloud = fake
        await controller.turn_on("sciemniacz")
        assert fake.sent_commands == [("cloud4", {"commands": [{"code": "switch_1", "value": True}]})]

    async def test_falls_back_to_status_when_getdps_missing(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloud()
        fake.status_by_device["cloud4"] = [{"code": "switch_1", "value": False}]
        controller._cloud = fake
        await controller.turn_on("sciemniacz")
        assert fake.sent_commands == [("cloud4", {"commands": [{"code": "switch_1", "value": True}]})]

    async def test_functions_based_codes_are_cached(self):
        controller = TuyaCloudController(self.DEVICES_CFG_NO_SWITCH_CODE, CREDENTIALS, retries=2, retry_delay=0.01)
        fake = FakeTinytuyaCloudWithFunctions(functions_by_device={
            "cloud4": [{"code": "switch_led", "type": "Boolean", "dp_id": 1}],
        })
        controller._cloud = fake
        await controller.turn_on("sciemniacz")
        await controller.turn_off("sciemniacz")
        assert controller._switch_codes_cache["sciemniacz"] == ["switch_led"]


def test_client_created_lazily_with_credentials(monkeypatch):
    created_with = {}

    class FakeCloudCtor:
        def __init__(self, apiRegion=None, apiKey=None, apiSecret=None):
            created_with.update(apiRegion=apiRegion, apiKey=apiKey, apiSecret=apiSecret)

    import bridge.tuya_cloud as tuya_cloud_module
    monkeypatch.setattr(tuya_cloud_module.tinytuya, "Cloud", FakeCloudCtor)

    controller = TuyaCloudController(DEVICES_CFG, CREDENTIALS)
    assert created_with == {}
    client = controller._client()
    assert created_with == {"apiRegion": "eu", "apiKey": "aid", "apiSecret": "akey"}
    assert controller._client() is client
