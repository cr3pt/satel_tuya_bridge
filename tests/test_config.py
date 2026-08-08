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
