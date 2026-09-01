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


async def test_get_status_normalizes_to_key_1_like_local_controller(controller):
    fake = FakeTinytuyaCloud()
    fake.status_by_device["cloud1"] = [{"code": "switch_1", "value": True}, {"code": "other", "value": 5}]
    controller._cloud = fake
    status = await controller.get_status("lampa_taras")
    assert status == {"1": True}


async def test_get_status_empty_when_switch_code_not_present(controller):
    fake = FakeTinytuyaCloud()
    fake.status_by_device["cloud1"] = [{"code": "other", "value": 5}]
    controller._cloud = fake
    status = await controller.get_status("lampa_taras")
    assert status == {}


async def test_get_all_statuses_queries_all_devices(controller):
    fake = FakeTinytuyaCloud()
    fake.status_by_device["cloud1"] = [{"code": "switch_1", "value": True}]
    fake.status_by_device["cloud2"] = [{"code": "switch", "value": False}]
    controller._cloud = fake
    statuses = await controller.get_all_statuses()
    assert statuses == {"lampa_taras": {"1": True}, "wentylator": {"1": False}}


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
