"""Testy dla dashboard/setup.py i tras kreatora (/setup, /api/setup/save,
/api/setup/draft)."""
import pathlib

import pytest
import yaml

from dashboard.setup import (
    build_config_dict, validate, build_automations_dict, validate_automations,
    build_satel_fields_dict, build_tuya_devices_dict, validate_satel_fields, validate_tuya_devices,
    validate_cloud_devices, cloud_devices_to_rows,
    build_connections_patch, validate_connections, connections_to_form, list_plugins,
    draft_path_for, load_draft, save_draft, delete_draft,
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

    def test_missing_integration_key_is_error(self):
        form = {**VALID_FORM, "satel": {**VALID_FORM["satel"], "integration_key": ""}}
        errors = validate(form)
        assert any("klucz integracji" in e for e in errors)

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

    def test_satel_config_has_fields_file_marker_not_inline_fields(self):
        config = build_config_dict(VALID_FORM)
        assert config["satel"]["fields_file"] == "satel_fields.yaml"
        assert "partitions" not in config["satel"]
        assert "monitored_zones" not in config["satel"]
        assert "inputs" not in config["satel"]["virtual_inputs"]

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
        """W odróżnieniu od hasła, nazwa użytkownika NIE jest sekretem (jak
        login) - zapisywana jawnym tekstem, nie hashowana."""
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
        # Sceny/reguły_ruchu/presence_simulation NIE są tutaj - patrz
        # build_automations_dict (automations.yaml), testowane osobno niżej.
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
        # brak własnego mqtt_host -> spada na ten sam co eBUS
        assert config["home_assistant"]["mqtt_host"] == "127.0.0.1"

    def test_unifi_protect_omitted_when_no_host(self):
        config = build_config_dict(VALID_FORM)
        assert "unifi_protect" not in config

    def test_unifi_protect_included_when_host_given(self):
        form = {**VALID_FORM, "unifi_protect": {"host": "192.168.1.5", "username": "admin", "password": "x"}}
        config = build_config_dict(form)
        assert config["unifi_protect"]["host"] == "192.168.1.5"

    def test_tuya_cloud_uses_region_derived_endpoints_from_wizard(self):
        """Kreator wyprowadza api_endpoint/mq_endpoint z regionu wybranego w
        kroku 2 (patrz setup.html: TUYA_REGION_ENDPOINTS) i wysyła je wprost
        w formularzu - build_config_dict ma je zapisać, NIE nadpisywać
        domyślnym (UE), inaczej integracja łączy się z niewłaściwym regionem
        mimo poprawnie wybranego przez użytkownika (dokładnie ten bug, który
        mógł powodować 401 na Pulsar mimo poprawnych danych)."""
        form = {**VALID_FORM, "tuya_cloud": {
            "enabled": True, "access_id": "id1", "access_key": "key1",
            "api_endpoint": "https://openapi.tuyaus.com", "mq_endpoint": "wss://mqe.tuyaus.com:8285/",
        }}
        config = build_config_dict(form)
        assert config["tuya"]["cloud"]["api_endpoint"] == "https://openapi.tuyaus.com"
        assert config["tuya"]["cloud"]["mq_endpoint"] == "wss://mqe.tuyaus.com:8285/"

    def test_tuya_cloud_blank_endpoints_fall_back_to_eu_defaults(self):
        """Dla regionów bez pewnego adresu Pulsar (us-e/eu-w/sg) kreator
        celowo wysyła mq_endpoint jako pusty string zamiast zgadywać - to
        MUSI spaść na domyślny adres UE, nie zostać zapisane jako pusty."""
        form = {**VALID_FORM, "tuya_cloud": {
            "enabled": True, "access_id": "id1", "access_key": "key1",
            "api_endpoint": "", "mq_endpoint": "",
        }}
        config = build_config_dict(form)
        assert config["tuya"]["cloud"]["api_endpoint"] == "https://openapi.tuyaeu.com"
        assert config["tuya"]["cloud"]["mq_endpoint"] == "wss://mqe.tuyaeu.com:8285/"

    def test_result_is_yaml_serializable(self):
        """Krytyczne - jeśli to się nie da zserializować, zapis configu
        wywali się w runtime, więc test to wyłapuje na sucho."""
        config = build_config_dict(VALID_FORM)
        dumped = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        reloaded = yaml.safe_load(dumped)
        assert reloaded["satel"]["host"] == "192.168.1.10"


class TestSatelFieldsAndTuyaDevicesBuilders:
    """build_satel_fields_dict/build_tuya_devices_dict i ich walidatory -
    współdzielone przez kreator i panel /config (patrz dashboard/app.py:
    /api/config/satel-fields, /api/config/tuya-devices)."""

    def test_build_satel_fields_shape(self):
        form = {
            "partitions": [{"name": "Dom", "number": "1"}, {"name": "Garaz", "number": "2"}],
            "monitored_zones": [{"name": "Salon", "number": "12", "type": "ruch"}],
            "monitored_outputs": [{"name": "Syrena", "number": "1", "type": "syrena"}],
            "virtual_inputs": [{"name": "motion_hall_camera", "number": "17", "key": "KLUCZ17"}],
        }
        fields = build_satel_fields_dict(form)
        assert fields == {
            "partitions": {"Dom": 1, "Garaz": 2},
            "monitored_zones": {"Salon": 12},
            "monitored_outputs": {"Syrena": 1},
            "virtual_inputs": {"motion_hall_camera": {"number": 17, "key": "KLUCZ17"}},
            "zone_types": {"Salon": "ruch"},
            "output_types": {"Syrena": "syrena"},
        }

    def test_build_satel_fields_type_ignored_when_unknown_slug(self):
        """Nieznany slug rodzaju (np. literówka albo stary zapis sprzed tej
        funkcji) jest po prostu pomijany - nie wywala walidacji/zapisu."""
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
        form = {"virtual_inputs": [{"name": "x", "number": "1", "key": "k"}]}
        assert validate_satel_fields(form) == []

    def test_validate_satel_fields_incomplete_virtual_input_is_error(self):
        form = {"virtual_inputs": [{"name": "x", "number": "", "key": "k"}]}
        errors = validate_satel_fields(form)
        assert any("Wirtualne wejście" in e for e in errors)

    def test_validate_satel_fields_does_not_require_zone_number_pairs(self):
        """Niepełne wiersze stref/wyjść/partycji nie są błędem - po prostu
        pomijane przy zapisie (patrz build_satel_fields_dict)."""
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
        """Podurządzenie Zigbee (pole 'gateway') idzie przez połączenie
        bramki - nie ma sensu żądać od niego własnego ip/local_key/version
        (patrz TuyaLocalController w bridge/tuya_local.py)."""
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
            "virtual_inputs": [{"name": "x", "number": "1", "key": "k"}],
            "tuya_devices": [{"name": "d", "id": "i", "ip": "1.2.3.4", "local_key": "k"}],
        }
        dumped_satel = yaml.safe_dump(build_satel_fields_dict(form), allow_unicode=True, sort_keys=False)
        dumped_tuya = yaml.safe_dump(build_tuya_devices_dict(form), allow_unicode=True, sort_keys=False)
        assert yaml.safe_load(dumped_satel)["partitions"] == {"Dom": 1}
        assert yaml.safe_load(dumped_tuya)["devices"]["d"]["ip"] == "1.2.3.4"


class TestCloudDevicesBuilder:
    """Urządzenia sterowane WYŁĄCZNIE przez Tuya Cloud (bez local_key/IP) -
    zgłoszony błąd: import z Tuya Cloud dawał "nazwa, ID, adres IP i
    local_key są wymagane" dla KAŻDEGO urządzenia bez local_key/IP, bo
    lądowały w walidacji urządzeń lokalnych (validate_tuya_devices) zamiast
    tutaj (validate_cloud_devices, wymaga tylko nazwy i ID)."""

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
        """To jest sedno naprawy - w odróżnieniu od validate_tuya_devices,
        BRAK ip/local_key nie jest błędem tutaj."""
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
        """Reprodukcja pełnego zgłoszonego formularza z 40 urządzeniami bez
        local_key/IP - żaden nie powinien dawać błędu walidacji, wszystkie
        powinny wylądować w cloud_devices, nie w devices."""
        form = {"cloud_devices": [{"name": f"urzadzenie_{i}", "id": f"id{i}"} for i in range(40)]}
        assert validate_cloud_devices(form) == []
        result = build_tuya_devices_dict(form)
        assert len(result["cloud_devices"]) == 40
        assert result["devices"] == {}


class TestConnectionsBuilder:
    """build_connections_patch/validate_connections/connections_to_form -
    zakładka "Połączenia" panelu /config, jedyne miejsce do zmiany
    host/port/kluczy/tematów MQTT PO pierwszej konfiguracji (patrz
    dashboard/app.py: /api/config/connections). W przeciwieństwie do
    build_config_dict (kreator) - patchuje ISTNIEJĄCY config.yaml, nie
    buduje go od zera, więc testy skupiają się na tym, co jest ZACHOWANE
    z `existing`, a co NADPISANE formularzem."""

    EXISTING = {
        "satel": {
            "host": "192.168.1.10", "port": 7094, "integration_key": "stary_klucz", "partition": 1,
            "virtual_inputs": {"port": 8888, "url_template": "http://{host}:{port}/input.cgi?key={key}&input={input_no}"},
            "outputs": {}, "fields_file": "satel_fields.yaml",
        },
        "tuya": {"status_poll_interval_seconds": 30, "devices_file": "tuya_devices.yaml"},
        "ebus": {"mqtt_host": "127.0.0.1", "mqtt_port": 1883, "sensors_file": "sensors.yaml"},
        "boiler_output": {"mqtt": {"host": "127.0.0.1", "port": 1883, "topic_prefix": "home/boiler"},
                           "tuya_virtual_device": {"device_id": ""}},
        "notifications": {"ntfy_topic_url": "", "grace_seconds": 120},
        # Pola spoza formularza połączeń - MUSZĄ przetrwać nietknięte.
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
        """Kluczowy przypadek: pusty klucz w formularzu (bo formularz NIGDY
        nie dostaje prawdziwego sekretu z powrotem - patrz connections_to_form)
        NIE MOŻE skasować już zapisanego klucza integracji."""
        form = {"satel": {"host": "192.168.1.10", "integration_key": ""}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["satel"]["integration_key"] == "stary_klucz"

    def test_non_blank_integration_key_overwrites(self):
        form = {"satel": {"host": "192.168.1.10", "integration_key": "nowy_klucz"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["satel"]["integration_key"] == "nowy_klucz"

    def test_ebus_topic_prefix_updates_boiler_output_mqtt(self):
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1", "topic_prefix": "ebusd_state"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["boiler_output"]["mqtt"]["topic_prefix"] == "ebusd_state"

    def test_mqtt_publish_topics_default_to_satel_and_tuya(self):
        form = {"satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["mqtt_publish"] == {"enabled": True, "topics": {"satel": "satel", "tuya": "tuya"}}

    def test_mqtt_publish_topics_configurable(self):
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "mqtt_publish": {"enabled": True, "satel_topic": "alarm", "tuya_topic": "smart_home"},
        }
        patched = build_connections_patch(form, self.EXISTING)
        assert patched["mqtt_publish"]["topics"] == {"satel": "alarm", "tuya": "smart_home"}

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
        """log_level (patrz plugins/mqtt_publish/routes.py) nie pochodzi z
        TEGO formularza - musi przetrwać, nawet gdy zapisujemy stąd tylko
        tematy MQTT."""
        existing = {**self.EXISTING, "mqtt_publish": {"enabled": True, "topics": {"satel": "satel", "tuya": "tuya"}, "log_level": "DEBUG"}}
        form = {
            "satel": {"host": "192.168.1.10"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "mqtt_publish": {"enabled": True, "satel_topic": "alarm", "tuya_topic": "smart_home"},
        }
        patched = build_connections_patch(form, existing)
        assert patched["mqtt_publish"]["log_level"] == "DEBUG"
        assert patched["mqtt_publish"]["topics"] == {"satel": "alarm", "tuya": "smart_home"}

    def test_tuya_cloud_preserves_log_level_and_real_enabled_flag(self):
        """log_level i "enabled" ustawione przez /plugins/tuya_cloud (⚙
        Ustawienia wtyczki) muszą przetrwać zapis stąd - w odróżnieniu od
        "enabled" w TYM formularzu (tylko sygnał "czy w ogóle
        skonfigurowane", patrz connections_to_form: bool(tuya_cloud))."""
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
        assert patched["tuya"]["cloud"]["enabled"] is False  # nietknięte, mimo enabled=True w formularzu

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

    def test_roundtrip_with_build_connections_patch(self):
        """connections_to_form(existing) -> uzupełnij formularz w
        przeglądarce (bez zmiany sekretów, bo user zostawia je puste) ->
        build_connections_patch -> te same wartości niesekretne."""
        existing = TestConnectionsBuilder.EXISTING
        form = connections_to_form(existing)
        # Symulacja formularza po stronie przeglądarki: sekrety puste
        # (użytkownik ich nie zmienił), reszta wypełniona z GET.
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


@pytest.fixture
def setup_client(tmp_path):
    """Klient testowy w stanie 'NIE skonfigurowany' - config.yaml jeszcze
    nie istnieje, dokładnie jak przy pierwszym uruchomieniu na czystym RPi."""
    import dashboard.app as app_module

    app_module.history = None
    app_module.config_store = None
    app_module.auth_guard = None
    app_module.satel_partitions_cfg = {}
    app_module.config_path = tmp_path / "config.yaml"  # celowo NIE tworzymy tego pliku
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), tmp_path


class TestSetupRedirect:
    def test_index_redirects_to_setup_when_unconfigured(self, setup_client):
        client, _ = setup_client
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/setup" in resp.headers["Location"]

    def test_config_page_redirects_to_setup_when_unconfigured(self, setup_client):
        client, _ = setup_client
        resp = client.get("/config", follow_redirects=False)
        assert resp.status_code == 302

    def test_setup_page_itself_loads_without_redirect(self, setup_client):
        client, _ = setup_client
        resp = client.get("/setup")
        assert resp.status_code == 200

    def test_satel_connection_test_reachable_without_redirect(self, setup_client, monkeypatch):
        """Kreator (krok "Centrala Satel") musi móc wywołać test połączenia
        ZANIM config.yaml w ogóle istnieje - endpoint jest zwolniony z
        przekierowania na /setup (patrz _SETUP_EXEMPT_ENDPOINTS)."""
        client, _ = setup_client

        async def fake_test_connection(host, port):
            return None

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        resp = client.post("/api/setup/test-satel-connection", json={"host": "192.168.1.10", "port": 7094})
        assert resp.status_code == 200

    def test_discover_tuya_cloud_reachable_without_redirect(self, setup_client, monkeypatch):
        client, _ = setup_client
        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", lambda *a, **kw: [])
        resp = client.post(
            "/api/discover/tuya-cloud",
            json={"api_region": "eu", "access_id": "x", "access_key": "y"},
        )
        assert resp.status_code == 200

    def test_discover_tuya_lan_scan_reachable_without_redirect(self, setup_client, monkeypatch):
        client, _ = setup_client
        monkeypatch.setattr("dashboard.app.scan_tuya_network", lambda: [])
        resp = client.post("/api/discover/tuya", json={})
        assert resp.status_code == 200


class TestSetupSave:
    def test_save_creates_config_db(self, setup_client):
        client, tmp_path = setup_client
        resp = client.post("/api/setup/save", json=VALID_FORM)
        assert resp.status_code == 200
        assert (tmp_path / "config.db").exists()

        import dashboard.app as app_module
        assert app_module.config_store.get("config")["satel"]["host"]
        assert app_module.config_store.has("sensors")

    def test_save_with_errors_returns_400(self, setup_client):
        client, _ = setup_client
        bad_form = {**VALID_FORM, "admin_password": "short"}
        resp = client.post("/api/setup/save", json=bad_form)
        assert resp.status_code == 400
        assert "details" in resp.get_json()

    def test_save_with_non_numeric_field_returns_json_400_not_html_500(self, setup_client):
        """Reprodukcja zgłoszonego błędu: pole "liczbowe" (tu: numer strefy)
        z tekstem zamiast liczby nie może lecieć jako nieobsłużony wyjątek
        (HTML 500 - JS robi `await resp.json()`, więc dostawałby
        `SyntaxError: Unexpected token '<'` bez żadnej wskazówki) - musi
        wrócić jako czytelny JSON 400. `validate()` świadomie nie sprawdza
        formatu KAŻDEGO pola liczbowego (za dużo miejsc), więc to
        `build_config_dict` (przez `int()`) naturalnie rzuca ValueError -
        `api_setup_save` musi to złapać (patrz dashboard/app.py)."""
        client, _ = setup_client
        bad_form = {
            **VALID_FORM,
            "partitions": [{"name": "Dom", "number": "niejestem_liczbą"}],
        }
        resp = client.post("/api/setup/save", json=bad_form)
        assert resp.status_code == 400
        data = resp.get_json()
        assert "details" in data
        assert "liczbow" in data["details"][0].lower()

    def test_unexpected_error_in_api_route_returns_json_not_html(self, setup_client, monkeypatch):
        """Zabezpieczenie ogólne (handle_unexpected_error w dashboard/app.py)
        - NIEZALEŻNIE od tego, gdzie dokładnie wybuchnie, każdy endpoint
        /api/* musi zwrócić JSON, nie domyślną stronę HTML Flaska."""
        import dashboard.setup as setup_module

        client, _ = setup_client

        def _boom(form):
            raise RuntimeError("symulowany nieoczekiwany błąd")

        monkeypatch.setattr(setup_module, "build_config_dict", _boom)
        import dashboard.app as app_module
        monkeypatch.setattr(app_module, "build_config_dict", _boom)

        resp = client.post("/api/setup/save", json=VALID_FORM)
        assert resp.status_code == 500
        assert resp.content_type.startswith("application/json")
        assert "błąd serwera" in resp.get_json()["error"].lower()

    def test_permission_error_in_api_route_returns_actionable_message(self, setup_client, monkeypatch):
        """Reprodukcja realnego zgłoszonego przypadku: wolumen danych w
        Dockerze niezapisywalny dla appuser (UID 1000) - PermissionError
        (np. z save_draft) musi wrócić jako JSON z konkretną wskazówką
        o uprawnieniach/chown, nie ogólnikowe "nieoczekiwany błąd"."""
        import dashboard.setup as setup_module

        client, _ = setup_client

        def _boom(path, data):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(setup_module, "save_draft", _boom)
        import dashboard.app as app_module
        monkeypatch.setattr(app_module, "save_draft", _boom)

        resp = client.post("/api/setup/draft", json={"step": 1})
        assert resp.status_code == 500
        assert resp.content_type.startswith("application/json")
        error = resp.get_json()["error"].lower()
        assert "uprawnie" in error
        assert "chown" in error

    def test_dashboard_usable_immediately_after_save(self, setup_client):
        """Po zapisie panel powinien być od razu dostępny w TYM SAMYM
        procesie, bez ręcznego restartu dashboardu."""
        client, _ = setup_client
        client.post("/api/setup/save", json=VALID_FORM)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200  # już NIE przekierowuje na /setup

    def test_second_save_attempt_rejected(self, setup_client):
        """Kreator nie może nadpisać istniejącej, być może ręcznie
        zmienionej konfiguracji."""
        client, _ = setup_client
        client.post("/api/setup/save", json=VALID_FORM)
        resp = client.post("/api/setup/save", json=VALID_FORM)
        assert resp.status_code == 409

    def test_setup_page_redirects_to_index_once_configured(self, setup_client):
        client, _ = setup_client
        client.post("/api/setup/save", json=VALID_FORM)
        resp = client.get("/setup", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/") or "index" in resp.headers["Location"] or resp.headers["Location"] == "/"

    def test_saved_config_is_loadable_by_bridge_config(self, setup_client):
        """Sprawdza, że plik wygenerowany przez kreator to nie tylko
        poprawny YAML, ale też przechodzi pełną walidację BridgeConfig,
        której będzie używał GŁÓWNY proces bridge'a."""
        from bridge.config import BridgeConfig

        client, tmp_path = setup_client
        client.post("/api/setup/save", json=VALID_FORM)
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.satel["host"] == "192.168.1.10"
        assert config.ebus["sensors"] == []  # pusta lista - dodaje się przez /config


# ---------------------------------------------------------------------
# Sceny, reguły ruchu (harmonogram + eskalacja), wirtualne wejścia
# ---------------------------------------------------------------------

FORM_WITH_ADVANCED = {
    **VALID_FORM,
    "virtual_inputs": [{"name": "motion_hall_camera", "number": "17", "key": "KLUCZ17"}],
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
        """Puste "Czas włączenia" dla zwykłej akcji "włącz urządzenie" NIE
        jest błędem - oznacza "aż do ustania naruszenia", patrz README."""
        rule = {"zone_number": "16", "target_device": "swiatlo_korytarz", "duration_seconds": ""}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        assert validate(form) == []

    def test_motion_rule_unknown_trigger_on_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "trigger_on": "cos_innego"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule]}
        errors = validate(form)
        assert any("wyzwalacz" in e for e in errors)

    def test_incomplete_virtual_input_is_error(self):
        form = {**FORM_WITH_ADVANCED, "virtual_inputs": [{"name": "x", "number": "", "key": ""}]}
        errors = validate(form)
        assert any("Wirtualne wejście" in e for e in errors)


class TestConditionAndChainingValidation:
    """Walidacja nowych pól reguły ruchu: nazwa (do wyzwalania łańcuchowego),
    then_trigger_rule i dodatkowy warunek (and/or/not/state/time_window) -
    patrz dashboard/setup.py: _validate_condition/_validate_motion_rules."""

    def test_duplicate_rule_names_is_error(self):
        rule = {**FORM_WITH_ADVANCED["motion_rules"][0], "name": "dup"}
        form = {**FORM_WITH_ADVANCED, "motion_rules": [rule, {**rule}]}
        errors = validate(form)
        assert any("nazwa 'dup' powtarza się" in e for e in errors)

    def test_scene_and_rule_sharing_a_name_is_error(self):
        """Sceny i reguły ruchu dzielą wspólny klucz statusu
        "automation:<nazwa>" (warunek "inna automatyzacja jest uruchomiona")
        - kolizja nazw nadpisywałaby sobie nawzajem status, więc musi być
        odrzucona przy zapisie (patrz validate_automations)."""
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
    """presence_simulation nie jest częścią formularza kreatora, ale JEST
    częścią panelu /automations - build_automations_dict/validate_automations
    muszą obsłużyć ten sam kształt danych, co scenesy/reguły ruchu."""

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
        """enabled=true (przełącznik "Włączona" zaznaczony, domyślnie) NIE
        jest zapisywane jawnie - żeby nie zaśmiecać configu istniejących
        scen bez zmiany zachowania (patrz analogiczny wzorzec dla
        trigger_on reguł ruchu)."""
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

    def test_arm_disarm_actions_capture_explicit_partitions(self):
        """Regresja: sceny wcześniej nie miały żadnego sposobu na wskazanie,
        KTÓRE strefy uzbroić/rozbroić - zawsze dotykały tylko domyślnej
        (patrz bridge/scenes.py). `partitions` jest opcjonalne (pominięte =
        stare zachowanie), ale gdy podane, musi przejść przez build w
        całości, jako liczby całkowite (nie stringi z formularza)."""
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
        """Puste "Czas włączenia" ("duration_seconds": "") - NIE zapisujemy
        klucza wcale (nie 0), bo jego BRAK jest tym, co RulesEngine
        rozpoznaje jako "włącz aż do ustania naruszenia" (patrz README)."""
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
        """trigger_on nieobecne w wybudowanej regule = domyślne "violated" -
        nie zapisujemy go jawnie, żeby nie zaśmiecać configu istniejących
        (starszych) reguł bez zmiany zachowania."""
        automations = build_automations_dict(FORM_WITH_ADVANCED)
        assert "trigger_on" not in automations["reguły_ruchu"][0]

    def test_virtual_inputs_built_into_satel_fields(self):
        satel_fields = build_satel_fields_dict(FORM_WITH_ADVANCED)
        assert satel_fields["virtual_inputs"] == {
            "motion_hall_camera": {"number": 17, "key": "KLUCZ17"},
        }

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
        """Sprawdza integrację end-to-end: automations.yaml z kreatora
        (sceny + reguła z harmonogramem/eskalacją) da się podać wprost do
        SceneRunner bez żadnych transformacji."""
        from bridge.scenes import SceneRunner

        automations = build_automations_dict(FORM_WITH_ADVANCED)
        dumped = yaml.safe_dump(automations, allow_unicode=True, sort_keys=False)
        reloaded = yaml.safe_load(dumped)
        runner = SceneRunner(reloaded["scenes"], tuya=None, satel=None, satel_virtual_inputs=None)
        assert runner.exists("wykryto_intruza")


class TestSetupDraft:
    def test_no_draft_returns_empty_object(self, setup_client):
        client, _ = setup_client
        resp = client.get("/api/setup/draft")
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_saving_and_loading_draft_roundtrips(self, setup_client):
        client, _ = setup_client
        payload = {"step": 3, "data": {"satel": {"host": "192.168.1.10"}}}
        resp = client.post("/api/setup/draft", json=payload)
        assert resp.status_code == 200

        resp = client.get("/api/setup/draft")
        assert resp.get_json() == payload

    def test_draft_persists_to_disk_next_to_config_path(self, setup_client):
        client, tmp_path = setup_client
        client.post("/api/setup/draft", json={"step": 2, "data": {"a": 1}})
        assert (tmp_path / ".setup_draft.json").exists()

    def test_draft_overwritten_by_later_save(self, setup_client):
        client, _ = setup_client
        client.post("/api/setup/draft", json={"step": 2, "data": {"a": 1}})
        client.post("/api/setup/draft", json={"step": 5, "data": {"a": 2}})
        resp = client.get("/api/setup/draft")
        assert resp.get_json() == {"step": 5, "data": {"a": 2}}

    def test_draft_rejected_when_already_configured(self, setup_client):
        client, _ = setup_client
        client.post("/api/setup/save", json=VALID_FORM)
        resp = client.post("/api/setup/draft", json={"step": 1, "data": {}})
        assert resp.status_code == 409
        resp = client.get("/api/setup/draft")
        assert resp.status_code == 409

    def test_final_save_deletes_draft(self, setup_client):
        client, tmp_path = setup_client
        client.post("/api/setup/draft", json={"step": 4, "data": {"a": 1}})
        assert (tmp_path / ".setup_draft.json").exists()
        client.post("/api/setup/save", json=VALID_FORM)
        assert not (tmp_path / ".setup_draft.json").exists()

    def test_invalid_draft_body_returns_400(self, setup_client):
        client, _ = setup_client
        resp = client.post("/api/setup/draft", data="not json", content_type="application/json")
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
        delete_draft(path)  # nie istnieje - nie powinno wywalić
        save_draft(path, {"a": 1})
        delete_draft(path)
        assert not path.exists()

    def test_draft_path_for_is_next_to_config(self, tmp_path):
        config_path = tmp_path / "sub" / "config.yaml"
        assert draft_path_for(config_path) == tmp_path / "sub" / ".setup_draft.json"


class TestListPlugins:
    """list_plugins() - zakładka 🧩 Wtyczki, spójny przegląd wszystkich
    integracji na bazie AKTUALNEGO config.raw (patrz PLUGIN_DEFS)."""

    def test_empty_config_does_not_crash_and_all_unconfigured(self):
        plugins = list_plugins({})
        by_id = {p["id"]: p for p in plugins}
        assert len(plugins) == len(by_id) == 10
        # przy pustym configu nic nie jest skonfigurowane, więc nic nie jest
        # też "enabled" (satel/tuya/ebus itd. wymagają realnej konfiguracji)
        for plugin_id in (
            "satel", "tuya_local", "tuya_zigbee", "tuya_cloud", "ebus", "home_assistant",
            "mqtt_publish", "unifi_protect", "presence_simulation", "notifications",
        ):
            assert by_id[plugin_id]["enabled"] is False, plugin_id
            assert by_id[plugin_id]["configured"] is False, plugin_id

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
        """mqtt_publish.enabled samo w sobie nic nie znaczy bez brokera
        (patrz RulesEngine.__init__: wymaga też config.ebus.mqtt_host)."""
        by_id = {p["id"]: p for p in list_plugins({"mqtt_publish": {"enabled": True}})}
        assert by_id["mqtt_publish"]["enabled"] is False

    def test_toggleable_ids_match_expected_set(self):
        toggleable = {p["id"] for p in list_plugins({}) if p["toggleable"]}
        # tuya_zigbee to jedyny wyjątek - dzieli połączenie/runtime z tuya_local,
        # więc nie ma niezależnego cyklu życia do włączania/wyłączania
        assert toggleable == {
            "satel", "tuya_local", "tuya_cloud", "ebus", "home_assistant",
            "mqtt_publish", "unifi_protect", "presence_simulation", "notifications",
        }

    def test_poll_interval_ids_match_expected_set(self):
        with_interval = {p["id"] for p in list_plugins({}) if p["has_poll_interval"]}
        assert with_interval == {"satel", "tuya_local"}

    def test_poll_interval_defaults(self):
        by_id = {p["id"]: p for p in list_plugins({})}
        assert by_id["satel"]["poll_interval_seconds"] == 15
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
