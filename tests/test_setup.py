"""Testy dla dashboard/setup.py i tras kreatora (/setup, /api/setup/save)."""
import pathlib

import pytest
import yaml

from dashboard.setup import build_config_dict, validate


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

    def test_partitions_and_zones_converted_to_int(self):
        config = build_config_dict(VALID_FORM)
        assert config["satel"]["partitions"] == {"Dom": 1}
        assert config["satel"]["monitored_zones"] == {"Salon": 12}

    def test_empty_partitions_defaults_to_dom_1(self):
        form = {**VALID_FORM, "partitions": []}
        config = build_config_dict(form)
        assert config["satel"]["partitions"] == {"Dom": 1}

    def test_tuya_devices_structure(self):
        config = build_config_dict(VALID_FORM)
        assert "gniazdko_salon" in config["tuya"]["devices"]
        assert config["tuya"]["devices"]["gniazdko_salon"]["ip"] == "192.168.1.50"

    def test_dimmer_device_gets_dp_fields(self):
        form = {**VALID_FORM, "tuya_devices": [
            {"name": "d", "id": "x", "ip": "1.2.3.4", "local_key": "k", "type": "dimmer", "dimmer_dp": "3"},
        ]}
        config = build_config_dict(form)
        assert config["tuya"]["devices"]["d"]["dimmer_dp"] == "3"
        assert "dimmer_range" in config["tuya"]["devices"]["d"]

    def test_password_is_hashed_not_plaintext(self):
        config = build_config_dict(VALID_FORM)
        stored = config["dashboard"]["auth"]["password_hash"]
        assert stored != VALID_FORM["admin_password"]
        assert stored.startswith("scrypt:") or stored.startswith("pbkdf2:")

    def test_advanced_sections_get_empty_safe_defaults(self):
        config = build_config_dict(VALID_FORM)
        assert config["reguły_ruchu"] == []
        assert config["scenes"] == []
        assert config["alarm_rules"] == []
        assert config["presence_simulation"] == {"enabled": False}

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
