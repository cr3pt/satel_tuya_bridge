import pytest

from bridge.tuya_local import TuyaLocalController, scan_network, fetch_cloud_devices

DEVICES_CFG = {
    "gniazdko_salon": {"id": "dev1", "ip": "192.168.1.50", "local_key": "key1", "type": "socket"},
    "swiatlo_korytarz": {
        "id": "dev2", "ip": "192.168.1.51", "local_key": "key2", "type": "dimmer",
        "dimmer_dp": "3", "dimmer_range": [10, 1000],
    },
}


class FlakyDevice:

    def __init__(self, fail_times: int = 0, fail_via_error_dict: bool = False):
        self.fail_times = fail_times
        self.fail_via_error_dict = fail_via_error_dict
        self.call_count = 0
        self.turned_on = False

    def turn_on(self):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            if self.fail_via_error_dict:
                return {"Error": "Device not reachable"}
            raise ConnectionError("symulowana awaria sieci")
        self.turned_on = True
        return {"dps": {"1": True}}

    def turn_off(self):
        self.turned_on = False
        return {"dps": {"1": False}}

    def status(self):
        return {"dps": {"1": self.turned_on}}

    def set_status(self, value, dp):
        return {"dps": {dp: value}}

    def set_value(self, dp, value):
        return {"dps": {dp: value}}


@pytest.fixture
def controller():
    ctrl = TuyaLocalController(DEVICES_CFG, retries=2, retry_delay=0.01)
    return ctrl


async def test_successful_call_on_first_try(controller):
    fake_device = FlakyDevice(fail_times=0)
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on("gniazdko_salon")
    assert fake_device.turned_on is True
    assert fake_device.call_count == 1


async def test_retries_after_transient_failure(controller):
    fake_device = FlakyDevice(fail_times=2)
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on("gniazdko_salon")
    assert fake_device.turned_on is True
    assert fake_device.call_count == 3


async def test_error_dict_response_triggers_retry(controller):
    fake_device = FlakyDevice(fail_times=1, fail_via_error_dict=True)
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on("gniazdko_salon")
    assert fake_device.turned_on is True
    assert fake_device.call_count == 2


async def test_exhausted_retries_raises_connection_error(controller):
    fake_device = FlakyDevice(fail_times=10)
    controller._devices["gniazdko_salon"] = fake_device
    with pytest.raises(ConnectionError):
        await controller.turn_on("gniazdko_salon")
    assert fake_device.call_count == 3


async def test_turn_on_for_duration_still_turns_off_after_failed_turn_on(controller):
    fake_device = FlakyDevice(fail_times=10)
    controller._devices["gniazdko_salon"] = fake_device
    await controller.turn_on_for_duration("gniazdko_salon", duration_seconds=0.01)
    assert fake_device.turned_on is False


async def test_get_all_statuses_marks_unreachable_devices_as_none(controller):
    working = FlakyDevice(fail_times=0)
    broken = FlakyDevice(fail_times=999)
    controller._devices["gniazdko_salon"] = working
    controller._devices["swiatlo_korytarz"] = broken

    class AlwaysFailingStatus:
        def status(self):
            raise ConnectionError("padło")

    controller._devices["swiatlo_korytarz"] = AlwaysFailingStatus()
    result = await controller.get_all_statuses()
    assert result["gniazdko_salon"] is not None
    assert result["swiatlo_korytarz"] is None


class TestScanNetwork:

    def test_returns_id_ip_version_for_each_discovered_device(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.deviceScan", lambda **kw: {
            "192.168.1.50": {"gwId": "dev1", "version": "3.4"},
            "192.168.1.51": {"id": "dev2", "version": "3.3"},
        })
        devices = scan_network()
        assert {"id": "dev1", "ip": "192.168.1.50", "version": "3.4"} in devices
        assert {"id": "dev2", "ip": "192.168.1.51", "version": "3.3"} in devices

    def test_no_devices_found_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.deviceScan", lambda **kw: {})
        assert scan_network() == []

    def test_maxretry_passed_through_to_tinytuya(self, monkeypatch):
        captured = {}

        def fake_scan(**kwargs):
            captured.update(kwargs)
            return {}

        monkeypatch.setattr("bridge.tuya_local.tinytuya.deviceScan", fake_scan)
        scan_network(maxretry=3)
        assert captured["maxretry"] == 3
        assert captured["poll"] is False


class FakeCloud:

    def __init__(self, devices=None, error=None, getdevices_raises=None, getdevices_raw=None, **kwargs):
        self.error = error
        self._devices = devices if devices is not None else []
        self._raises = getdevices_raises
        self.getdevices_raw = getdevices_raw

    def getdevices(self, *args, **kwargs):
        if self._raises:
            raise self._raises
        return self._devices


class TestFetchCloudDevices:

    def test_returns_name_id_local_key_from_cloud(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "d1", "name": "Gniazdko", "key": "localkey1"},
        ]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices == [{
            "id": "d1", "name": "Gniazdko", "local_key": "localkey1", "ip": "", "version": "", "category": "",
            "gateway_id": "", "node_id": "",
        }]

    def test_surfaces_gateway_id_and_node_id_for_zigbee_sub_devices(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "sub1", "name": "Lampka Zigbee", "key": "", "gateway_id": "gw1", "node_id": "0011"},
        ]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["gateway_id"] == "gw1"
        assert devices[0]["node_id"] == "0011"

    def test_cross_references_lan_scan_by_id_for_ip(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "d1", "name": "Gniazdko", "key": "localkey1"},
        ]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [
            {"id": "d1", "ip": "192.168.1.50", "version": "3.4"},
        ])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["ip"] == "192.168.1.50"
        assert devices[0]["version"] == "3.4"

    def test_lan_scan_failure_does_not_break_cloud_import(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "d1", "name": "Gniazdko", "key": "localkey1"},
        ]))

        def raise_scan(**kwargs):
            raise RuntimeError("brak dostępu do sieci")

        monkeypatch.setattr("bridge.tuya_local.scan_network", raise_scan)
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["ip"] == ""

    def test_login_error_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(error={"Error": "Cloud _gettoken() failed"}),
        )
        with pytest.raises(ConnectionError):
            fetch_cloud_devices("eu", "bad_id", "bad_secret")

    def test_getdevices_exception_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(getdevices_raises=KeyError("result")),
        )
        with pytest.raises(ConnectionError):
            fetch_cloud_devices("eu", "access_id", "access_secret")

    def test_getdevices_error_dict_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(devices={"Error": "token expired"}),
        )
        with pytest.raises(ConnectionError, match="token expired"):
            fetch_cloud_devices("eu", "access_id", "access_secret")

    def test_constructor_raising_is_wrapped_as_connection_error(self, monkeypatch):
        def raise_typeerror(**kwargs):
            raise TypeError("Tuya Cloud Key and Secret required")

        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", raise_typeerror)
        with pytest.raises(ConnectionError):
            fetch_cloud_devices("eu", "", "")

    def test_no_devices_found_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        assert fetch_cloud_devices("eu", "access_id", "access_secret") == []

    def test_empty_result_with_success_false_raw_raises_clear_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(devices=[], getdevices_raw={"success": False, "code": 1004, "msg": "sign invalid"}),
        )
        with pytest.raises(ConnectionError, match="sign invalid"):
            fetch_cloud_devices("eu", "access_id", "access_secret")

    def test_empty_result_without_success_false_flag_stays_empty_list(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(devices=[], getdevices_raw={"success": True, "result": []}),
        )
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        assert fetch_cloud_devices("eu", "access_id", "access_secret") == []

    def test_empty_result_missing_raw_attribute_stays_empty_list(self, monkeypatch):
        cloud = FakeCloud(devices=[])
        del cloud.getdevices_raw
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: cloud)
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        assert fetch_cloud_devices("eu", "access_id", "access_secret") == []


class TestZigbeeGateways:

    GATEWAYS_CFG = {
        "brama_salon": {"id": "gw1", "ip": "192.168.1.60", "local_key": "gwkey", "version": 3.4},
    }

    def test_gateway_device_created_from_cfg(self):
        ctrl = TuyaLocalController({}, self.GATEWAYS_CFG)
        assert "brama_salon" in ctrl._gateways
        assert ctrl._gateways["brama_salon"].id == "gw1"

    def test_sub_device_gets_cid_parent_and_zigbee_dev_type(self):
        devices_cfg = {
            "lampka": {"id": "sub1", "gateway": "brama_salon", "cid": "node1", "type": "socket"},
        }
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        dev = ctrl._devices["lampka"]
        assert dev.cid == "node1"
        assert dev.parent is ctrl._gateways["brama_salon"]
        assert dev.dev_type == "zigbee"

    def test_sub_device_cid_defaults_to_device_id_when_not_given(self):
        devices_cfg = {"lampka": {"id": "sub1", "gateway": "brama_salon", "type": "socket"}}
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        assert ctrl._devices["lampka"].cid == "sub1"

    def test_sub_device_inherits_gateway_protocol_version(self):
        devices_cfg = {"lampka": {"id": "sub1", "gateway": "brama_salon", "type": "socket"}}
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        assert ctrl._devices["lampka"].version == ctrl._gateways["brama_salon"].version

    def test_device_without_gateway_field_is_a_plain_wifi_outlet(self):
        ctrl = TuyaLocalController(DEVICES_CFG, self.GATEWAYS_CFG)
        assert ctrl._devices["gniazdko_salon"].cid is None
        assert ctrl._devices["gniazdko_salon"].parent is None

    def test_dangling_gateway_reference_is_skipped_not_crashed(self):
        devices_cfg = {
            "lampka": {"id": "sub1", "gateway": "brama_nieistniejaca", "type": "socket"},
            "gniazdko_salon": DEVICES_CFG["gniazdko_salon"],
        }
        ctrl = TuyaLocalController(devices_cfg, self.GATEWAYS_CFG)
        assert "lampka" not in ctrl._devices
        assert "gniazdko_salon" in ctrl._devices

    def test_zigbee_gateways_cfg_defaults_to_empty_when_omitted(self):
        ctrl = TuyaLocalController(DEVICES_CFG)
        assert ctrl._gateways == {}
        assert set(ctrl._devices) == set(DEVICES_CFG)
