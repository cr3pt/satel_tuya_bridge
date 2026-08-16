"""Testy dla dashboard/setup.py i tras kreatora (/setup, /api/setup/save,
/api/setup/draft)."""
import pathlib

import pytest
import yaml

from dashboard.setup import (
    build_config_dict, validate, build_automations_dict, validate_automations,
    build_satel_fields_dict, build_tuya_devices_dict, validate_satel_fields, validate_tuya_devices,
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
            "monitored_zones": [{"name": "Salon", "number": "12"}],
            "monitored_outputs": [{"name": "Syrena", "number": "1"}],
            "virtual_inputs": [{"name": "motion_hall_camera", "number": "17", "key": "KLUCZ17"}],
        }
        fields = build_satel_fields_dict(form)
        assert fields == {
            "partitions": {"Dom": 1, "Garaz": 2},
            "monitored_zones": {"Salon": 12},
            "monitored_outputs": {"Syrena": 1},
            "virtual_inputs": {"motion_hall_camera": {"number": 17, "key": "KLUCZ17"}},
        }

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
        assert devices == {"devices": {
            "gniazdko": {"id": "d1", "ip": "1.2.3.4", "local_key": "k", "version": 3.4, "type": "socket"},
        }}

    def test_build_tuya_devices_dimmer_gets_extra_fields(self):
        form = {"tuya_devices": [
            {"name": "d", "id": "x", "ip": "1.2.3.4", "local_key": "k", "type": "dimmer", "dimmer_dp": "3"},
        ]}
        devices = build_tuya_devices_dict(form)
        assert devices["devices"]["d"]["dimmer_dp"] == "3"
        assert devices["devices"]["d"]["dimmer_range"] == [10, 1000]

    def test_build_tuya_devices_empty_form(self):
        assert build_tuya_devices_dict({}) == {"devices": {}}

    def test_validate_tuya_devices_no_errors_when_complete(self):
        form = {"tuya_devices": [{"name": "x", "id": "i", "ip": "1.2.3.4", "local_key": "k"}]}
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


@pytest.fixture
def setup_client(tmp_path):
    """Klient testowy w stanie 'NIE skonfigurowany' - config.yaml jeszcze
    nie istnieje, dokładnie jak przy pierwszym uruchomieniu na czystym RPi."""
    import dashboard.app as app_module

    app_module.history = None
    app_module.sensors_file_path = None
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
    def test_save_creates_config_and_sensors_files(self, setup_client):
        client, tmp_path = setup_client
        resp = client.post("/api/setup/save", json=VALID_FORM)
        assert resp.status_code == 200
        assert (tmp_path / "config.yaml").exists()
        assert (tmp_path / "sensors.yaml").exists()

    def test_save_with_errors_returns_400(self, setup_client):
        client, _ = setup_client
        bad_form = {**VALID_FORM, "admin_password": "short"}
        resp = client.post("/api/setup/save", json=bad_form)
        assert resp.status_code == 400
        assert "details" in resp.get_json()

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

    def test_incomplete_virtual_input_is_error(self):
        form = {**FORM_WITH_ADVANCED, "virtual_inputs": [{"name": "x", "number": "", "key": ""}]}
        errors = validate(form)
        assert any("Wirtualne wejście" in e for e in errors)


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
        assert "escalation" not in built

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
