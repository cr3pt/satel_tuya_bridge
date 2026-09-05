import pytest

from bridge.tuya_local import (
    TuyaLocalController, scan_network, fetch_cloud_devices, fetch_device_dp_schema,
    fetch_dp_schemas_for_devices, find_tuya_switch_key, format_tuya_dps,
    extract_tuya_sensor_readings, detect_tuya_sensor_type,
)

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

    def __init__(self, devices=None, error=None, getdevices_raises=None, getdevices_raw=None,
                 dps_by_device=None, getdps_raises=None, **kwargs):
        self.error = error
        self._devices = devices if devices is not None else []
        self._raises = getdevices_raises
        self.getdevices_raw = getdevices_raw
        self._dps_by_device = dps_by_device or {}
        self._getdps_raises = getdps_raises

    def getdevices(self, *args, **kwargs):
        if self._raises:
            raise self._raises
        return self._devices

    def getdps(self, device_id):
        if self._getdps_raises:
            raise self._getdps_raises
        return self._dps_by_device.get(device_id, {"success": True, "result": {"status": [], "functions": []}})


class TestFetchCloudDevices:

    def test_returns_name_id_local_key_from_cloud(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(devices=[
            {"id": "d1", "name": "Gniazdko", "key": "localkey1"},
        ]))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices == [{
            "id": "d1", "name": "Gniazdko", "local_key": "localkey1", "ip": "", "version": "", "category": "",
            "gateway_id": "", "node_id": "", "dp_schema": {},
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

    def test_includes_dp_schema_for_each_device(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(
            devices=[{"id": "d1", "name": "Czujnik CO2", "key": "k1"}],
            dps_by_device={"d1": {"success": True, "result": {
                "status": [{"code": "co2_value", "dp_id": 2, "type": "Value", "values": "{}"}],
                "functions": [{"code": "switch_1", "dp_id": 1, "type": "Boolean", "values": "{}"}],
            }}},
        ))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["dp_schema"] == {
            "2": {"code": "co2_value", "type": "Value", "values": "{}"},
            "1": {"code": "switch_1", "type": "Boolean", "values": "{}"},
        }

    def test_dp_schema_fetch_failure_for_one_device_does_not_break_import(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(
            devices=[{"id": "d1", "name": "Gniazdko", "key": "k1"}], getdps_raises=RuntimeError("boom"),
        ))
        monkeypatch.setattr("bridge.tuya_local.scan_network", lambda **kw: [])
        devices = fetch_cloud_devices("eu", "access_id", "access_secret")
        assert devices[0]["dp_schema"] == {}


class TestFetchDeviceDpSchema:

    def test_merges_status_and_functions_by_dp_id(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(dps_by_device={
            "d1": {"success": True, "result": {
                "status": [{"code": "va_temperature", "dp_id": 4, "type": "Value", "values": '{"scale":1,"unit":"C"}'}],
                "functions": [{"code": "switch_1", "dp_id": 1, "type": "Boolean", "values": "{}"}],
            }},
        }))
        schema = fetch_device_dp_schema("eu", "id", "secret", "d1")
        assert schema == {
            "4": {"code": "va_temperature", "type": "Value", "values": '{"scale":1,"unit":"C"}'},
            "1": {"code": "switch_1", "type": "Boolean", "values": "{}"},
        }

    def test_login_error_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(error={"Error": "bad credentials"}),
        )
        with pytest.raises(ConnectionError):
            fetch_device_dp_schema("eu", "bad", "bad", "d1")

    def test_unsuccessful_response_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: FakeCloud(dps_by_device={
            "d1": {"success": False, "msg": "device not found"},
        }))
        with pytest.raises(ConnectionError, match="device not found"):
            fetch_device_dp_schema("eu", "id", "secret", "d1")

    def test_getdps_exception_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "bridge.tuya_local.tinytuya.Cloud",
            lambda **kw: FakeCloud(getdps_raises=RuntimeError("network down")),
        )
        with pytest.raises(ConnectionError):
            fetch_device_dp_schema("eu", "id", "secret", "d1")


class TestFetchDpSchemasForDevices:

    def test_fetches_all_with_single_login(self, monkeypatch):
        logins = []

        def fake_ctor(**kw):
            logins.append(kw)
            return FakeCloud(dps_by_device={
                "d1": {"success": True, "result": {"status": [{"code": "switch_1", "dp_id": 1, "type": "Boolean", "values": "{}"}], "functions": []}},
                "d2": {"success": True, "result": {"status": [{"code": "co2_value", "dp_id": 2, "type": "Value", "values": "{}"}], "functions": []}},
            })

        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", fake_ctor)
        schemas, errors = fetch_dp_schemas_for_devices("eu", "id", "secret", ["d1", "d2"])
        assert len(logins) == 1
        assert schemas["d1"] == {"1": {"code": "switch_1", "type": "Boolean", "values": "{}"}}
        assert schemas["d2"] == {"2": {"code": "co2_value", "type": "Value", "values": "{}"}}
        assert errors == {}

    def test_one_device_failure_does_not_block_others(self, monkeypatch):
        class SelectiveCloud(FakeCloud):
            def getdps(self, device_id):
                if device_id == "bad":
                    raise RuntimeError("nie znaleziono urządzenia")
                return super().getdps(device_id)

        monkeypatch.setattr("bridge.tuya_local.tinytuya.Cloud", lambda **kw: SelectiveCloud(dps_by_device={
            "good": {"success": True, "result": {"status": [{"code": "switch_1", "dp_id": 1, "type": "Boolean", "values": "{}"}], "functions": []}},
        }))
        schemas, errors = fetch_dp_schemas_for_devices("eu", "id", "secret", ["good", "bad"])
        assert "good" in schemas
        assert "bad" not in schemas
        assert "bad" in errors


class TestFindTuyaSwitchKey:

    def test_falls_back_to_raw_key_1_without_schema(self):
        assert find_tuya_switch_key({"1": True, "4": 20}) == "1"

    def test_none_when_no_switch_and_no_schema(self):
        assert find_tuya_switch_key({"4": 20, "9": "auto"}) is None

    def test_schema_driven_boolean_switch_code(self):
        dps = {"1": True, "4": 20}
        schema = {"1": {"code": "switch_1", "type": "Boolean"}, "4": {"code": "va_temperature", "type": "Value"}}
        assert find_tuya_switch_key(dps, schema) == "1"

    def test_code_keyed_dps_recognized_without_schema(self):
        assert find_tuya_switch_key({"switch_1": True, "va_temperature": 20}) == "switch_1"

    def test_preferred_key_wins_when_present(self):
        assert find_tuya_switch_key({"power_toggle": True, "switch_1": False}, preferred="power_toggle") == "power_toggle"

    def test_preferred_key_ignored_when_absent(self):
        assert find_tuya_switch_key({"switch_1": True}, preferred="power_toggle") == "switch_1"


class TestFormatTuyaDps:

    def test_switch_only_device_has_no_extra_fields(self):
        is_on, extra = format_tuya_dps({"1": True})
        assert is_on is True
        assert extra == ""

    def test_sensor_device_without_schema_uses_raw_keys(self):
        is_on, extra = format_tuya_dps({"4": 235, "9": "auto"})
        assert is_on is None
        assert "4: 235" in extra
        assert "9: auto" in extra

    def test_schema_driven_labels_and_scaling(self):
        schema = {
            "1": {"code": "switch_1", "type": "Boolean"},
            "4": {"code": "va_temperature", "type": "Value", "values": '{"scale":1,"unit":"C"}'},
        }
        is_on, extra = format_tuya_dps({"1": False, "4": 235}, schema=schema)
        assert is_on is False
        assert extra == "temperatura: 23.5C"

    def test_boolean_extra_field_formatted_as_tak_nie(self):
        schema = {"1": {"code": "switch_1", "type": "Boolean"}, "2": {"code": "pir", "type": "Boolean"}}
        is_on, extra = format_tuya_dps({"1": True, "2": True}, schema=schema)
        assert extra == "ruch: tak"

    def test_none_values_are_skipped(self):
        is_on, extra = format_tuya_dps({"1": True, "4": None})
        assert extra == ""

    def test_camera_device_collapses_to_simple_status(self):
        dps = {
            "basic_indicator": False, "basic_flip": False, "basic_osd": True,
            "basic_private": False, "motion_sensitivity": 0, "basic_nightvision": 0,
            "sd_storge": "33554432|6401024|27153408", "sd_status": 1, "sd_format": False,
            "sd_format_state": 0, "ipc_auto_siren": False, "ipc_sharp": 62,
            "motion_switch": True, "decibel_switch": True, "decibel_sensitivity": 0,
            "record_switch": True, "record_mode": 1, "siren_switch": False,
            "motion_area_switch": False, "humanoid_filter": True,
        }
        is_on, extra = format_tuya_dps(dps)
        assert is_on is None
        assert extra == "działa"

    def test_camera_with_ipc_code_alone_is_detected(self):
        is_on, extra = format_tuya_dps({"ipc_bright": 50})
        assert extra == "działa"

    def test_device_with_only_two_camera_indicators_is_not_treated_as_camera(self):
        is_on, extra = format_tuya_dps({"basic_flip": False, "basic_osd": True})
        assert extra != "działa"
        assert "obrót obrazu" in extra

    def test_phase_raw_code_decoded_to_voltage_current_power(self):
        is_on, extra = format_tuya_dps({"switch_1": True, "phase_b": "CW4ABMUAAFQ="})
        assert extra == "faza B: 241.4V 1.221A 84W"

    def test_phase_raw_code_with_bad_data_falls_back_to_raw_value(self):
        is_on, extra = format_tuya_dps({"switch_1": True, "phase_b": "not-base64!!"})
        assert extra.startswith("faza B: ")

    def test_known_energy_code_scaled_even_without_schema(self):
        is_on, extra = format_tuya_dps({"switch_1": True, "forward_energy_total": 486706})
        assert extra == "energia pobrana (całkowita): 4867.06 kWh"

    def test_long_string_value_shown_as_binary_data_placeholder(self):
        blob = "A" * 200
        is_on, extra = format_tuya_dps({"switch_1": True, "alarm_message": blob})
        assert extra == f"wiadomość alarmowa: (dane binarne, {len(blob)} znaków)"

    def test_short_string_value_shown_verbatim(self):
        is_on, extra = format_tuya_dps({"switch_1": True, "breaker_number": "gom25010001"})
        assert extra == "numer licznika: gom25010001"


class TestExtractTuyaSensorReadings:

    def test_temperature_scaled_from_schema(self):
        schema = {"4": {"code": "va_temperature", "type": "Value", "values": '{"scale":1,"unit":"C"}'}}
        readings = extract_tuya_sensor_readings({"4": 235}, schema=schema)
        assert readings == {"temperature_celsius": 23.5}

    def test_temperature_defaults_to_tenths_scale_without_schema(self):
        readings = extract_tuya_sensor_readings({"va_temperature": 179})
        assert readings == {"temperature_celsius": 17.9}

    def test_temperature_converted_from_fahrenheit_when_unit_convert_is_f(self):
        readings = extract_tuya_sensor_readings({"va_temperature": 680, "temp_unit_convert": "f"})
        assert readings["temperature_celsius"] == pytest.approx(20.0, abs=0.1)

    def test_temperature_stays_celsius_when_unit_convert_is_c(self):
        readings = extract_tuya_sensor_readings({"va_temperature": 200, "temp_unit_convert": "c"})
        assert readings["temperature_celsius"] == 20.0

    def test_humidity_has_no_default_scaling(self):
        readings = extract_tuya_sensor_readings({"va_humidity": 53})
        assert readings == {"humidity_percent": 53.0}

    def test_humidity_clamped_to_0_100(self):
        readings = extract_tuya_sensor_readings({"va_humidity": 253})
        assert readings["humidity_percent"] == 100.0

    def test_combined_temperature_and_humidity(self):
        readings = extract_tuya_sensor_readings({"va_temperature": 212, "va_humidity": 53, "battery_percentage": 39})
        assert readings == {"temperature_celsius": 21.2, "humidity_percent": 53.0, "battery_percent": 39.0}

    def test_leak_enum_normal_is_not_detected(self):
        readings = extract_tuya_sensor_readings({"watersensor_state": "normal"})
        assert readings == {"leak_detected": False}

    def test_leak_enum_alarm_is_detected(self):
        readings = extract_tuya_sensor_readings({"watersensor_state": "alarm"})
        assert readings == {"leak_detected": True}

    def test_leak_boolean_true_is_detected(self):
        readings = extract_tuya_sensor_readings({"watersensor_state": True})
        assert readings == {"leak_detected": True}

    def test_contact_true_is_open(self):
        readings = extract_tuya_sensor_readings({"doorcontact_state": True})
        assert readings == {"contact_open": True}

    def test_contact_false_is_closed(self):
        readings = extract_tuya_sensor_readings({"doorcontact_state": False})
        assert readings == {"contact_open": False}

    def test_battery_percentage_no_scaling(self):
        readings = extract_tuya_sensor_readings({"battery_percentage": 100})
        assert readings == {"battery_percent": 100.0}

    def test_local_device_dp_ids_resolved_through_schema(self):
        schema = {
            "1": {"code": "doorcontact_state", "type": "Boolean"},
            "2": {"code": "battery_percentage", "type": "Value", "values": '{"scale":0,"unit":"%"}'},
        }
        readings = extract_tuya_sensor_readings({"1": False, "2": 100}, schema=schema)
        assert readings == {"contact_open": False, "battery_percent": 100.0}

    def test_no_recognized_codes_returns_empty(self):
        assert extract_tuya_sensor_readings({"switch_1": True}) == {}

    def test_none_values_are_ignored(self):
        assert extract_tuya_sensor_readings({"va_temperature": None}) == {}


class TestDetectTuyaSensorType:

    def test_local_device_temp_and_humidity_schema_detects_combo(self):
        device_cfg = {"dp_schema": {
            "1": {"code": "va_temperature", "type": "Value"},
            "2": {"code": "va_humidity", "type": "Value"},
        }}
        assert detect_tuya_sensor_type(device_cfg) == "temp_humidity_sensor"

    def test_local_device_temperature_only(self):
        device_cfg = {"dp_schema": {"1": {"code": "va_temperature", "type": "Value"}}}
        assert detect_tuya_sensor_type(device_cfg) == "temperature_sensor"

    def test_local_device_humidity_only(self):
        device_cfg = {"dp_schema": {"1": {"code": "va_humidity", "type": "Value"}}}
        assert detect_tuya_sensor_type(device_cfg) == "humidity_sensor"

    def test_local_device_leak_sensor(self):
        device_cfg = {"dp_schema": {"1": {"code": "watersensor_state", "type": "Enum"}}}
        assert detect_tuya_sensor_type(device_cfg) == "leak_sensor"

    def test_local_device_contact_sensor(self):
        device_cfg = {"dp_schema": {"1": {"code": "doorcontact_state", "type": "Boolean"}}}
        assert detect_tuya_sensor_type(device_cfg) == "contact_sensor"

    def test_cloud_device_category_wsdcg_detects_combo(self):
        assert detect_tuya_sensor_type({"category": "wsdcg"}) == "temp_humidity_sensor"

    def test_cloud_device_category_sj_detects_leak(self):
        assert detect_tuya_sensor_type({"category": "sj"}) == "leak_sensor"

    def test_cloud_device_category_mcs_detects_contact(self):
        assert detect_tuya_sensor_type({"category": "mcs"}) == "contact_sensor"

    def test_no_schema_no_matching_category_returns_empty(self):
        assert detect_tuya_sensor_type({"category": "cz"}) == ""
        assert detect_tuya_sensor_type({}) == ""

    def test_dp_schema_takes_priority_over_category(self):
        device_cfg = {"category": "sj", "dp_schema": {"1": {"code": "va_temperature", "type": "Value"}}}
        assert detect_tuya_sensor_type(device_cfg) == "temperature_sensor"


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
