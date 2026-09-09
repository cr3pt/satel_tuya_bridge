import copy
import pathlib

import pytest
import yaml

from dashboard.setup import (
    build_config_dict, validate, build_automations_dict, validate_automations,
    build_satel_fields_dict, build_tuya_devices_dict, validate_satel_fields, validate_tuya_devices,
    validate_cloud_devices, cloud_devices_to_rows, tuya_devices_to_rows,
    build_connections_patch, validate_connections, connections_to_form, list_plugins,
    satel_connection_to_form, build_satel_connection_patch, validate_satel_connection,
    ebus_connection_to_form, build_ebus_connection_patch, validate_ebus_connection,
    build_location_patch, validate_location,
    draft_path_for, load_draft, save_draft, delete_draft,
    motion_rule_to_flow,
)


VALID_FORM = {
    "satel": {"host": "192.168.1.10", "port": "7094", "integration_key": "klucz123", "virtual_inputs_port": "8888"},
    "partitions": [{"name": "Dom", "number": "1"}],
    "monitored_zones": [{"name": "Salon", "number": "12"}],
    "monitored_outputs": [],
    "tuya_devices": [
        {"name": "gniazdko_salon", "id": "dev1", "ip": "192.168.1.50", "local_key": "key1", "type": "socket"},
    ],
    "ebus": {"mqtt_host": "127.0.0.1", "mqtt_port": "1883"},
    "admin_username": "admin",
    "admin_password": "bardzo_bezpieczne_haslo",
    "notifications": {"ntfy_topic_url": ""},
    "location": {"latitude": "", "longitude": ""},
    "home_assistant": {"enabled": False},
    "unifi_protect": {"host": ""},
    "tuya_cloud": {"access_id": ""},
}


class TestValidation:
    def test_valid_form_has_no_errors(self):
        assert validate(VALID_FORM) == []

    def test_missing_satel_host_is_error(self):
        form = {**VALID_FORM, "satel": {**VALID_FORM["satel"], "host": ""}}
        errors = validate(form)
        assert any("Satel" in e and "IP" in e for e in errors)

    def test_missing_integration_key_is_not_an_error(self):
        form = {**VALID_FORM, "satel": {**VALID_FORM["satel"], "integration_key": ""}}
        errors = validate(form)
        assert errors == []

    def test_short_password_is_error(self):
        form = {**VALID_FORM, "admin_password": "krotkie"}
        errors = validate(form)
        assert any("Hasło" in e for e in errors)

    def test_missing_admin_username_is_error(self):
        form = {**VALID_FORM, "admin_username": ""}
        errors = validate(form)
        assert any("użytkownika" in e for e in errors)

    def test_missing_mqtt_host_is_error(self):
        form = {**VALID_FORM, "ebus": {"mqtt_host": "", "mqtt_port": "1883"}}
        errors = validate(form)
        assert any("MQTT" in e for e in errors)

    def test_incomplete_tuya_device_is_error(self):
        form = {**VALID_FORM, "tuya_devices": [{"name": "x", "id": "", "ip": "1.2.3.4", "local_key": "k"}]}
        errors = validate(form)
        assert any("Tuya" in e for e in errors)


class TestBuildConfigDict:
    def test_basic_satel_fields(self):
        config = build_config_dict(VALID_FORM)
        assert config["satel"]["host"] == "192.168.1.10"
        assert config["satel"]["port"] == 7094
        assert config["satel"]["integration_key"] == "klucz123"

    def test_encryption_enabled_defaults_to_false(self):
        config = build_config_dict(VALID_FORM)
        assert config["satel"]["encryption_enabled"] is False

    def test_encryption_enabled_true_when_form_sets_it(self):
        form = {**VALID_FORM, "satel": {**VALID_FORM["satel"], "encryption_enabled": True, "encryption_key": "AESKEY"}}
        config = build_config_dict(form)
        assert config["satel"]["encryption_enabled"] is True
        assert config["satel"]["encryption_key"] == "AESKEY"

    def test_satel_config_has_fields_file_marker_not_inline_fields(self):
        config = build_config_dict(VALID_FORM)
        assert config["satel"]["fields_file"] == "satel_fields.yaml"
        assert "partitions" not in config["satel"]
        assert "monitored_zones" not in config["satel"]
        assert config["satel"]["virtual_inputs"]["inputs"] == {}

    def test_tuya_config_has_devices_file_marker_not_inline_devices(self):
        config = build_config_dict(VALID_FORM)
        assert config["tuya"]["devices_file"] == "tuya_devices.yaml"
        assert "devices" not in config["tuya"]

    def test_partitions_and_zones_converted_to_int(self):
        satel_fields = build_satel_fields_dict(VALID_FORM)
        assert satel_fields["partitions"] == {"Dom": 1}
        assert satel_fields["monitored_zones"] == {"Salon": 12}

    def test_empty_partitions_defaults_to_dom_1(self):
        form = {**VALID_FORM, "partitions": []}
        satel_fields = build_satel_fields_dict(form)
        assert satel_fields["partitions"] == {"Dom": 1}

    def test_tuya_devices_structure(self):
        tuya_devices = build_tuya_devices_dict(VALID_FORM)
        assert "gniazdko_salon" in tuya_devices["devices"]
        assert tuya_devices["devices"]["gniazdko_salon"]["ip"] == "192.168.1.50"

    def test_dimmer_device_gets_dp_fields(self):
        form = {**VALID_FORM, "tuya_devices": [
            {"name": "d", "id": "x", "ip": "1.2.3.4", "local_key": "k", "type": "dimmer", "dimmer_dp": "3"},
        ]}
        tuya_devices = build_tuya_devices_dict(form)
        assert tuya_devices["devices"]["d"]["dimmer_dp"] == "3"
        assert "dimmer_range" in tuya_devices["devices"]["d"]

    def test_password_is_hashed_not_plaintext(self):
        config = build_config_dict(VALID_FORM)
        stored = config["dashboard"]["auth"]["password_hash"]
        assert stored != VALID_FORM["admin_password"]
        assert stored.startswith("scrypt:") or stored.startswith("pbkdf2:")

    def test_username_is_stored_as_plaintext(self):
        form = {**VALID_FORM, "admin_username": "szef"}
        config = build_config_dict(form)
        assert config["dashboard"]["auth"]["username"] == "szef"

    def test_missing_admin_username_defaults_to_admin(self):
        form = {**VALID_FORM, "admin_username": ""}
        config = build_config_dict(form)
        assert config["dashboard"]["auth"]["username"] == "admin"

    def test_advanced_sections_get_empty_safe_defaults(self):
        config = build_config_dict(VALID_FORM)
        assert config["alarm_rules"] == []
        assert config["automations_file"] == "automations.yaml"
        assert "scenes" not in config
        assert "reguły_ruchu" not in config
        assert "presence_simulation" not in config

    def test_automations_dict_has_empty_safe_defaults(self):
        automations = build_automations_dict(VALID_FORM)
        assert automations["reguły_ruchu"] == []
        assert automations["scenes"] == []
        assert automations["presence_simulation"] == {"enabled": False}

    def test_location_omitted_when_not_provided(self):
        config = build_config_dict(VALID_FORM)
        assert "location" not in config

    def test_location_included_when_provided(self):
        form = {**VALID_FORM, "location": {"latitude": "52.23", "longitude": "21.01"}}
        config = build_config_dict(form)
        assert config["location"] == {"latitude": 52.23, "longitude": 21.01}

    def test_home_assistant_omitted_when_disabled(self):
        config = build_config_dict(VALID_FORM)
        assert "home_assistant" not in config

    def test_home_assistant_included_when_enabled(self):
        form = {**VALID_FORM, "home_assistant": {"enabled": True, "mqtt_host": ""}}
        config = build_config_dict(form)
        assert config["home_assistant"]["enabled"] is True
        assert config["home_assistant"]["mqtt_host"] == "127.0.0.1"

    def test_unifi_protect_omitted_when_no_host(self):
        config = build_config_dict(VALID_FORM)
        assert "unifi_protect" not in config

    def test_unifi_protect_included_when_host_given(self):
        form = {**VALID_FORM, "unifi_protect": {"host": "192.168.1.5", "username": "admin", "password": "x"}}
        config = build_config_dict(form)
        assert config["unifi_protect"]["host"] == "192.168.1.5"

    def test_tuya_cloud_uses_region_derived_endpoints_from_wizard(self):
        form = {**VALID_FORM, "tuya_cloud": {
            "enabled": True, "access_id": "id1", "access_key": "key1",
            "api_endpoint": "https://openapi.tuyaus.com", "mq_endpoint": "wss://mqe.tuyaus.com:8285/",
        }}
        config = build_config_dict(form)
        assert config["tuya"]["cloud"]["api_endpoint"] == "https://openapi.tuyaus.com"
        assert config["tuya"]["cloud"]["mq_endpoint"] == "wss://mqe.tuyaus.com:8285/"

    def test_tuya_cloud_blank_endpoints_fall_back_to_eu_defaults(self):
        form = {**VALID_FORM, "tuya_cloud": {
            "enabled": True, "access_id": "id1", "access_key": "key1",
            "api_endpoint": "", "mq_endpoint": "",
        }}
        config = build_config_dict(form)
        assert config["tuya"]["cloud"]["api_endpoint"] == "https://openapi.tuyaeu.com"
        assert config["tuya"]["cloud"]["mq_endpoint"] == "wss://mqe.tuyaeu.com:8285/"

    def test_tuya_cloud_persists_api_region(self):
        form = {**VALID_FORM, "tuya_cloud": {
            "enabled": True, "access_id": "id1", "access_key": "key1", "api_region": "us",
        }}
        config = build_config_dict(form)
        assert config["tuya"]["cloud"]["api_region"] == "us"

    def test_tuya_cloud_missing_api_region_defaults_to_eu(self):
        form = {**VALID_FORM, "tuya_cloud": {"enabled": True, "access_id": "id1", "access_key": "key1"}}
        config = build_config_dict(form)
        assert config["tuya"]["cloud"]["api_region"] == "eu"

    def test_result_is_yaml_serializable(self):
        config = build_config_dict(VALID_FORM)
        dumped = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        reloaded = yaml.safe_load(dumped)
        assert reloaded["satel"]["host"] == "192.168.1.10"


class TestSatelFieldsAndTuyaDevicesBuilders:

    def test_build_satel_fields_shape(self):
        form = {
            "partitions": [{"name": "Dom", "number": "1"}, {"name": "Garaz", "number": "2"}],
            "monitored_zones": [{"name": "Salon", "number": "12", "type": "ruch"}],
            "monitored_outputs": [{"name": "Syrena", "number": "1", "type": "syrena"}],
            "virtual_inputs": [{"name": "motion_hall_camera", "number": "17"}],
        }
        fields = build_satel_fields_dict(form)
        assert fields == {
            "partitions": {"Dom": 1, "Garaz": 2},
            "monitored_zones": {"Salon": 12},
            "monitored_outputs": {"Syrena": 1},
            "virtual_inputs": {"motion_hall_camera": 17},
            "zone_types": {"Salon": "ruch"},
            "output_types": {"Syrena": "syrena"},
        }

    def test_build_satel_fields_shape_same_regardless_of_mode(self):
        form = {
            "virtual_inputs": [{"name": "motion_hall_camera", "number": "17"}],
        }
        fields = build_satel_fields_dict(form)
        assert fields["virtual_inputs"] == {"motion_hall_camera": 17}

    def test_build_satel_fields_type_ignored_when_unknown_slug(self):
        form = {"monitored_zones": [{"name": "Salon", "number": "12", "type": "cos-nieznanego"}]}
        fields = build_satel_fields_dict(form)
        assert fields["zone_types"] == {}

    def test_build_satel_fields_partitions_default_to_dom_1_when_empty(self):
        fields = build_satel_fields_dict({"partitions": []})
        assert fields["partitions"] == {"Dom": 1}

    def test_build_satel_fields_rows_missing_number_are_skipped(self):
        form = {"monitored_zones": [{"name": "Salon", "number": ""}, {"name": "", "number": "5"}]}
        fields = build_satel_fields_dict(form)
        assert fields["monitored_zones"] == {}

    def test_build_satel_fields_defaults_when_form_empty(self):
        fields = build_satel_fields_dict({})
        assert fields == {
            "partitions": {"Dom": 1}, "monitored_zones": {}, "monitored_outputs": {}, "virtual_inputs": {},
            "zone_types": {}, "output_types": {},
        }

    def test_validate_satel_fields_no_errors_when_complete(self):
        form = {"virtual_inputs": [{"name": "x", "number": "1"}]}
        assert validate_satel_fields(form) == []

    def test_validate_satel_fields_incomplete_virtual_input_is_error(self):
        form = {"virtual_inputs": [{"name": "x", "number": ""}]}
        errors = validate_satel_fields(form)
        assert any("Wirtualne wejście" in e for e in errors)

    def test_validate_satel_fields_does_not_require_zone_number_pairs(self):
        form = {"monitored_zones": [{"name": "x", "number": ""}]}
        assert validate_satel_fields(form) == []

    def test_build_tuya_devices_shape(self):
        form = {"tuya_devices": [
            {"name": "gniazdko", "id": "d1", "ip": "1.2.3.4", "local_key": "k", "type": "socket"},
        ]}
        devices = build_tuya_devices_dict(form)
        assert devices == {
            "devices": {
                "gniazdko": {"id": "d1", "ip": "1.2.3.4", "local_key": "k", "version": 3.4, "type": "socket"},
            },
            "zigbee_gateways": {},
            "cloud_devices": {},
        }

    def test_build_tuya_devices_zigbee_sub_device_has_no_ip_or_local_key(self):
        form = {
            "tuya_devices": [
                {"name": "lampka", "id": "sub1", "gateway": "brama", "cid": "node1", "type": "socket"},
            ],
            "zigbee_gateways": [
                {"name": "brama", "id": "gw1", "ip": "1.2.3.4", "local_key": "gwkey", "version": "3.4"},
            ],
        }
        result = build_tuya_devices_dict(form)
        assert result["devices"]["lampka"] == {"id": "sub1", "gateway": "brama", "cid": "node1", "type": "socket"}
        assert result["zigbee_gateways"] == {
            "brama": {"id": "gw1", "ip": "1.2.3.4", "local_key": "gwkey", "version": 3.4},
        }

    def test_build_tuya_devices_zigbee_sub_device_cid_defaults_to_omitted_when_blank(self):
        form = {
            "tuya_devices": [{"name": "lampka", "id": "sub1", "gateway": "brama", "type": "socket"}],
            "zigbee_gateways": [{"name": "brama", "id": "gw1", "ip": "1.2.3.4", "local_key": "gwkey"}],
        }
        result = build_tuya_devices_dict(form)
        assert "cid" not in result["devices"]["lampka"]

    def test_build_tuya_devices_dimmer_gets_extra_fields(self):
        form = {"tuya_devices": [
            {"name": "d", "id": "x", "ip": "1.2.3.4", "local_key": "k", "type": "dimmer", "dimmer_dp": "3"},
        ]}
        devices = build_tuya_devices_dict(form)
        assert devices["devices"]["d"]["dimmer_dp"] == "3"
        assert devices["devices"]["d"]["dimmer_range"] == [10, 1000]

    def test_build_tuya_devices_empty_form(self):
        assert build_tuya_devices_dict({}) == {"devices": {}, "zigbee_gateways": {}, "cloud_devices": {}}

    def test_build_tuya_devices_preserves_submitted_dp_schema(self):
        schema = {"1": {"code": "switch_1", "type": "Boolean", "values": "{}"}}
        form = {"tuya_devices": [
            {"name": "d", "id": "x", "ip": "1.2.3.4", "local_key": "k", "type": "socket", "dp_schema": schema},
        ]}
        devices = build_tuya_devices_dict(form)
        assert devices["devices"]["d"]["dp_schema"] == schema

    def test_build_tuya_devices_omits_dp_schema_when_absent(self):
        form = {"tuya_devices": [
            {"name": "d", "id": "x", "ip": "1.2.3.4", "local_key": "k", "type": "socket", "dp_schema": {}},
        ]}
        devices = build_tuya_devices_dict(form)
        assert "dp_schema" not in devices["devices"]["d"]

    def test_tuya_devices_to_rows_includes_dp_schema(self):
        schema = {"1": {"code": "switch_1", "type": "Boolean", "values": "{}"}}
        rows = tuya_devices_to_rows({"d": {"id": "x", "dp_schema": schema}})
        assert rows[0]["dp_schema"] == schema

    def test_tuya_devices_to_rows_defaults_dp_schema_to_empty_dict(self):
        rows = tuya_devices_to_rows({"d": {"id": "x"}})
        assert rows[0]["dp_schema"] == {}

    def test_validate_tuya_devices_no_errors_when_complete(self):
        form = {"tuya_devices": [{"name": "x", "id": "i", "ip": "1.2.3.4", "local_key": "k"}]}
        assert validate_tuya_devices(form) == []

    def test_validate_tuya_devices_zigbee_sub_device_requires_existing_gateway(self):
        form = {
            "tuya_devices": [{"name": "lampka", "id": "sub1", "gateway": "nieznana_brama"}],
        }
        errors = validate_tuya_devices(form)
        assert any("nieznana_brama" in e for e in errors)

    def test_validate_tuya_devices_zigbee_sub_device_no_errors_when_gateway_exists(self):
        form = {
            "tuya_devices": [{"name": "lampka", "id": "sub1", "gateway": "brama"}],
            "zigbee_gateways": [{"name": "brama", "id": "gw1", "ip": "1.2.3.4", "local_key": "gwkey"}],
        }
        assert validate_tuya_devices(form) == []

    def test_validate_tuya_devices_incomplete_is_error(self):
        form = {"tuya_devices": [{"name": "x", "id": "", "ip": "1.2.3.4", "local_key": "k"}]}
        errors = validate_tuya_devices(form)
        assert any("Tuya" in e for e in errors)

    def test_satel_fields_and_tuya_devices_are_yaml_serializable(self):
        form = {
            "partitions": [{"name": "Dom", "number": "1"}],
            "virtual_inputs": [{"name": "x", "number": "1"}],
            "tuya_devices": [{"name": "d", "id": "i", "ip": "1.2.3.4", "local_key": "k"}],
        }
        dumped_satel = yaml.safe_dump(build_satel_fields_dict(form), allow_unicode=True, sort_keys=False)
        dumped_tuya = yaml.safe_dump(build_tuya_devices_dict(form), allow_unicode=True, sort_keys=False)
        assert yaml.safe_load(dumped_satel)["partitions"] == {"Dom": 1}
        assert yaml.safe_load(dumped_tuya)["devices"]["d"]["ip"] == "1.2.3.4"


class TestCloudDevicesBuilder:

    def test_build_cloud_devices_requires_only_name_and_id(self):
        form = {"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]}
        devices = build_tuya_devices_dict(form)["cloud_devices"]
        assert devices == {"lampa_taras": {"id": "c1", "category": "", "switch_code": "switch_1"}}

    def test_build_cloud_devices_keeps_category_and_switch_code(self):
        form = {"cloud_devices": [{"name": "wentylator", "id": "c2", "category": "fs", "switch_code": "switch"}]}
        devices = build_tuya_devices_dict(form)["cloud_devices"]
        assert devices["wentylator"] == {"id": "c2", "category": "fs", "switch_code": "switch"}

    def test_build_cloud_devices_skips_incomplete_rows(self):
        form = {"cloud_devices": [{"name": "", "id": "c1"}, {"name": "x", "id": ""}]}
        assert build_tuya_devices_dict(form)["cloud_devices"] == {}

    def test_validate_cloud_devices_no_errors_when_name_and_id_present(self):
        form = {"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]}
        assert validate_cloud_devices(form) == []

    def test_validate_cloud_devices_does_not_require_ip_or_local_key(self):
        form = {"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]}
        errors = validate_cloud_devices(form)
        assert not any("local_key" in e or "adres IP" in e for e in errors)

    def test_validate_cloud_devices_missing_name_is_error(self):
        form = {"cloud_devices": [{"name": "", "id": "c1"}]}
        errors = validate_cloud_devices(form)
        assert any("nazwa i ID" in e for e in errors)

    def test_cloud_devices_to_rows_roundtrip(self):
        devices = {"lampa_taras": {"id": "c1", "category": "kg", "switch_code": "switch_1"}}
        assert cloud_devices_to_rows(devices) == [
            {"name": "lampa_taras", "id": "c1", "category": "kg", "switch_code": "switch_1"},
        ]

    def test_build_tuya_devices_dict_includes_cloud_devices_key(self):
        form = {"cloud_devices": [{"name": f"urzadzenie_{i}", "id": f"id{i}"} for i in range(40)]}
        assert validate_cloud_devices(form) == []
        result = build_tuya_devices_dict(form)
        assert len(result["cloud_devices"]) == 40
        assert result["devices"] == {}


class TestConnectionsBuilder:

    EXISTING = {
        "satel": {
            "host": "192.168.1.10", "port": 7094, "integration_key": "stary_klucz", "partition": 1,
            "virtual_inputs": {"port": 8888, "url_template": "http://{host}:{port}/{name}"},
            "outputs": {}, "fields_file": "satel_fields.yaml",
        },
        "tuya": {"status_poll_interval_seconds": 30, "devices_file": "tuya_devices.yaml"},
        "ebus": {"mqtt_host": "127.0.0.1", "mqtt_port": 1883, "sensors_file": "sensors.yaml"},
        "boiler_output": {"mqtt": {"host": "127.0.0.1", "port": 1883, "topic_prefix": "home/boiler"},
                           "tuya_virtual_device": {"device_id": ""}},
        "notifications": {"ntfy_topic_url": "", "grace_seconds": 120},
        "reguły_kamera_alarm": [{"source": "unifi_protect", "camera_id": "cam1", "action": "arm"}],
        "boiler_rules": [{"some": "rule"}],
        "maintenance": {"purge_enabled": True, "retention_days": 90},
        "dashboard": {"auth": {"password_hash": "already-hashed"}},
    }

    def test_preserves_unrelated_sections(self):
        form = {"satel": {"host": "10.0.0.5"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["reguły_kamera_alarm"] == self.EXISTING["reguły_kamera_alarm"]
        assert patched["boiler_rules"] == self.EXISTING["boiler_rules"]
        assert patched["maintenance"] == self.EXISTING["maintenance"]
        assert patched["dashboard"] == self.EXISTING["dashboard"]

    def test_updates_satel_host_and_port(self):
        form = {"satel": {"host": "10.0.0.5", "port": "7095"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["satel"]["host"] == "10.0.0.5"
        assert patched["satel"]["port"] == 7095

    def test_blank_integration_key_keeps_existing_secret(self):
        form = {"satel": {"host": "192.168.1.10", "integration_key": ""}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["satel"]["integration_key"] == "stary_klucz"

    def test_non_blank_integration_key_overwrites(self):
        form = {"satel": {"host": "192.168.1.10", "integration_key": "nowy_klucz"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["satel"]["integration_key"] == "nowy_klucz"

    def test_encryption_enabled_defaults_to_false_when_absent_from_form(self):
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["satel"]["encryption_enabled"] is False

    def test_encryption_enabled_true_persists(self):
        form = {
            "satel": {"host": "192.168.1.10", "encryption_enabled": True},
            "ebus": {"mqtt_host": "127.0.0.1"},
        }
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["satel"]["encryption_enabled"] is True

    def test_encryption_enabled_can_be_turned_back_off(self):
        existing = copy.deepcopy(self.EXISTING)
        existing["satel"]["encryption_enabled"] = True
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, existing)
        assert patched["satel"]["encryption_enabled"] is False

    def test_ebus_topic_prefix_updates_boiler_output_mqtt(self):
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1", "topic_prefix": "ebusd_state"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["boiler_output"]["mqtt"]["topic_prefix"] == "ebusd_state"

    def test_mqtt_publish_topics_default_to_satel_and_tuya(self):
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["mqtt_publish"] == {
            "enabled": True, "topics": {"satel": "satel", "tuya": "tuya", "homewhiz": "homewhiz"},
        }

    def test_mqtt_publish_topics_configurable(self):
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "mqtt_publish": {
                "enabled": True, "satel_topic": "alarm", "tuya_topic": "smart_home",
                "homewhiz_topic": "agd",
            },
        }
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["mqtt_publish"]["topics"] == {"satel": "alarm", "tuya": "smart_home", "homewhiz": "agd"}

    def test_mqtt_publish_can_be_disabled(self):
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "mqtt_publish": {"enabled": False},
        }
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["mqtt_publish"]["enabled"] is False

    def test_tuya_cloud_enabled_sets_cloud_section(self):
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya_cloud": {"enabled": True, "access_id": "id1", "access_key": "secret1"},
        }
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["tuya"]["cloud"]["access_id"] == "id1"
        assert patched["tuya"]["cloud"]["access_key"] == "secret1"

    def test_tuya_cloud_save_preserves_api_region_set_elsewhere(self):
        existing = {**self.EXISTING, "tuya": {**self.EXISTING["tuya"], "cloud": {
            "access_id": "id1", "access_key": "secret1", "api_region": "us",
            "api_endpoint": "https://openapi.tuyaus.com", "mq_endpoint": "wss://mqe.tuyaus.com:8285/",
            "camera_device_ids": ["cam1"],
        }}}
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya_cloud": {"enabled": True, "access_id": "id1", "access_key": ""},
        }
        patched = build_connections_patch(form, existing)
        assert patched["tuya"]["cloud"]["api_region"] == "us"

    def test_tuya_cloud_new_config_without_region_defaults_to_eu(self):
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya_cloud": {"enabled": True, "access_id": "id1", "access_key": "secret1"},
        }
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["tuya"]["cloud"]["api_region"] == "eu"

    def test_tuya_cloud_blank_secret_keeps_existing(self):
        existing = {**self.EXISTING, "tuya": {**self.EXISTING["tuya"], "cloud": {
            "access_id": "id1", "access_key": "old_secret", "api_endpoint": "https://openapi.tuyaeu.com",
            "mq_endpoint": "wss://mqe.tuyaeu.com:8285/", "camera_device_ids": ["cam1"],
        }}}
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya_cloud": {"enabled": True, "access_id": "id1", "access_key": ""},
        }
        patched = build_connections_patch(form, existing)
        assert patched["tuya"]["cloud"]["access_key"] == "old_secret"
        assert patched["tuya"]["cloud"]["camera_device_ids"] == ["cam1"]

    def test_tuya_cloud_disabled_removes_cloud_section(self):
        existing = {**self.EXISTING, "tuya": {**self.EXISTING["tuya"], "cloud": {"access_id": "id1"}}}
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"}, "tuya_cloud": {"enabled": False}}
        patched = build_connections_patch(form, existing)
        assert "cloud" not in patched["tuya"]

    def test_home_assistant_enabled_defaults_mqtt_host_to_ebus(self):
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "home_assistant": {"enabled": True},
        }
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["home_assistant"] == {
            "enabled": True, "mqtt_host": "127.0.0.1", "mqtt_port": 1883,
            "discovery_prefix": "homeassistant", "state_topic_prefix": "home/bridge",
            "device_name": "Satel-Tuya-eBUS Bridge",
        }

    def test_home_assistant_disabled_stays_disabled(self):
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["home_assistant"]["enabled"] is False

    def test_mqtt_publish_preserves_log_level_set_via_dedicated_page(self):
        existing = {**self.EXISTING, "mqtt_publish": {"enabled": True, "topics": {"satel": "satel", "tuya": "tuya"}, "log_level": "DEBUG"}}
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "mqtt_publish": {"enabled": True, "satel_topic": "alarm", "tuya_topic": "smart_home"},
        }
        patched = build_connections_patch(form, existing)
        assert patched["mqtt_publish"]["log_level"] == "DEBUG"
        assert patched["mqtt_publish"]["topics"] == {"satel": "alarm", "tuya": "smart_home", "homewhiz": "homewhiz"}

    def test_tuya_cloud_preserves_log_level_and_real_enabled_flag(self):
        existing = {**self.EXISTING, "tuya": {**self.EXISTING["tuya"], "cloud": {
            "access_id": "id1", "access_key": "secret1", "api_endpoint": "https://openapi.tuyaeu.com",
            "mq_endpoint": "wss://mqe.tuyaeu.com:8285/", "camera_device_ids": [],
            "enabled": False, "log_level": "WARNING",
        }}}
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya_cloud": {"enabled": True, "access_id": "id1", "access_key": ""},
        }
        patched = build_connections_patch(form, existing)
        assert patched["tuya"]["cloud"]["log_level"] == "WARNING"
        assert patched["tuya"]["cloud"]["enabled"] is False

    def test_home_assistant_preserves_log_level_set_via_dedicated_page(self):
        existing = {**self.EXISTING, "home_assistant": {"enabled": True, "mqtt_host": "127.0.0.1", "mqtt_port": 1883, "log_level": "ERROR"}}
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "home_assistant": {"enabled": True, "mqtt_host": "10.0.0.9"},
        }
        patched = build_connections_patch(form, existing)
        assert patched["home_assistant"]["log_level"] == "ERROR"
        assert patched["home_assistant"]["mqtt_host"] == "10.0.0.9"

    def test_result_is_yaml_serializable(self):
        form = {
            "satel": {"host": "192.168.1.10", "integration_key": "k"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya_cloud": {"enabled": True, "access_id": "id1", "access_key": "s1"},
            "home_assistant": {"enabled": True},
        }
        patched = build_connections_patch(form, self.EXISTING)
        dumped = yaml.safe_dump(patched, allow_unicode=True, sort_keys=False)
        assert yaml.safe_load(dumped)["satel"]["host"] == "192.168.1.10"


class TestValidateConnections:
    def test_no_errors_when_complete(self):
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        assert validate_connections(form) == []

    def test_missing_satel_host_is_error(self):
        errors = validate_connections({"satel": {}, "ebus": {"mqtt_host": "127.0.0.1"}})
        assert any("Satel" in e for e in errors)

    def test_missing_ebus_mqtt_host_is_error(self):
        errors = validate_connections({"satel": {"host": "1.2.3.4"}, "ebus": {}})
        assert any("eBUS" in e for e in errors)

    def test_tuya_cloud_enabled_without_access_id_is_error(self):
        form = {
            "satel": {"host": "1.2.3.4"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya_cloud": {"enabled": True, "access_id": ""},
        }
        errors = validate_connections(form)
        assert any("Tuya Cloud" in e for e in errors)


class TestLocationBuilder:

    EXISTING = {
        "satel": {"host": "1.2.3.4"},
        "reguły_kamera_alarm": [{"source": "unifi_protect", "camera_id": "cam1"}],
    }

    def test_sets_location_from_form(self):
        patched = build_location_patch({"latitude": "52.23", "longitude": "21.01"}, self.EXISTING)
        assert patched["location"] == {"latitude": 52.23, "longitude": 21.01}

    def test_preserves_unrelated_sections(self):
        patched = build_location_patch({"latitude": "52.23", "longitude": "21.01"}, self.EXISTING)
        assert patched["satel"] == self.EXISTING["satel"]
        assert patched["reguły_kamera_alarm"] == self.EXISTING["reguły_kamera_alarm"]

    def test_blank_fields_remove_existing_location(self):
        existing = {**self.EXISTING, "location": {"latitude": 52.23, "longitude": 21.01}}
        patched = build_location_patch({"latitude": "", "longitude": ""}, existing)
        assert "location" not in patched

    def test_missing_fields_remove_existing_location(self):
        existing = {**self.EXISTING, "location": {"latitude": 52.23, "longitude": 21.01}}
        patched = build_location_patch({}, existing)
        assert "location" not in patched

    def test_overwrites_existing_location(self):
        existing = {**self.EXISTING, "location": {"latitude": 1.0, "longitude": 2.0}}
        patched = build_location_patch({"latitude": "52.23", "longitude": "21.01"}, existing)
        assert patched["location"] == {"latitude": 52.23, "longitude": 21.01}


class TestValidateLocation:
    def test_both_blank_is_valid(self):
        assert validate_location({"latitude": "", "longitude": ""}) == []

    def test_missing_fields_is_valid(self):
        assert validate_location({}) == []

    def test_valid_coordinates_no_errors(self):
        assert validate_location({"latitude": "52.23", "longitude": "21.01"}) == []

    def test_only_latitude_is_error(self):
        errors = validate_location({"latitude": "52.23", "longitude": ""})
        assert any("oba pola" in e for e in errors)

    def test_only_longitude_is_error(self):
        errors = validate_location({"latitude": "", "longitude": "21.01"})
        assert any("oba pola" in e for e in errors)

    def test_non_numeric_is_error(self):
        errors = validate_location({"latitude": "abc", "longitude": "21.01"})
        assert any("liczbami" in e for e in errors)

    def test_latitude_out_of_range_is_error(self):
        errors = validate_location({"latitude": "95", "longitude": "21.01"})
        assert any("-90..90" in e for e in errors)

    def test_longitude_out_of_range_is_error(self):
        errors = validate_location({"latitude": "52.23", "longitude": "200"})
        assert any("-180..180" in e for e in errors)


class TestConnectionsToForm:
    def test_never_exposes_secrets(self):
        existing = {
            "satel": {"host": "1.2.3.4", "integration_key": "sekret"},
            "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya": {"cloud": {"access_id": "id1", "access_key": "sekretny_klucz"}},
        }
        form = connections_to_form(existing)
        dumped = str(form)
        assert "sekret" not in dumped
        assert form["satel"]["integration_key_set"] is True
        assert form["tuya_cloud"]["access_key_set"] is True

    def test_reports_unset_secrets(self):
        existing = {"satel": {"host": "1.2.3.4"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        form = connections_to_form(existing)
        assert form["satel"]["integration_key_set"] is False
        assert form["tuya_cloud"]["enabled"] is False

    def test_exposes_tuya_cloud_api_region(self):
        existing = {
            "satel": {"host": "1.2.3.4"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "tuya": {"cloud": {"access_id": "id1", "api_region": "us"}},
        }
        form = connections_to_form(existing)
        assert form["tuya_cloud"]["api_region"] == "us"

    def test_exposes_encryption_enabled_flag(self):
        existing = {
            "satel": {"host": "1.2.3.4", "encryption_key": "AESKEY", "encryption_enabled": True},
            "ebus": {"mqtt_host": "127.0.0.1"},
        }
        form = connections_to_form(existing)
        assert form["satel"]["encryption_enabled"] is True
        assert form["satel"]["encryption_key_set"] is True

    def test_encryption_enabled_defaults_to_false(self):
        existing = {"satel": {"host": "1.2.3.4"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        form = connections_to_form(existing)
        assert form["satel"]["encryption_enabled"] is False

    def test_roundtrip_with_build_connections_patch(self):
        existing = TestConnectionsBuilder.EXISTING
        form = connections_to_form(existing)
        browser_form = {
            "satel": {
                "host": form["satel"]["host"], "port": str(form["satel"]["port"]),
                "partition": str(form["satel"]["partition"]),
                "virtual_inputs_port": str(form["satel"]["virtual_inputs_port"]),
                "integration_key": "",
            },
            "ebus": {"mqtt_host": form["ebus"]["mqtt_host"], "mqtt_port": str(form["ebus"]["mqtt_port"])},
        }
        patched = build_connections_patch(browser_form, existing)
        assert patched["satel"]["host"] == existing["satel"]["host"]
        assert patched["satel"]["integration_key"] == existing["satel"]["integration_key"]


class TestSatelConnectionForm:

    def test_validate_requires_host(self):
        assert validate_satel_connection({"host": ""}) != []
        assert validate_satel_connection({"host": "1.2.3.4"}) == []

    def test_to_form_never_exposes_secrets(self):
        section = {"host": "1.2.3.4", "integration_key": "sekret", "encryption_key": "AESKEY"}
        form = satel_connection_to_form(section)
        assert "sekret" not in str(form)
        assert "AESKEY" not in str(form)
        assert form["integration_key_set"] is True
        assert form["encryption_key_set"] is True

    def test_to_form_reports_unset_secrets_and_defaults(self):
        form = satel_connection_to_form({"host": "1.2.3.4"})
        assert form["integration_key_set"] is False
        assert form["encryption_key_set"] is False
        assert form["encryption_enabled"] is False
        assert form["virtual_inputs_mode"] == "library"
        assert form["port"] == 7094

    def test_patch_updates_host_and_port(self):
        existing = {"host": "1.2.3.4", "port": 7094, "integration_key": "x"}
        patched = build_satel_connection_patch({"host": "10.0.0.9", "port": "7095"}, existing)
        assert patched["host"] == "10.0.0.9"
        assert patched["port"] == 7095

    def test_patch_blank_integration_key_preserves_existing(self):
        existing = {"host": "1.2.3.4", "integration_key": "sekret"}
        patched = build_satel_connection_patch({"host": "1.2.3.4", "integration_key": ""}, existing)
        assert patched["integration_key"] == "sekret"

    def test_patch_non_blank_integration_key_overwrites(self):
        existing = {"host": "1.2.3.4", "integration_key": "stary"}
        patched = build_satel_connection_patch({"host": "1.2.3.4", "integration_key": "nowy"}, existing)
        assert patched["integration_key"] == "nowy"

    def test_patch_encryption_key_clear(self):
        existing = {"host": "1.2.3.4", "encryption_key": "AESKEY"}
        patched = build_satel_connection_patch(
            {"host": "1.2.3.4", "encryption_key_clear": True}, existing,
        )
        assert patched["encryption_key"] is None

    def test_patch_does_not_store_url_template(self):
        patched = build_satel_connection_patch({"host": "1.2.3.4"}, {"host": "1.2.3.4"})
        assert "url_template" not in patched["virtual_inputs"]

    def test_patch_preserves_fields_not_in_this_form(self):
        existing = {
            "host": "1.2.3.4", "status_poll_interval_seconds": 5, "outputs": {"a": 1},
            "fields_file": "custom.yaml",
        }
        patched = build_satel_connection_patch({"host": "1.2.3.4"}, existing)
        assert patched["status_poll_interval_seconds"] == 5
        assert patched["outputs"] == {"a": 1}
        assert patched["fields_file"] == "custom.yaml"

    def test_roundtrip(self):
        existing = {
            "host": "1.2.3.4", "port": 7094, "partition": 1, "integration_key": "sekret",
            "virtual_inputs": {"mode": "library", "port": 8888},
        }
        form = satel_connection_to_form(existing)
        browser_form = {
            "host": form["host"], "port": str(form["port"]), "partition": str(form["partition"]),
            "virtual_inputs_mode": form["virtual_inputs_mode"],
            "virtual_inputs_port": str(form["virtual_inputs_port"]), "integration_key": "",
        }
        patched = build_satel_connection_patch(browser_form, existing)
        assert patched["host"] == existing["host"]
        assert patched["integration_key"] == existing["integration_key"]


class TestEbusConnectionForm:

    def test_validate_requires_mqtt_host(self):
        assert validate_ebus_connection({"mqtt_host": ""}) != []
        assert validate_ebus_connection({"mqtt_host": "127.0.0.1"}) == []

    def test_to_form_reads_topic_prefix_from_boiler_output(self):
        form = ebus_connection_to_form(
            {"mqtt_host": "127.0.0.1", "mqtt_port": 1884},
            {"mqtt": {"topic_prefix": "dom/kociol"}},
        )
        assert form == {"mqtt_host": "127.0.0.1", "mqtt_port": 1884, "topic_prefix": "dom/kociol"}

    def test_to_form_defaults_when_unset(self):
        form = ebus_connection_to_form({}, {})
        assert form == {"mqtt_host": "", "mqtt_port": 1883, "topic_prefix": "home/boiler"}

    def test_patch_mirrors_host_and_port_into_boiler_output_mqtt(self):
        existing = {"ebus": {"mqtt_host": "1.2.3.4", "mqtt_port": 1883}, "satel": {"host": "9.9.9.9"}}
        patched = build_ebus_connection_patch({"mqtt_host": "10.0.0.5", "mqtt_port": "1884"}, existing)
        assert patched["ebus"]["mqtt_host"] == "10.0.0.5"
        assert patched["ebus"]["mqtt_port"] == 1884
        assert patched["boiler_output"]["mqtt"]["host"] == "10.0.0.5"
        assert patched["boiler_output"]["mqtt"]["port"] == 1884
        assert patched["satel"]["host"] == "9.9.9.9"

    def test_patch_updates_topic_prefix(self):
        existing = {"ebus": {"mqtt_host": "1.2.3.4"}, "boiler_output": {"mqtt": {"topic_prefix": "home/boiler"}}}
        patched = build_ebus_connection_patch(
            {"mqtt_host": "1.2.3.4", "topic_prefix": "dom/kociol"}, existing,
        )
        assert patched["boiler_output"]["mqtt"]["topic_prefix"] == "dom/kociol"

    def test_patch_blank_topic_prefix_preserves_existing(self):
        existing = {"ebus": {"mqtt_host": "1.2.3.4"}, "boiler_output": {"mqtt": {"topic_prefix": "dom/kociol"}}}
        patched = build_ebus_connection_patch({"mqtt_host": "1.2.3.4", "topic_prefix": ""}, existing)
        assert patched["boiler_output"]["mqtt"]["topic_prefix"] == "dom/kociol"


@pytest.fixture
def setup_client(tmp_path):
    import dashboard.app as app_module

    app_module.history = None
    app_module.config_store = None
    app_module.auth_guard = None
    app_module.satel_partitions_cfg = {}
    app_module.config_path = tmp_path / "config.yaml"
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), tmp_path


class TestSetupRedirect:
    async def test_index_redirects_to_setup_when_unconfigured(self, setup_client):
        client, _ = setup_client
        resp = (await client.get("/", follow_redirects=False))
        assert resp.status_code == 302
        assert "/setup" in resp.headers["Location"]

    async def test_plugins_page_redirects_to_setup_when_unconfigured(self, setup_client):
        client, _ = setup_client
        resp = (await client.get("/plugins", follow_redirects=False))
        assert resp.status_code == 302

    async def test_setup_page_itself_loads_without_redirect(self, setup_client):
        client, _ = setup_client
        resp = (await client.get("/setup"))
        assert resp.status_code == 200

    async def test_satel_connection_test_reachable_without_redirect(self, setup_client, monkeypatch):
        client, _ = setup_client

        async def fake_test_connection(host, port, **_kwargs):
            return None

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        resp = (await client.post("/api/setup/test-satel-connection", json={"host": "192.168.1.10", "port": 7094}))
        assert resp.status_code == 200

    async def test_discover_tuya_cloud_reachable_without_redirect(self, setup_client, monkeypatch):
        client, _ = setup_client
        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", lambda *a, **kw: [])
        resp = (await client.post(
            "/api/discover/tuya-cloud",
            json={"api_region": "eu", "access_id": "x", "access_key": "y"},
        ))
        assert resp.status_code == 200

    async def test_discover_tuya_lan_scan_reachable_without_redirect(self, setup_client, monkeypatch):
        client, _ = setup_client
        monkeypatch.setattr("dashboard.app.scan_tuya_network", lambda: [])
        resp = (await client.post("/api/discover/tuya", json={}))
        assert resp.status_code == 200


class TestSetupSave:
    async def test_save_creates_config_db(self, setup_client):
        client, tmp_path = setup_client
        resp = (await client.post("/api/setup/save", json=VALID_FORM))
        assert resp.status_code == 200
        assert (tmp_path / "config.db").exists()

        import dashboard.app as app_module
        assert app_module.config_store.get("config")["satel"]["host"]
        assert app_module.config_store.has("sensors")

    async def test_save_with_errors_returns_400(self, setup_client):
        client, _ = setup_client
        bad_form = {**VALID_FORM, "admin_password": "short"}
        resp = (await client.post("/api/setup/save", json=bad_form))
        assert resp.status_code == 400
        assert "details" in await resp.get_json()

    async def test_save_with_non_numeric_field_returns_json_400_not_html_500(self, setup_client):
        client, _ = setup_client
        bad_form = {
            **VALID_FORM,
            "partitions": [{"name": "Dom", "number": "niejestem_liczbą"}],
        }
        resp = (await client.post("/api/setup/save", json=bad_form))
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "details" in data
        assert "liczbow" in data["details"][0].lower()

    async def test_unexpected_error_in_api_route_returns_json_not_html(self, setup_client, monkeypatch):
        import dashboard.setup as setup_module

        client, _ = setup_client

        def _boom(form):
            raise RuntimeError("symulowany nieoczekiwany błąd")

        monkeypatch.setattr(setup_module, "build_config_dict", _boom)
        import dashboard.app as app_module
        monkeypatch.setattr(app_module, "build_config_dict", _boom)

        resp = (await client.post("/api/setup/save", json=VALID_FORM))
        assert resp.status_code == 500
        assert resp.content_type.startswith("application/json")
        assert "błąd serwera" in (await resp.get_json())["error"].lower()

    async def test_permission_error_in_api_route_returns_actionable_message(self, setup_client, monkeypatch):
        import dashboard.setup as setup_module

        client, _ = setup_client

        def _boom(path, data):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(setup_module, "save_draft", _boom)
        import dashboard.app as app_module
        monkeypatch.setattr(app_module, "save_draft", _boom)

        resp = (await client.post("/api/setup/draft", json={"step": 1}))
        assert resp.status_code == 500
        assert resp.content_type.startswith("application/json")
        error = (await resp.get_json())["error"].lower()
        assert "uprawnie" in error
        assert "chown" in error

    async def test_dashboard_usable_immediately_after_save(self, setup_client):
        client, _ = setup_client
        (await client.post("/api/setup/save", json=VALID_FORM))
        resp = (await client.get("/", follow_redirects=False))
        assert resp.status_code == 200

    async def test_second_save_attempt_rejected(self, setup_client):
        client, _ = setup_client
        (await client.post("/api/setup/save", json=VALID_FORM))
        resp = (await client.post("/api/setup/save", json=VALID_FORM))
        assert resp.status_code == 409

    async def test_setup_page_redirects_to_index_once_configured(self, setup_client):
        client, _ = setup_client
        (await client.post("/api/setup/save", json=VALID_FORM))
        resp = (await client.get("/setup", follow_redirects=False))
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/") or "index" in resp.headers["Location"] or resp.headers["Location"] == "/"

    async def test_saved_config_is_loadable_by_bridge_config(self, setup_client):
        from bridge.config import BridgeConfig

        client, tmp_path = setup_client
        (await client.post("/api/setup/save", json=VALID_FORM))
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.satel["host"] == "192.168.1.10"
        assert config.ebus["sensors"] == []


FORM_WITH_ADVANCED = {
    **VALID_FORM,
    "virtual_inputs": [{"name": "motion_hall_camera", "number": "17"}],
    "scenes": [
        {
            "name": "wykryto_intruza",
            "actions": [
                {"type": "tuya_on", "device": "gniazdko_salon", "dimmer_level_percent": ""},
                {"type": "satel_output", "output_number": "1", "state": True},
                {"type": "delay", "seconds": "2"},
                {"type": "satel_virtual_input", "virtual_input": "motion_hall_camera"},
            ],
        },
    ],
    "motion_rules": [
        {
            "zone_number": "12",
            "target_device": "gniazdko_salon",
            "duration_seconds": "60",
            "dimmer_level_percent": "",
            "time_window": {"enabled": True, "start": "sunset-15", "end": "23:30"},
            "escalation": {
                "enabled": True, "threshold_count": "3", "window_seconds": "120",
                "escalated_scene": "wykryto_intruza",
            },
        },
    ],
}


class TestValidationAdvanced:
    def test_advanced_form_has_no_errors(self):
        assert validate(FORM_WITH_ADVANCED) == []

    def test_scene_without_name_is_error(self):
        form = {**FORM_WITH_ADVANCED, "scenes": [{"name": "", "actions": [{"type": "tuya_on", "device": "x"}]}]}
        errors = validate(form)
        assert any("nazwa jest wymagana" in e for e in errors)

    def test_scene_without_actions_is_error(self):
        form = {**FORM_WITH_ADVANCED, "scenes": [{"name": "pusta", "actions": []}]}
        errors = validate(form)
        assert any("co najmniej jedną akcję" in e for e in errors)

    def test_scene_action_missing_required_field_is_error(self):
        form = {**FORM_WITH_ADVANCED, "scenes": [{"name": "s", "actions": [{"type": "tuya_on", "device": ""}]}]}
        errors = validate(form)
        assert any("device" in e for e in errors)

    def test_duplicate_scene_names_is_error(self):
        form = {**FORM_WITH_ADVANCED, "scenes": [
            {"name": "dup", "actions": [{"type": "satel_disarm"}]},
            {"name": "dup", "actions": [{"type": "satel_disarm"}]},
        ]}
        errors = validate(form)
        assert any("powtarza się" in e for e in errors)

    def test_motion_rule_without_zone_number_is_error(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [{**FORM_WITH_ADVANCED["motion_rules"][0], "zone_number": ""}]}
        errors = validate(form)
        assert any("numer wejścia" in e for e in errors)

    def test_motion_rule_needs_device_or_scene(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0]}
        rule.pop("target_device")
        rule["duration_seconds"] = ""
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("urządzenie docelowe" in e for e in errors)

    def test_motion_rule_scene_reference_must_exist(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {"zone_number": "12", "scene": "nieistniejaca"},
        ]}
        errors = validate(form)
        assert any("nie istnieje" in e for e in errors)

    def test_motion_rule_escalation_scene_reference_must_exist(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0]}
        rule["escalation"] = {**rule["escalation"], "escalated_scene": "nieistniejaca"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("nie istnieje" in e for e in errors)

    def test_motion_rule_escalation_missing_fields_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0]}
        rule["escalation"] = {"enabled": True, "threshold_count": "", "window_seconds": "", "escalated_scene": ""}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("liczba naruszeń" in e for e in errors)
        assert any("okno czasowe" in e for e in errors)
        assert any("wybierz scenę" in e for e in errors)

    def test_motion_rule_window_missing_end_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0]}
        rule["time_window"] = {"enabled": True, "start": "22:00", "end": ""}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("harmonogram" in e for e in errors)

    def test_motion_rule_turn_off_action_does_not_require_duration(self):
        rule = {"zone_number": "15", "trigger_on": "restored", "target_device": "swiatlo_korytarz", "action": "turn_off"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert not any("czas włączenia" in e for e in errors)

    def test_motion_rule_device_action_blank_duration_is_not_an_error(self):
        rule = {"zone_number": "16", "target_device": "swiatlo_korytarz", "duration_seconds": ""}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        assert validate(form) == []

    def test_motion_rule_unknown_trigger_on_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "trigger_on": "cos_innego"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wyzwalacz" in e for e in errors)

    def test_motion_rule_unknown_target_system_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "target_system": "cos_innego"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("system urządzenia docelowego" in e for e in errors)

    def test_motion_rule_satel_output_target_is_not_an_error(self):
        rule = {"zone_number": "22", "target_system": "satel_output", "target_device": "Brama"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        assert validate(form) == []

    def test_motion_rule_device_state_source_is_not_an_error(self):
        rule = {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": "5",
        }
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        assert validate(form) == []

    def test_motion_rule_homewhiz_state_source_is_not_an_error(self):
        rule = {
            "source": "homewhiz_state", "source_device": "Zmywarka", "trigger_value": "finished",
            "target_device": "gniazdko_salon", "duration_seconds": "5",
        }
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        assert validate(form) == []

    def test_motion_rule_device_state_source_requires_source_device(self):
        rule = {
            "source": "tuya_state", "source_device": "", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": "5",
        }
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("urządzenie źródłowe" in e for e in errors)

    def test_motion_rule_device_state_source_requires_trigger_value(self):
        rule = {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "",
            "target_device": "gniazdko_salon", "duration_seconds": "5",
        }
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wartość wyzwalającą" in e for e in errors)

    def test_motion_rule_device_state_source_does_not_require_zone_number(self):
        rule = {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": "5",
        }
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert not any("numer wejścia" in e for e in errors)

    def test_motion_rule_virtual_input_action_is_not_an_error(self):
        rule = {
            "source": "satel_zone", "zone_number": "5", "virtual_input": "motion_hall_camera",
        }
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        assert validate(form) == []

    def test_motion_rule_without_target_device_scene_or_virtual_input_is_error(self):
        rule = {"source": "satel_zone", "zone_number": "5"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wirtualne wejście" in e for e in errors)

    def test_motion_rule_unknown_source_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "source": "cos_innego"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("nieznane wejście" in e for e in errors)

    def test_incomplete_virtual_input_is_error(self):
        form = {**FORM_WITH_ADVANCED, "virtual_inputs": [{"name": "x", "number": "", "key": ""}]}
        errors = validate(form)
        assert any("Wirtualne wejście" in e for e in errors)


class TestConditionAndChainingValidation:

    def test_duplicate_rule_names_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "dup"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule, {**rule}]}
        errors = validate(form)
        assert any("nazwa 'dup' powtarza się" in e for e in errors)

    def test_scene_and_rule_sharing_a_name_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "wykryto_intruza"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wykryto_intruza" in e and "sceny i reguły ruchu" in e for e in errors)

    def test_then_trigger_rule_unknown_name_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "then_trigger_rule": "nie_istnieje"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("nieistniejącą nazwę reguły" in e for e in errors)

    def test_then_trigger_rule_self_reference_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "siebie", "then_trigger_rule": "siebie"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("samej siebie" in e for e in errors)

    def test_then_trigger_rule_valid_reference_is_ok(self):
        rule_a = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "a", "then_trigger_rule": "b"}
        rule_b = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "b"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule_a, rule_b]}
        assert validate(form) == []

    def test_state_condition_missing_key_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {"type": "state", "equals": "x"}}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wymaga wybrania statusu" in e for e in errors)

    def test_state_condition_missing_value_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {"type": "state", "key": "satel_input:3"}}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wartości porównania" in e for e in errors)

    def test_time_window_condition_missing_end_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {"type": "time_window", "start": "22:00", "end": ""}}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wymaga początku i końca" in e for e in errors)

    def test_composite_condition_without_children_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {"type": "and", "conditions": []}}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wymaga co najmniej jednego warunku podrzędnego" in e for e in errors)

    def test_unknown_condition_type_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {"type": "bogus"}}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("nieznany typ warunku" in e for e in errors)

    def test_valid_nested_condition_has_no_errors(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "satel_input:3", "equals": "naruszone"},
                {"type": "not", "conditions": [{"type": "time_window", "start": "22:00", "end": "06:00"}]},
            ],
        }}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        assert validate(form) == []


class TestPresenceSimulation:

    def test_disabled_presence_needs_no_fields(self):
        form = {"presence_simulation": {"enabled": False}}
        assert validate_automations(form) == []
        assert build_automations_dict(form)["presence_simulation"] == {"enabled": False}

    def test_enabled_presence_without_devices_is_error(self):
        form = {"presence_simulation": {"enabled": True, "devices": []}}
        errors = validate_automations(form)
        assert any("wybierz co najmniej jedno urządzenie" in e for e in errors)

    def test_enabled_presence_with_devices_builds_correctly(self):
        form = {"presence_simulation": {
            "enabled": True, "devices": ["gniazdko_salon", "swiatlo_korytarz"],
            "min_interval_minutes": "5", "max_interval_minutes": "15",
        }}
        assert validate_automations(form) == []
        built = build_automations_dict(form)["presence_simulation"]
        assert built == {
            "enabled": True, "devices": ["gniazdko_salon", "swiatlo_korytarz"],
            "min_interval_minutes": 5, "max_interval_minutes": 15,
        }

    def test_presence_window_included_when_enabled_and_complete(self):
        form = {"presence_simulation": {
            "enabled": True, "devices": ["x"],
            "window": {"enabled": True, "start": "sunset", "end": "23:30"},
        }}
        built = build_automations_dict(form)["presence_simulation"]
        assert built["window"] == {"start": "sunset", "end": "23:30"}

    def test_presence_window_omitted_when_disabled(self):
        form = {"presence_simulation": {
            "enabled": True, "devices": ["x"],
            "window": {"enabled": False, "start": "sunset", "end": "23:30"},
        }}
        built = build_automations_dict(form)["presence_simulation"]
        assert "window" not in built

    def test_incomplete_presence_window_is_error(self):
        form = {"presence_simulation": {
            "enabled": True, "devices": ["x"], "window": {"enabled": True, "start": "sunset", "end": ""},
        }}
        errors = validate_automations(form)
        assert any("harmonogram" in e for e in errors)

    def test_min_interval_greater_than_max_is_error(self):
        form = {"presence_simulation": {
            "enabled": True, "devices": ["x"], "min_interval_minutes": "60", "max_interval_minutes": "10",
        }}
        errors = validate_automations(form)
        assert any("minimalny odstęp nie może być większy" in e for e in errors)

    def test_default_intervals_when_not_provided(self):
        form = {"presence_simulation": {"enabled": True, "devices": ["x"]}}
        built = build_automations_dict(form)["presence_simulation"]
        assert built["min_interval_minutes"] == 10
        assert built["max_interval_minutes"] == 40


class TestBuildConfigDictAdvanced:
    def test_scenes_built_with_typed_fields(self):
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        scenes = automations["scenes"]
        assert len(scenes) == 1
        assert scenes[0]["name"] == "wykryto_intruza"
        actions = scenes[0]["actions"]
        assert actions[0] == {"type": "tuya_on", "device": "gniazdko_salon"}
        assert actions[1] == {"type": "satel_output", "output_number": 1, "state": True}
        assert actions[2] == {"type": "delay", "seconds": 2.0}
        assert actions[3] == {"type": "satel_virtual_input", "virtual_input": "motion_hall_camera"}

    def test_scene_enabled_default_is_omitted(self):
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        assert "enabled" not in automations["scenes"][0]

    def test_scene_disabled_is_built(self):
        form = {**VALID_FORM, "scenes": [
            {"name": "scena_x", "enabled": False, "actions": [{"type": "tuya_on", "device": "x"}]},
        ]}
        scene = build_automations_dict(form)["scenes"][0]
        assert scene["enabled"] is False

    def test_motion_rule_enabled_default_is_omitted(self):
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        assert "enabled" not in automations["reguły_ruchu"][0]

    def test_motion_rule_disabled_is_built(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "enabled": False}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built_rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert built_rule["enabled"] is False

    def test_motion_rule_imported_to_flows_flag_is_preserved(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "enabled": False, "imported_to_flows": True}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built_rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert built_rule["imported_to_flows"] is True

    def test_motion_rule_imported_to_flows_omitted_when_falsy(self):
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        assert "imported_to_flows" not in automations["reguły_ruchu"][0]

    def test_arm_disarm_actions_capture_explicit_partitions(self):
        form = {**VALID_FORM, "scenes": [
            {
                "name": "wyjscie_z_domu",
                "actions": [
                    {"type": "satel_arm", "mode": "0", "partitions": ["1", "2"]},
                    {"type": "satel_disarm", "partitions": ["2"]},
                ],
            },
        ]}
        actions = build_automations_dict(form)["scenes"][0]["actions"]
        assert actions[0] == {"type": "satel_arm", "mode": 0, "partitions": [1, 2]}
        assert actions[1] == {"type": "satel_disarm", "partitions": [2]}

    def test_arm_disarm_actions_omit_partitions_when_not_given(self):
        form = {**VALID_FORM, "scenes": [
            {"name": "s", "actions": [{"type": "satel_arm"}, {"type": "satel_disarm"}]},
        ]}
        actions = build_automations_dict(form)["scenes"][0]["actions"]
        assert actions[0] == {"type": "satel_arm"}
        assert actions[1] == {"type": "satel_disarm"}

    def test_motion_rules_built_with_harmonogram_and_escalation(self):
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        rules = automations["reguły_ruchu"]
        assert len(rules) == 1
        rule = rules[0]
        assert rule["source"] == "satel_zone"
        assert rule["zone_number"] == 12
        assert rule["target_device"] == "gniazdko_salon"
        assert rule["duration_seconds"] == 60
        assert rule["time_window"] == {"start": "sunset-15", "end": "23:30"}
        assert rule["escalation"] == {
            "threshold_count": 3, "window_seconds": 120, "escalated_scene": "wykryto_intruza",
        }

    def test_motion_rule_with_scene_instead_of_device(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {"zone_number": "14", "scene": "wykryto_intruza", "time_window": {}, "escalation": {}},
        ]}
        automations = build_automations_dict(form)
        rule = automations["reguły_ruchu"][0]
        assert rule["scene"] == "wykryto_intruza"
        assert "target_device" not in rule
        assert "time_window" not in rule
        assert "escalation" not in rule

    def test_motion_rule_turn_off_action_omits_duration_and_dimmer(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "zone_number": "15", "trigger_on": "restored", "target_device": "swiatlo_korytarz",
                "action": "turn_off", "time_window": {}, "escalation": {},
            },
        ]}
        automations = build_automations_dict(form)
        rule = automations["reguły_ruchu"][0]
        assert rule["target_device"] == "swiatlo_korytarz"
        assert rule["action"] == "turn_off"
        assert rule["trigger_on"] == "restored"
        assert "duration_seconds" not in rule
        assert "dimmer_level_percent" not in rule

    def test_motion_rule_blank_duration_omits_duration_seconds(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "zone_number": "16", "target_device": "swiatlo_korytarz",
                "duration_seconds": "", "time_window": {}, "escalation": {},
            },
        ]}
        automations = build_automations_dict(form)
        rule = automations["reguły_ruchu"][0]
        assert rule["target_device"] == "swiatlo_korytarz"
        assert "duration_seconds" not in rule
        assert "action" not in rule

    def test_motion_rule_default_trigger_on_is_omitted(self):
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        assert "trigger_on" not in automations["reguły_ruchu"][0]

    def test_motion_rule_default_target_system_is_omitted(self):
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        assert "target_system" not in automations["reguły_ruchu"][0]

    def test_motion_rule_satel_output_target_is_built(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "zone_number": "22", "target_system": "satel_output", "target_device": "Brama",
                "duration_seconds": "10", "dimmer_level_percent": "50",
                "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["target_system"] == "satel_output"
        assert rule["target_device"] == "Brama"
        assert rule["duration_seconds"] == 10
        assert "dimmer_level_percent" not in rule

    def test_motion_rule_satel_output_turn_off_is_built(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "zone_number": "22", "target_system": "satel_output", "target_device": "Brama",
                "action": "turn_off", "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["target_system"] == "satel_output"
        assert rule["action"] == "turn_off"
        assert "duration_seconds" not in rule
        assert "dimmer_level_percent" not in rule

    def test_motion_rule_duration_zero_is_kept_explicit(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "zone_number": "16", "target_device": "swiatlo_korytarz",
                "duration_seconds": "0", "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["duration_seconds"] == 0

    def test_motion_rule_tuya_state_source_is_built(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
                "target_device": "gniazdko_salon", "duration_seconds": "5",
                "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["source"] == "tuya_state"
        assert rule["source_device"] == "czujnik_drzwi"
        assert rule["trigger_value"] == "włączone"
        assert "zone_number" not in rule
        assert "trigger_on" not in rule

    def test_motion_rule_homewhiz_state_source_is_built(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "source": "homewhiz_state", "source_device": "Zmywarka", "trigger_value": "finished",
                "target_device": "gniazdko_salon", "duration_seconds": "5",
                "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["source"] == "homewhiz_state"
        assert rule["source_device"] == "Zmywarka"
        assert rule["trigger_value"] == "finished"

    def test_motion_rule_virtual_input_action_is_built(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "source": "satel_zone", "zone_number": "5", "virtual_input": "motion_hall_camera",
                "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["virtual_input"] == "motion_hall_camera"
        assert "target_device" not in rule
        assert "target_system" not in rule
        assert "duration_seconds" not in rule
        assert "dimmer_level_percent" not in rule

    def test_motion_rule_virtual_input_action_with_explicit_duration_is_built(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "source": "satel_zone", "zone_number": "5", "virtual_input": "motion_hall_camera",
                "duration_seconds": "10", "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["duration_seconds"] == 10

    def test_motion_rule_virtual_input_action_duration_zero_is_kept_explicit(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "source": "satel_zone", "zone_number": "5", "virtual_input": "motion_hall_camera",
                "duration_seconds": "0", "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["duration_seconds"] == 0

    def test_motion_rule_scene_takes_priority_over_virtual_input(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {
                "source": "satel_zone", "zone_number": "5",
                "scene": "wykryto_intruza", "virtual_input": "motion_hall_camera",
                "time_window": {}, "escalation": {},
            },
        ]}
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["scene"] == "wykryto_intruza"
        assert "virtual_input" not in rule

    def test_motion_rule_device_state_source_without_device_or_value_is_skipped(self):
        form = {**FORM_WITH_ADVANCED, "motion_rules": [
            {"source": "tuya_state", "source_device": "", "trigger_value": "włączone"},
        ]}
        assert build_automations_dict(form)["reguły_ruchu"] == []

    def test_virtual_inputs_built_into_satel_fields(self):
        satel_fields = build_satel_fields_dict(FORM_WITH_ADVANCED)
        assert satel_fields["virtual_inputs"] == {"motion_hall_camera": 17}

    def test_disabled_time_window_and_escalation_are_omitted(self):
        rule = {
            "zone_number": "13", "target_device": "gniazdko_salon", "duration_seconds": "30",
            "time_window": {"enabled": False, "start": "22:00", "end": "06:00"},
            "escalation": {"enabled": False, "threshold_count": "3", "window_seconds": "120", "escalated_scene": "x"},
        }
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        automations = build_automations_dict(form)
        built = automations["reguły_ruchu"][0]
        assert "time_window" not in built

    def test_name_and_then_trigger_rule_are_built(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "wejscie_salon", "then_trigger_rule": "korytarz"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built = build_automations_dict(form)["reguły_ruchu"][0]
        assert built["name"] == "wejscie_salon"
        assert built["then_trigger_rule"] == "korytarz"

    def test_blank_name_and_then_trigger_rule_are_omitted(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "  ", "then_trigger_rule": ""}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built = build_automations_dict(form)["reguły_ruchu"][0]
        assert "name" not in built
        assert "then_trigger_rule" not in built

    def test_nested_condition_is_built_and_normalized(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "satel_input:3", "equals": "naruszone"},
                {"type": "not", "conditions": [{"type": "time_window", "start": " 22:00 ", "end": " 06:00 "}]},
            ],
        }}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built = build_automations_dict(form)["reguły_ruchu"][0]
        assert built["condition"] == {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "satel_input:3", "equals": "naruszone"},
                {"type": "not", "conditions": [{"type": "time_window", "start": "22:00", "end": "06:00"}]},
            ],
        }

    def test_condition_with_not_equals_is_built(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {
            "type": "state", "key": "satel_output:5", "not_equals": "włączone",
        }}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built = build_automations_dict(form)["reguły_ruchu"][0]
        assert built["condition"] == {"type": "state", "key": "satel_output:5", "not_equals": "włączone"}

    def test_empty_condition_is_omitted(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "condition": {"type": "and", "conditions": []}}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built = build_automations_dict(form)["reguły_ruchu"][0]
        assert "condition" not in built

    def test_no_condition_key_is_omitted(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0]}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        built = build_automations_dict(form)["reguły_ruchu"][0]
        assert "condition" not in built

    def test_advanced_result_is_yaml_serializable(self):
        config = build_config_dict(FORM_WITH_ADVANCED)
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        dumped = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        dumped_automations = yaml.safe_dump(automations, allow_unicode=True, sort_keys=False)
        reloaded = yaml.safe_load(dumped)
        reloaded_automations = yaml.safe_load(dumped_automations)
        assert reloaded["satel"]["host"] == "192.168.1.10"
        assert reloaded_automations["scenes"][0]["name"] == "wykryto_intruza"

    def test_saved_advanced_config_is_loadable_and_runs_scene_runner(self):
        from bridge.scenes import SceneRunner

        automations = build_automations_dict(FORM_WITH_ADVANCED)
        dumped = yaml.safe_dump(automations, allow_unicode=True, sort_keys=False)
        reloaded = yaml.safe_load(dumped)
        runner = SceneRunner(reloaded["scenes"], tuya=None, satel=None, satel_virtual_inputs=None)
        assert runner.exists("wykryto_intruza")


class TestMultiSourceAndMultiActionMotionRules:
    def _form_with_rule(self, rule: dict) -> dict:
        return {**FORM_WITH_ADVANCED, "motion_rules": [{**rule, "time_window": {}, "escalation": {}}]}

    def test_multi_source_or_rule_has_no_errors(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": "12"},
                {"source": "satel_zone", "zone_number": "13"},
            ],
            "target_device": "gniazdko_salon", "duration_seconds": "60",
        })
        assert validate_automations(form) == []

    def test_multi_source_and_rule_has_no_errors(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": "12"},
                {"source": "satel_zone", "zone_number": "13"},
            ],
            "source_combinator": "and",
            "target_device": "gniazdko_salon",
        })
        assert validate_automations(form) == []

    def test_single_entry_sources_list_is_an_error(self):
        form = self._form_with_rule({
            "sources": [{"source": "satel_zone", "zone_number": "12"}],
            "target_device": "gniazdko_salon", "duration_seconds": "60",
        })
        errors = validate_automations(form)
        assert any("co najmniej 2" in e for e in errors)

    def test_unknown_combinator_is_an_error(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": "12"},
                {"source": "satel_zone", "zone_number": "13"},
            ],
            "source_combinator": "xor",
            "target_device": "gniazdko_salon", "duration_seconds": "60",
        })
        errors = validate_automations(form)
        assert any("nieznany łącznik źródeł" in e for e in errors)

    def test_multi_source_invalid_entry_is_reported(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": ""},
                {"source": "satel_zone", "zone_number": "13"},
            ],
            "target_device": "gniazdko_salon", "duration_seconds": "60",
        })
        errors = validate_automations(form)
        assert any("źródło #1" in e for e in errors)

    def test_multi_action_rule_has_no_errors(self):
        form = self._form_with_rule({
            "zone_number": "12",
            "actions": [
                {"target_device": "gniazdko_salon"},
                {"target_device": "wiatrak"},
            ],
            "duration_seconds": "60",
        })
        assert validate_automations(form) == []

    def test_multi_action_invalid_entry_is_reported(self):
        form = self._form_with_rule({
            "zone_number": "12",
            "actions": [
                {"target_device": ""},
                {"target_device": "wiatrak"},
            ],
            "duration_seconds": "60",
        })
        errors = validate_automations(form)
        assert any("akcja #1" in e for e in errors)

    def test_multi_source_rule_is_built_with_sources_list(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": "12"},
                {"source": "satel_zone", "zone_number": "13"},
            ],
            "target_device": "gniazdko_salon", "duration_seconds": "60",
        })
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["sources"] == [
            {"source": "satel_zone", "zone_number": 12},
            {"source": "satel_zone", "zone_number": 13},
        ]
        assert "source_combinator" not in rule
        assert "source" not in rule
        assert "zone_number" not in rule
        assert rule["target_device"] == "gniazdko_salon"
        assert rule["duration_seconds"] == 60

    def test_multi_source_and_combinator_is_built(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": "12"},
                {"source": "satel_zone", "zone_number": "13"},
            ],
            "source_combinator": "and",
            "target_device": "gniazdko_salon",
        })
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["source_combinator"] == "and"

    def test_sources_list_with_fewer_than_two_valid_entries_skips_rule(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": "12"},
                {"source": "satel_zone", "zone_number": ""},
            ],
            "target_device": "gniazdko_salon", "duration_seconds": "60",
        })
        assert build_automations_dict(form)["reguły_ruchu"] == []

    def test_mixed_source_types_are_built(self):
        form = self._form_with_rule({
            "sources": [
                {"source": "satel_zone", "zone_number": "12"},
                {"source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone"},
            ],
            "target_device": "gniazdko_salon", "duration_seconds": "60",
        })
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["sources"] == [
            {"source": "satel_zone", "zone_number": 12},
            {"source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone"},
        ]

    def test_multi_action_rule_is_built_with_actions_list(self):
        form = self._form_with_rule({
            "zone_number": "12",
            "actions": [
                {"target_device": "gniazdko_salon", "dimmer_level_percent": "70"},
                {"target_device": "wiatrak"},
            ],
            "duration_seconds": "60",
        })
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["actions"] == [
            {"target_device": "gniazdko_salon", "dimmer_level_percent": 70},
            {"target_device": "wiatrak"},
        ]
        assert "target_device" not in rule
        assert rule["duration_seconds"] == 60

    def test_multi_action_turn_off_is_built(self):
        form = self._form_with_rule({
            "zone_number": "12", "trigger_on": "restored", "action": "turn_off",
            "actions": [
                {"target_device": "gniazdko_salon"},
                {"target_device": "wiatrak"},
            ],
        })
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["action"] == "turn_off"
        assert "duration_seconds" not in rule

    def test_multi_action_satel_output_entry_omits_dimmer(self):
        form = self._form_with_rule({
            "zone_number": "12",
            "actions": [
                {"target_device": "Brama", "target_system": "satel_output"},
                {"target_device": "wiatrak"},
            ],
            "duration_seconds": "60",
        })
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["actions"][0] == {"target_device": "Brama", "target_system": "satel_output"}

    def test_multi_action_invalid_entry_is_skipped(self):
        form = self._form_with_rule({
            "zone_number": "12",
            "actions": [
                {"target_device": ""},
                {"target_device": "wiatrak"},
            ],
            "duration_seconds": "60",
        })
        rule = build_automations_dict(form)["reguły_ruchu"][0]
        assert rule["actions"] == [{"target_device": "wiatrak"}]

    def test_all_actions_invalid_skips_rule(self):
        form = self._form_with_rule({
            "zone_number": "12",
            "actions": [{"target_device": ""}],
            "duration_seconds": "60",
        })
        assert build_automations_dict(form)["reguły_ruchu"] == []


class TestFlows:
    def _form_with_flow(self, flow: dict) -> dict:
        return {**VALID_FORM, "flows": [flow]}

    def _simple_flow(self, **overrides) -> dict:
        flow = {
            "name": "test-flow",
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "gniazdko_salon"}, "wires": []},
            ],
        }
        flow.update(overrides)
        return flow

    def test_valid_flow_has_no_errors(self):
        assert validate_automations(self._form_with_flow(self._simple_flow())) == []

    def test_flow_without_name_is_error(self):
        errors = validate_automations(self._form_with_flow(self._simple_flow(name="")))
        assert any("nazwa jest wymagana" in e for e in errors)

    def test_flow_without_nodes_is_error(self):
        errors = validate_automations(self._form_with_flow(self._simple_flow(nodes=[])))
        assert any("co najmniej jeden węzeł" in e for e in errors)

    def test_flow_without_entry_node_is_error(self):
        flow = self._simple_flow(nodes=[
            {"id": "dev", "type": "device", "config": {"target_device": "gniazdko_salon"}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("węzła wejściowego" in e for e in errors)

    def test_duplicate_node_ids_is_error(self):
        flow = self._simple_flow(nodes=[
            {"id": "a", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["a"]]},
            {"id": "a", "type": "device", "config": {"target_device": "x"}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("unikalne" in e for e in errors)

    def test_unknown_node_type_is_error(self):
        flow = self._simple_flow(nodes=[{"id": "a", "type": "not_a_type", "config": {}, "wires": []}])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("nieznany typ węzła" in e for e in errors)

    def test_trigger_satel_zone_requires_zone_number(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone"}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("numer wejścia Satela" in e for e in errors)

    def test_trigger_device_state_requires_source_device(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "tuya_state"}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("wybierz urządzenie źródłowe" in e for e in errors)

    def test_condition_requires_op_and_value(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["c"]]},
            {"id": "c", "type": "condition", "config": {}, "wires": [[], []]},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("nieznany operator" in e for e in errors)
        assert any("wartości porównania" in e for e in errors)

    def test_device_requires_target_device(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["d"]]},
            {"id": "d", "type": "device", "config": {}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("wybierz urządzenie docelowe" in e for e in errors)

    def test_device_accepts_satel_virtual_input_target_system(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["d"]]},
            {"id": "d", "type": "device", "config": {"target_device": "kamera", "target_system": "satel_virtual_input"}, "wires": []},
        ])
        assert validate_automations(self._form_with_flow(flow)) == []

    def test_device_rejects_unknown_target_system(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["d"]]},
            {"id": "d", "type": "device", "config": {"target_device": "x", "target_system": "nope"}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("nieznany system urządzenia docelowego" in e for e in errors)

    def test_device_accepts_toggle_value(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["d"]]},
            {"id": "d", "type": "device", "config": {"target_device": "x", "value": "toggle"}, "wires": []},
        ])
        assert validate_automations(self._form_with_flow(flow)) == []

    def test_device_rejects_unknown_value(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["d"]]},
            {"id": "d", "type": "device", "config": {"target_device": "x", "value": "blink"}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("nieznana wartość akcji" in e for e in errors)

    def test_scene_node_must_reference_existing_scene(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
            {"id": "s", "type": "scene", "config": {"scene_name": "brak_takiej"}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("nie istnieje wśród zdefiniowanych scen" in e for e in errors)

    def test_scene_node_with_existing_scene_is_valid(self):
        form = {
            **VALID_FORM,
            "scenes": [{"name": "moja_scena", "actions": [{"type": "tuya_on", "device": "x"}]}],
            "flows": [self._simple_flow(nodes=[
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
                {"id": "s", "type": "scene", "config": {"scene_name": "moja_scena"}, "wires": []},
            ])],
        }
        assert validate_automations(form) == []

    def test_mqtt_node_requires_topic(self):
        flow = self._simple_flow(nodes=[{"id": "m", "type": "mqttIn", "config": {}, "wires": []}])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("temat MQTT" in e for e in errors)

    def test_http_node_requires_url(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["h"]]},
            {"id": "h", "type": "http", "config": {}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("podaj adres URL" in e for e in errors)

    def test_schedule_node_requires_start_and_end(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
            {"id": "s", "type": "schedule", "config": {}, "wires": []},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("harmonogram wymaga początku i końca" in e for e in errors)

    def test_schedule_node_with_start_and_end_is_valid(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
            {"id": "s", "type": "schedule", "config": {"start": "22:00", "end": "sunrise"}, "wires": []},
        ])
        assert validate_automations(self._form_with_flow(flow)) == []

    def test_escalation_node_requires_threshold_and_window(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["e"]]},
            {"id": "e", "type": "escalation", "config": {}, "wires": [[], []]},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("progu (liczby naruszeń)" in e for e in errors)
        assert any("okna czasowego (sekundy)" in e for e in errors)

    def test_escalation_node_with_values_is_valid(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["e"]]},
            {"id": "e", "type": "escalation", "config": {"threshold_count": 3, "window_seconds": 120}, "wires": [[], []]},
        ])
        assert validate_automations(self._form_with_flow(flow)) == []

    def test_build_schedule_node(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
            {"id": "s", "type": "schedule", "config": {"start": "22:00", "end": "06:00"}, "wires": []},
        ])
        automations = build_automations_dict(self._form_with_flow(flow))
        assert automations["flows"][0]["nodes"][1]["config"] == {"start": "22:00", "end": "06:00"}

    def test_build_escalation_node_coerces_numbers(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["e"]]},
            {"id": "e", "type": "escalation", "config": {"threshold_count": "3", "window_seconds": "120"}, "wires": [[], []]},
        ])
        automations = build_automations_dict(self._form_with_flow(flow))
        assert automations["flows"][0]["nodes"][1]["config"] == {"threshold_count": 3, "window_seconds": 120.0}

    def test_state_node_requires_key_op_value(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
            {"id": "s", "type": "state", "config": {}, "wires": [[], []]},
        ])
        errors = validate_automations(self._form_with_flow(flow))
        assert any("wybrania statusu" in e for e in errors)
        assert any("nieznany operator warunku stanu" in e for e in errors)
        assert any("wartości porównania" in e for e in errors)

    def test_state_node_with_values_is_valid(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
            {"id": "s", "type": "state", "config": {"key": "satel_input:9", "op": "equals", "value": "naruszone"}, "wires": [[], []]},
        ])
        assert validate_automations(self._form_with_flow(flow)) == []

    def test_build_state_node(self):
        flow = self._simple_flow(nodes=[
            {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": "3"}, "wires": [["s"]]},
            {"id": "s", "type": "state", "config": {"key": "tuya:gniazdko", "op": "not_equals", "value": "włączone"}, "wires": [[], []]},
        ])
        automations = build_automations_dict(self._form_with_flow(flow))
        assert automations["flows"][0]["nodes"][1]["config"] == {
            "key": "tuya:gniazdko", "op": "not_equals", "value": "włączone",
        }

    def test_build_flow_produces_expected_shape(self):
        automations = build_automations_dict(self._form_with_flow(self._simple_flow()))
        flows = automations["flows"]
        assert len(flows) == 1
        flow = flows[0]
        assert flow["name"] == "test-flow"
        assert flow["nodes"][0] == {
            "id": "trig", "type": "trigger",
            "config": {"source": "satel_zone", "op": "violated", "zone_number": 3},
            "wires": [["dev"]],
        }
        assert flow["nodes"][1] == {
            "id": "dev", "type": "device",
            "config": {"target_device": "gniazdko_salon", "target_system": "tuya", "value": "on"},
            "wires": [],
        }

    def test_build_flow_without_name_is_skipped(self):
        automations = build_automations_dict(self._form_with_flow(self._simple_flow(name="")))
        assert automations["flows"] == []

    def test_build_flow_disabled_flag_is_kept(self):
        automations = build_automations_dict(self._form_with_flow(self._simple_flow(enabled=False)))
        assert automations["flows"][0]["enabled"] is False

    def test_build_flow_enabled_default_is_omitted(self):
        automations = build_automations_dict(self._form_with_flow(self._simple_flow()))
        assert "enabled" not in automations["flows"][0]

    def test_build_flow_preserves_positions(self):
        flow = self._simple_flow()
        flow["nodes"][0]["x"] = 100
        flow["nodes"][0]["y"] = 50
        automations = build_automations_dict(self._form_with_flow(flow))
        assert automations["flows"][0]["nodes"][0]["x"] == 100
        assert automations["flows"][0]["nodes"][0]["y"] == 50


class TestMotionRuleToFlow:
    def _nodes_by_type(self, flow):
        by_type = {}
        for node in flow["nodes"]:
            by_type.setdefault(node["type"], []).append(node)
        return by_type

    def test_simple_fixed_duration_rule(self):
        rule = {
            "name": "swiatlo_na_taras", "source": "satel_zone", "zone_number": 5,
            "target_device": "lampa_taras", "duration_seconds": 60,
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        assert flow["name"] == "Import: swiatlo_na_taras"
        by_type = self._nodes_by_type(flow)
        assert len(by_type["trigger"]) == 1
        assert by_type["trigger"][0]["config"] == {"source": "satel_zone", "zone_number": 5, "op": "violated"}
        # "włącz na czas" reguły ruchu importuje się jako włącz->opóźnienie->wyłącz,
        # tak jak faktycznie działa RulesEngine._run_on_for_duration - inaczej zaimportowany
        # flow zostawiałby urządzenie włączone na zawsze (bez automatycznego wyłączenia).
        devices = by_type["device"]
        assert len(devices) == 2
        on_device = next(d for d in devices if d["config"]["value"] == "on")
        off_device = next(d for d in devices if d["config"]["value"] == "off")
        assert on_device["config"]["target_device"] == "lampa_taras"
        assert off_device["config"]["target_device"] == "lampa_taras"
        delay = by_type["delay"][0]
        assert delay["config"] == {"seconds": 60}
        # trigger -> device(on) -> delay -> device(off)
        assert flow["nodes"][0]["wires"] == [[on_device["id"]]]
        assert on_device["wires"] == [[delay["id"]]]
        assert delay["wires"] == [[off_device["id"]]]

    def test_until_restored_mode_creates_two_pipelines(self):
        rule = {"name": "przedpokoj", "source": "satel_zone", "zone_number": 5, "target_device": "swiatlo"}
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        by_type = self._nodes_by_type(flow)
        ops = sorted(n["config"]["op"] for n in by_type["trigger"])
        assert ops == ["restored", "violated"]
        values = sorted(n["config"]["value"] for n in by_type["device"])
        assert values == ["off", "on"]

    def test_action_turn_off_with_trigger_on_restored(self):
        rule = {
            "name": "gasniecie", "zone_number": 12, "trigger_on": "restored",
            "target_device": "swiatlo_korytarz", "action": "turn_off",
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        by_type = self._nodes_by_type(flow)
        assert len(by_type["trigger"]) == 1
        assert by_type["trigger"][0]["config"]["op"] == "restored"
        assert by_type["device"][0]["config"]["value"] == "off"

    def test_scene_rule(self):
        rule = {"name": "alarm_wlaczony", "source": "satel_zone", "zone_number": 3, "scene": "powiadomienie"}
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        by_type = self._nodes_by_type(flow)
        assert "device" not in by_type
        assert by_type["scene"][0]["config"] == {"scene_name": "powiadomienie"}

    def test_time_window_becomes_schedule_node(self):
        rule = {
            "name": "wieczorem", "source": "satel_zone", "zone_number": 3, "target_device": "lampa",
            "duration_seconds": 60, "time_window": {"start": "22:00", "end": "sunrise"},
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        by_type = self._nodes_by_type(flow)
        assert by_type["schedule"][0]["config"] == {"start": "22:00", "end": "sunrise"}
        trig = by_type["trigger"][0]
        sched = by_type["schedule"][0]
        assert trig["wires"] == [[sched["id"]]]

    def test_escalation_becomes_escalation_node_with_scene_branch(self):
        rule = {
            "name": "ruch_powtarzajacy", "source": "satel_zone", "zone_number": 7, "target_device": "lampa",
            "duration_seconds": 60,
            "escalation": {"threshold_count": 3, "window_seconds": 120, "escalated_scene": "wykryto_intruza"},
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        by_type = self._nodes_by_type(flow)
        esc = by_type["escalation"][0]
        assert esc["config"] == {"threshold_count": 3, "window_seconds": 120}
        scenes = [n for n in by_type["scene"] if n["config"]["scene_name"] == "wykryto_intruza"]
        assert len(scenes) == 1
        assert esc["wires"][1] == [scenes[0]["id"]]
        devices = by_type["device"]
        assert len(devices) == 2
        on_device = next(d for d in devices if d["config"]["value"] == "on")
        assert esc["wires"][0] == [on_device["id"]]

    def test_escalation_skipped_with_warning_for_until_restored_mode(self):
        rule = {
            "name": "do_ustania", "source": "satel_zone", "zone_number": 7, "target_device": "lampa",
            "escalation": {"threshold_count": 3, "window_seconds": 120, "escalated_scene": "wykryto_intruza"},
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert any("eskalacja pominięta" in w for w in warnings)
        by_type = self._nodes_by_type(flow)
        assert "escalation" not in by_type

    def test_unnamed_rule_gets_positional_name(self):
        rule = {"source": "satel_zone", "zone_number": 1, "target_device": "x", "duration_seconds": 5}
        flow, warnings = motion_rule_to_flow(rule, 2)
        assert flow["name"] == "Import: Reguła ruchu #3"

    def test_multi_source_rule_is_skipped_entirely(self):
        rule = {"name": "wielo", "sources": [{"zone_number": 1}, {"zone_number": 2}], "target_device": "x"}
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert flow is None
        assert any("wielo" in w and "sources/actions" in w for w in warnings)

    def test_virtual_input_rule_converts_to_device_node(self):
        rule = {
            "name": "wejscie_ip", "source": "satel_zone", "zone_number": 1,
            "virtual_input": "kamera", "duration_seconds": 30,
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        by_type = self._nodes_by_type(flow)
        assert by_type["device"][0]["config"] == {
            "target_device": "kamera", "target_system": "satel_virtual_input", "value": "on",
        }

    def test_virtual_input_rule_until_restored_creates_two_device_nodes(self):
        rule = {"name": "wejscie_ip", "source": "satel_zone", "zone_number": 1, "virtual_input": "kamera"}
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert warnings == []
        by_type = self._nodes_by_type(flow)
        values = sorted(n["config"]["value"] for n in by_type["device"])
        assert values == ["off", "on"]
        assert all(n["config"]["target_system"] == "satel_virtual_input" for n in by_type["device"])

    def test_condition_produces_warning_but_still_converts(self):
        rule = {
            "name": "z_warunkiem", "source": "satel_zone", "zone_number": 1, "target_device": "x",
            "duration_seconds": 5, "condition": {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert flow is not None
        assert any("warunek" in w and "NIE został przeniesiony" in w for w in warnings)

    def test_then_trigger_rule_produces_warning_but_still_converts(self):
        rule = {
            "name": "lancuch", "source": "satel_zone", "zone_number": 1, "target_device": "x",
            "duration_seconds": 5, "then_trigger_rule": "inna_regula",
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert flow is not None
        assert any("then_trigger_rule" in w for w in warnings)

    def test_dimmer_level_percent_is_carried_over(self):
        rule = {
            "name": "sciemniacz", "source": "satel_zone", "zone_number": 1, "target_device": "lampa",
            "duration_seconds": 5, "dimmer_level_percent": 40,
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        by_type = self._nodes_by_type(flow)
        assert by_type["device"][0]["config"]["dimmer_level_percent"] == 40

    def test_flow_ids_are_unique_across_calls(self):
        rule = {"name": "x", "source": "satel_zone", "zone_number": 1, "target_device": "x", "duration_seconds": 5}
        flow_a, _ = motion_rule_to_flow(rule, 0)
        flow_b, _ = motion_rule_to_flow(rule, 0)
        assert flow_a["id"] != flow_b["id"]

    def test_already_imported_rule_is_skipped_with_warning(self):
        rule = {
            "name": "juz_zaimportowana", "source": "satel_zone", "zone_number": 1, "target_device": "x",
            "duration_seconds": 5, "imported_to_flows": True,
        }
        flow, warnings = motion_rule_to_flow(rule, 0)
        assert flow is None
        assert any("juz_zaimportowana" in w and "już zaimportowano" in w for w in warnings)


class TestSetupDraft:
    async def test_no_draft_returns_empty_object(self, setup_client):
        client, _ = setup_client
        resp = (await client.get("/api/setup/draft"))
        assert resp.status_code == 200
        assert await resp.get_json() == {}

    async def test_saving_and_loading_draft_roundtrips(self, setup_client):
        client, _ = setup_client
        payload = {"step": 3, "data": {"satel": {"host": "192.168.1.10"}}}
        resp = (await client.post("/api/setup/draft", json=payload))
        assert resp.status_code == 200

        resp = (await client.get("/api/setup/draft"))
        assert await resp.get_json() == payload

    async def test_draft_persists_to_disk_next_to_config_path(self, setup_client):
        client, tmp_path = setup_client
        (await client.post("/api/setup/draft", json={"step": 2, "data": {"a": 1}}))
        assert (tmp_path / ".setup_draft.json").exists()

    async def test_draft_overwritten_by_later_save(self, setup_client):
        client, _ = setup_client
        (await client.post("/api/setup/draft", json={"step": 2, "data": {"a": 1}}))
        (await client.post("/api/setup/draft", json={"step": 5, "data": {"a": 2}}))
        resp = (await client.get("/api/setup/draft"))
        assert await resp.get_json() == {"step": 5, "data": {"a": 2}}

    async def test_draft_rejected_when_already_configured(self, setup_client):
        client, _ = setup_client
        (await client.post("/api/setup/save", json=VALID_FORM))
        resp = (await client.post("/api/setup/draft", json={"step": 1, "data": {}}))
        assert resp.status_code == 409
        resp = (await client.get("/api/setup/draft"))
        assert resp.status_code == 409

    async def test_final_save_deletes_draft(self, setup_client):
        client, tmp_path = setup_client
        (await client.post("/api/setup/draft", json={"step": 4, "data": {"a": 1}}))
        assert (tmp_path / ".setup_draft.json").exists()
        (await client.post("/api/setup/save", json=VALID_FORM))
        assert not (tmp_path / ".setup_draft.json").exists()

    async def test_invalid_draft_body_returns_400(self, setup_client):
        client, _ = setup_client
        resp = (await client.post(
            "/api/setup/draft", data="not json", headers={"Content-Type": "application/json"},
        ))
        assert resp.status_code == 400


class TestDraftHelpers:
    def test_load_draft_missing_file_returns_empty(self, tmp_path):
        assert load_draft(tmp_path / "missing.json") == {}

    def test_load_draft_corrupted_file_returns_empty(self, tmp_path):
        path = tmp_path / ".setup_draft.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert load_draft(path) == {}

    def test_save_then_load_roundtrips(self, tmp_path):
        path = tmp_path / ".setup_draft.json"
        save_draft(path, {"step": 2, "data": {"x": 1}})
        assert load_draft(path) == {"step": 2, "data": {"x": 1}}

    def test_delete_draft_is_idempotent(self, tmp_path):
        path = tmp_path / ".setup_draft.json"
        delete_draft(path)
        save_draft(path, {"a": 1})
        delete_draft(path)
        assert not path.exists()

    def test_draft_path_for_is_next_to_config(self, tmp_path):
        config_path = tmp_path / "sub" / "config.yaml"
        assert draft_path_for(config_path) == tmp_path / "sub" / ".setup_draft.json"


class TestListPlugins:

    def test_empty_config_does_not_crash_and_all_unconfigured(self):
        plugins = list_plugins({})
        by_id = {p["id"]: p for p in plugins}
        assert len(plugins) == len(by_id) == 13
        for plugin_id in (
            "satel", "tuya_local", "tuya_zigbee", "tuya_cloud", "ebus", "home_assistant",
            "mqtt_publish", "unifi_protect", "homewhiz", "presence_simulation", "notifications",
        ):
            assert by_id[plugin_id]["enabled"] is False, plugin_id
            assert by_id[plugin_id]["configured"] is False, plugin_id
        assert by_id["mcp_server"]["enabled"] is False
        assert by_id["mcp_server"]["configured"] is True
        assert by_id["homekit"]["enabled"] is False
        assert by_id["homekit"]["configured"] is True

    def test_satel_configured_when_host_and_key_present(self):
        by_id = {p["id"]: p for p in list_plugins({
            "satel": {"host": "192.168.1.20", "port": 7094, "integration_key": "x"},
        })}
        assert by_id["satel"]["configured"] is True
        assert "192.168.1.20" in by_id["satel"]["detail"]

    def test_tuya_local_and_zigbee_split_by_gateway_field(self):
        config_raw = {
            "tuya": {
                "devices": {
                    "gniazdko": {"id": "d1", "ip": "1.2.3.4", "local_key": "k"},
                    "lampka_zigbee": {"id": "sub1", "gateway": "brama"},
                },
                "zigbee_gateways": {"brama": {"id": "gw1", "ip": "1.2.3.9", "local_key": "gwkey"}},
            },
        }
        by_id = {p["id"]: p for p in list_plugins(config_raw)}
        assert by_id["tuya_local"]["enabled"] is True
        assert "1 urządzeń WiFi" in by_id["tuya_local"]["detail"]
        assert by_id["tuya_zigbee"]["enabled"] is True
        assert "1 bramek" in by_id["tuya_zigbee"]["detail"]
        assert "1 urządzeń" in by_id["tuya_zigbee"]["detail"]

    def test_tuya_local_disabled_when_only_zigbee_devices(self):
        config_raw = {
            "tuya": {
                "devices": {"lampka_zigbee": {"id": "sub1", "gateway": "brama"}},
                "zigbee_gateways": {"brama": {"id": "gw1", "ip": "1.2.3.9", "local_key": "gwkey"}},
            },
        }
        by_id = {p["id"]: p for p in list_plugins(config_raw)}
        assert by_id["tuya_local"]["enabled"] is False

    def test_toggleable_flags_reflected(self):
        config_raw = {
            "tuya": {"cloud": {"access_id": "x"}},
            "ebus": {"mqtt_host": "127.0.0.1"},
            "home_assistant": {"enabled": True, "mqtt_host": "127.0.0.1"},
            "mqtt_publish": {"enabled": True},
            "presence_simulation": {"enabled": True, "devices": ["gniazdko"]},
        }
        by_id = {p["id"]: p for p in list_plugins(config_raw)}
        assert by_id["tuya_cloud"]["enabled"] is True
        assert by_id["tuya_cloud"]["configured"] is True
        assert by_id["home_assistant"]["enabled"] is True
        assert by_id["mqtt_publish"]["enabled"] is True
        assert by_id["presence_simulation"]["enabled"] is True

    def test_mqtt_publish_disabled_without_ebus_broker_even_if_flag_true(self):
        by_id = {p["id"]: p for p in list_plugins({"mqtt_publish": {"enabled": True}})}
        assert by_id["mqtt_publish"]["enabled"] is False

    def test_toggleable_ids_match_expected_set(self):
        toggleable = {p["id"] for p in list_plugins({}) if p["toggleable"]}
        assert toggleable == {
            "satel", "tuya_local", "tuya_cloud", "ebus", "home_assistant",
            "mqtt_publish", "unifi_protect", "homewhiz", "presence_simulation", "notifications",
            "mcp_server", "homekit",
        }

    def test_poll_interval_ids_match_expected_set(self):
        with_interval = {p["id"] for p in list_plugins({}) if p["has_poll_interval"]}
        assert with_interval == {"satel", "tuya_local"}

    def test_poll_interval_defaults(self):
        by_id = {p["id"]: p for p in list_plugins({})}
        assert by_id["satel"]["poll_interval_seconds"] == 3
        assert by_id["tuya_local"]["poll_interval_seconds"] == 30

    def test_poll_interval_reflects_config(self):
        by_id = {p["id"]: p for p in list_plugins({
            "satel": {"status_poll_interval_seconds": 20},
            "tuya": {"status_poll_interval_seconds": 60},
        })}
        assert by_id["satel"]["poll_interval_seconds"] == 20
        assert by_id["tuya_local"]["poll_interval_seconds"] == 60

    def test_other_plugins_have_no_poll_interval_field(self):
        by_id = {p["id"]: p for p in list_plugins({})}
        for plugin_id in (
            "tuya_zigbee", "tuya_cloud", "ebus", "home_assistant",
            "mqtt_publish", "unifi_protect", "presence_simulation", "notifications",
        ):
            assert by_id[plugin_id]["has_poll_interval"] is False
            assert "poll_interval_seconds" not in by_id[plugin_id]

    def test_unifi_protect_and_notifications_are_presence_based(self):
        by_id = {p["id"]: p for p in list_plugins({
            "unifi_protect": {"host": "192.168.1.30"},
            "notifications": {"ntfy_topic_url": "https://ntfy.sh/x"},
        })}
        assert by_id["unifi_protect"]["enabled"] is True
        assert by_id["notifications"]["enabled"] is True
