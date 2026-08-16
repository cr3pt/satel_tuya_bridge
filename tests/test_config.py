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
