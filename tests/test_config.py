"""Testy dla bridge/config.py - wczytywanie config.yaml + scalanie z sensors.yaml."""
import pytest

from bridge.config import BridgeConfig

MINIMAL_CONFIG = """
satel:
  host: "127.0.0.1"
  port: 7094
  integration_key: "x"
  virtual_inputs:
    port: 8888
    url_template: "http://{host}:{port}/x"
    inputs: {}
tuya:
  devices: {}
reguły_ruchu: []
ebus:
  mqtt_host: "127.0.0.1"
  sensors_file: "sensors.yaml"
"""

SENSORS_YAML = """
sensors:
  - name: kociol_temp
    label: "Temperatura"
    mqtt_topic: "ebusd/bai/FlowTemp"
    targets:
      history: true
      mqtt_publish: true
      tuya_virtual_device: false
      tuya_dp: ""
"""


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    (tmp_path / "sensors.yaml").write_text(SENSORS_YAML, encoding="utf-8")
    return tmp_path


def test_load_merges_sensors_file(config_dir):
    config = BridgeConfig.load(config_dir / "config.yaml")
    assert len(config.ebus["sensors"]) == 1
    assert config.ebus["sensors"][0]["name"] == "kociol_temp"


def test_missing_sensors_file_gives_empty_list(tmp_path):
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    config = BridgeConfig.load(tmp_path / "config.yaml")
    assert config.ebus["sensors"] == []


def test_missing_config_file_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.yaml"):
        BridgeConfig.load(tmp_path / "nieistniejacy.yaml")


def test_missing_required_section_raises(tmp_path):
    (tmp_path / "config.yaml").write_text("satel: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Brak wymaganych sekcji"):
        BridgeConfig.load(tmp_path / "config.yaml")


def test_sensors_file_path_relative_to_config(config_dir):
    config = BridgeConfig.load(config_dir / "config.yaml")
    assert config.sensors_file == config_dir / "sensors.yaml"


def test_convenience_properties(config_dir):
    config = BridgeConfig.load(config_dir / "config.yaml")
    assert config.satel["host"] == "127.0.0.1"
    assert config.tuya == {"devices": {}}
    assert config.motion_rules == []
    assert config.boiler_output == {"mode": "mqtt"}  # wartość domyślna gdy sekcja nieobecna
    assert config.unifi_protect is None


AUTOMATIONS_YAML = """
scenes:
  - name: powitanie
    actions:
      - {type: satel_disarm}
reguły_ruchu:
  - source: satel_zone
    zone_number: 5
    target_device: gniazdko
    duration_seconds: 30
presence_simulation:
  enabled: true
  devices: [gniazdko]
"""


class TestAutomationsFile:
    """Sceny/reguły ruchu/symulacja obecności - wczytywane z osobnego
    automations.yaml (patrz dashboard/setup.py i dashboard/app.py:
    panel /automations), analogicznie do sensors.yaml dla czujników."""

    def test_automations_file_overrides_inline_config_values(self, config_dir):
        (config_dir / "automations.yaml").write_text(AUTOMATIONS_YAML, encoding="utf-8")
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["scenes"][0]["name"] == "powitanie"
        assert config.raw["reguły_ruchu"][0]["zone_number"] == 5
        assert config.raw["presence_simulation"]["enabled"] is True
        assert config.automations_file == config_dir / "automations.yaml"

    def test_missing_automations_file_defaults_to_empty(self, config_dir):
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["scenes"] == []
        assert config.raw["reguły_ruchu"] == []
        assert config.raw["presence_simulation"] == {"enabled": False}

    def test_inline_values_used_when_automations_file_missing(self, tmp_path):
        """Wsteczna zgodność - configi sprzed rozdzielenia do automations.yaml
        mają sceny/reguły wpisane wprost w config.yaml; muszą wciąż działać,
        dopóki nikt nie zapisze niczego przez panel /automations."""
        inline_config = MINIMAL_CONFIG.replace("reguły_ruchu: []", """reguły_ruchu:
  - source: satel_zone
    zone_number: 9
    target_device: x
    duration_seconds: 10
scenes:
  - name: legacy_scene
    actions: [{type: satel_disarm}]
""")
        (tmp_path / "config.yaml").write_text(inline_config, encoding="utf-8")
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.raw["reguły_ruchu"][0]["zone_number"] == 9
        assert config.raw["scenes"][0]["name"] == "legacy_scene"

    def test_custom_automations_file_path(self, tmp_path):
        config_text = MINIMAL_CONFIG + "automations_file: custom_automations.yaml\n"
        (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
        (tmp_path / "custom_automations.yaml").write_text(
            "scenes:\n  - name: x\n    actions: [{type: satel_disarm}]\n", encoding="utf-8",
        )
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.raw["scenes"][0]["name"] == "x"
        assert config.automations_file == tmp_path / "custom_automations.yaml"


SATEL_FIELDS_YAML = """
monitored_zones:
  Salon: 12
monitored_outputs:
  Syrena: 1
partitions:
  Dom: 1
  Garaz: 2
virtual_inputs:
  motion_hall_camera:
    number: 17
    key: KLUCZ17
zone_types:
  Salon: ruch
output_types:
  Syrena: syrena
"""

TUYA_DEVICES_YAML = """
devices:
  gniazdko_salon:
    id: dev1
    ip: 192.168.1.50
    local_key: key1
    version: 3.4
    type: socket
"""


class TestSatelFieldsAndTuyaDevicesFiles:
    """Strefy/wyjścia/wejścia IP/partycje Satela i urządzenia Tuya - też
    wczytywane z osobnych plików (satel_fields.yaml / tuya_devices.yaml),
    z tego samego powodu co automations.yaml/sensors.yaml: panel /config
    może je bezpiecznie nadpisywać w całości."""

    def test_satel_fields_file_overrides_inline_values(self, config_dir):
        (config_dir / "satel_fields.yaml").write_text(SATEL_FIELDS_YAML, encoding="utf-8")
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["satel"]["monitored_zones"] == {"Salon": 12}
        assert config.raw["satel"]["monitored_outputs"] == {"Syrena": 1}
        assert config.raw["satel"]["partitions"] == {"Dom": 1, "Garaz": 2}
        assert config.raw["satel"]["virtual_inputs"]["inputs"] == {
            "motion_hall_camera": {"number": 17, "key": "KLUCZ17"},
        }
        assert config.raw["satel"]["zone_types"] == {"Salon": "ruch"}
        assert config.raw["satel"]["output_types"] == {"Syrena": "syrena"}
        assert config.satel_fields_file == config_dir / "satel_fields.yaml"

    def test_tuya_devices_file_overrides_inline_values(self, config_dir):
        (config_dir / "tuya_devices.yaml").write_text(TUYA_DEVICES_YAML, encoding="utf-8")
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["tuya"]["devices"]["gniazdko_salon"]["ip"] == "192.168.1.50"
        assert config.tuya_devices_file == config_dir / "tuya_devices.yaml"

    def test_missing_satel_fields_and_tuya_devices_files_default_to_empty(self, config_dir):
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["satel"]["monitored_zones"] == {}
        assert config.raw["satel"]["monitored_outputs"] == {}
        assert config.raw["satel"]["partitions"] == {}
        assert config.raw["satel"]["virtual_inputs"]["inputs"] == {}
        assert config.raw["satel"]["zone_types"] == {}
        assert config.raw["satel"]["output_types"] == {}
        assert config.raw["tuya"]["devices"] == {}

    def test_inline_values_used_when_satel_fields_file_missing(self, tmp_path):
        """Wsteczna zgodność - configi z polami wpisanymi wprost w
        config.yaml (jak generował stary kreator) wciąż działają."""
        inline_config = MINIMAL_CONFIG.replace(
            'inputs: {}', 'inputs: {motion_hall_camera: {number: 17, key: "K"}}',
        ).replace(
            'integration_key: "x"\n',
            'integration_key: "x"\n  partitions: {Dom: 1}\n  monitored_zones: {Salon: 12}\n',
        )
        (tmp_path / "config.yaml").write_text(inline_config, encoding="utf-8")
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.raw["satel"]["partitions"] == {"Dom": 1}
        assert config.raw["satel"]["monitored_zones"] == {"Salon": 12}
        assert config.raw["satel"]["virtual_inputs"]["inputs"]["motion_hall_camera"]["number"] == 17

    def test_inline_tuya_devices_used_when_file_missing(self, tmp_path):
        inline_config = MINIMAL_CONFIG.replace(
            "tuya:\n  devices: {}",
            'tuya:\n  devices: {gniazdko: {id: "d1", ip: "1.2.3.4", local_key: "k", version: 3.4, type: socket}}',
        )
        (tmp_path / "config.yaml").write_text(inline_config, encoding="utf-8")
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.raw["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"

    def test_custom_satel_fields_and_tuya_devices_file_paths(self, tmp_path):
        config_text = MINIMAL_CONFIG.replace(
            'integration_key: "x"\n', 'integration_key: "x"\n  fields_file: custom_satel.yaml\n',
        ).replace("tuya:\n  devices: {}", "tuya:\n  devices: {}\n  devices_file: custom_tuya.yaml")
        (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
        (tmp_path / "custom_satel.yaml").write_text("partitions: {Dom: 1}\n", encoding="utf-8")
        (tmp_path / "custom_tuya.yaml").write_text(
            'devices: {x: {id: "i", ip: "1.2.3.4", local_key: "k", version: 3.4, type: socket}}\n',
            encoding="utf-8",
        )
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.raw["satel"]["partitions"] == {"Dom": 1}
        assert config.raw["tuya"]["devices"]["x"]["ip"] == "1.2.3.4"
        assert config.satel_fields_file == tmp_path / "custom_satel.yaml"
        assert config.tuya_devices_file == tmp_path / "custom_tuya.yaml"
