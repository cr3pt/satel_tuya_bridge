"""Testy dla bridge/config.py - wczytywanie config.yaml + scalanie z sensors.yaml."""
import pytest
import yaml

from bridge.config import BridgeConfig
from bridge.config_store import ConfigStore

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


def test_convenience_properties(config_dir):
    config = BridgeConfig.load(config_dir / "config.yaml")
    assert config.satel["host"] == "127.0.0.1"
    assert config.tuya == {"devices": {}, "zigbee_gateways": {}, "cloud_devices": {}}
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
        assert config.store.get("automations")["scenes"][0]["name"] == "powitanie"

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

    def test_custom_automations_file_path_used_only_for_one_time_migration(self, tmp_path):
        """Niestandardowa nazwa pliku (`automations_file`) miała sens tylko
        dla starych, plikowych instalacji - respektowana PRZY MIGRACJI (żeby
        znaleźć zawartość do zaimportowania), ale PO migracji to zwykły
        dokument "automations" w bazie, bez własnej ścieżki."""
        config_text = MINIMAL_CONFIG + "automations_file: custom_automations.yaml\n"
        (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
        (tmp_path / "custom_automations.yaml").write_text(
            "scenes:\n  - name: x\n    actions: [{type: satel_disarm}]\n", encoding="utf-8",
        )
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.raw["scenes"][0]["name"] == "x"
        assert config.store.get("automations")["scenes"][0]["name"] == "x"


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
  lampka_zigbee:
    id: sub1
    gateway: brama_salon
    cid: node1
    type: socket
zigbee_gateways:
  brama_salon:
    id: gw1
    ip: 192.168.1.60
    local_key: gwkey
    version: 3.4
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
        assert config.store.get("satel_fields")["partitions"] == {"Dom": 1, "Garaz": 2}

    def test_tuya_devices_file_overrides_inline_values(self, config_dir):
        (config_dir / "tuya_devices.yaml").write_text(TUYA_DEVICES_YAML, encoding="utf-8")
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["tuya"]["devices"]["gniazdko_salon"]["ip"] == "192.168.1.50"
        assert config.raw["tuya"]["devices"]["lampka_zigbee"] == {
            "id": "sub1", "gateway": "brama_salon", "cid": "node1", "type": "socket",
        }
        assert config.raw["tuya"]["zigbee_gateways"] == {
            "brama_salon": {"id": "gw1", "ip": "192.168.1.60", "local_key": "gwkey", "version": 3.4},
        }
        assert config.store.get("tuya_devices")["devices"]["gniazdko_salon"]["ip"] == "192.168.1.50"

    def test_missing_satel_fields_and_tuya_devices_files_default_to_empty(self, config_dir):
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["satel"]["monitored_zones"] == {}
        assert config.raw["satel"]["monitored_outputs"] == {}
        assert config.raw["satel"]["partitions"] == {}
        assert config.raw["satel"]["virtual_inputs"]["inputs"] == {}
        assert config.raw["satel"]["zone_types"] == {}
        assert config.raw["satel"]["output_types"] == {}
        assert config.raw["tuya"]["devices"] == {}
        assert config.raw["tuya"]["zigbee_gateways"] == {}

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

    def test_custom_satel_fields_and_tuya_devices_file_paths_used_only_for_migration(self, tmp_path):
        """Jak wyżej dla automations_file - `fields_file`/`devices_file`
        respektowane tylko przy jednorazowej migracji ze starej instalacji
        plikowej, żeby znaleźć zawartość do zaimportowania."""
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


class TestDatabaseMigration:
    """Konfiguracja żyje w config.db (bridge/config_store.py) - stare
    instalacje plikowe migrują się do niej automatycznie i jednorazowo,
    patrz docstring migrate_legacy_yaml_dir()."""

    def test_first_load_creates_db_and_renames_legacy_files(self, config_dir):
        assert not (config_dir / "config.db").exists()
        BridgeConfig.load(config_dir / "config.yaml")
        assert (config_dir / "config.db").exists()
        assert not (config_dir / "config.yaml").exists()
        assert (config_dir / "config.yaml.migrated").exists()
        assert (config_dir / "sensors.yaml.migrated").exists()

    def test_second_load_reads_db_directly_ignores_stale_legacy_content(self, config_dir):
        """Migracja jest jednorazowa - modyfikacja resztek starych plików
        (albo ich przywrócenie z backupu) PO migracji nie powinna niczego
        cofnąć, bo od tego momentu bridge w ogóle ich nie czyta."""
        BridgeConfig.load(config_dir / "config.yaml")
        # Symulujemy "przywrócony z backupu" stary plik z innym adresem -
        # gdyby migracja odpalała się ponownie, dostalibyśmy 9.9.9.9.
        (config_dir / "config.yaml").write_text(
            MINIMAL_CONFIG.replace('"127.0.0.1"', '"9.9.9.9"', 1), encoding="utf-8",
        )
        config = BridgeConfig.load(config_dir / "config.yaml")
        assert config.raw["satel"]["host"] == "127.0.0.1"

    def test_passing_db_path_directly_works_for_fresh_install(self, tmp_path):
        """Konwencja od tej wersji: kreator/panel tworzą config.db wprost
        (bez żadnego pośredniczącego pliku .yaml) - BridgeConfig.load musi
        to obsłużyć tak samo jak migrację ze starej instalacji."""
        store = ConfigStore(tmp_path / "config.db")
        store.set("config", {
            "satel": {"host": "1.2.3.4", "port": 7094, "integration_key": "x",
                      "virtual_inputs": {"port": 8888, "url_template": "http://x", "inputs": {}}},
            "tuya": {"devices": {}},
            "reguły_ruchu": [],
            "ebus": {"mqtt_host": "127.0.0.1"},
        })
        config = BridgeConfig.load(tmp_path / "config.db")
        assert config.raw["satel"]["host"] == "1.2.3.4"


VALID_CONFIG_YAML = """
satel:
  host: "1.2.3.4"
  port: 7094
  integration_key: "klucz"
  virtual_inputs:
    port: 8888
    url_template: "http://x"
    inputs: {}
tuya:
  devices: {}
reguły_ruchu: []
ebus:
  mqtt_host: "127.0.0.1"
"""


class TestImportYamlBundleValidated:
    """ConfigStore.import_yaml_bundle_validated() - używane przez import z
    archiwum .zip (dashboard/app.py: /api/config/import-zip). Podwójne
    sprawdzenie PRZED zapisem: (1) składnia YAML, (2) realny start procesu
    (BridgeConfig.load) na tymczasowej kopii bazy + jawne pola satela."""

    def test_valid_bundle_imports_successfully(self, tmp_path):
        store = ConfigStore(tmp_path / "config.db")
        imported = store.import_yaml_bundle_validated({"config.yaml": VALID_CONFIG_YAML})
        assert imported == ["config"]
        assert store.get("config")["satel"]["host"] == "1.2.3.4"

    def test_invalid_yaml_syntax_raises_and_writes_nothing(self, tmp_path):
        store = ConfigStore(tmp_path / "config.db")
        broken = "satel: [unclosed"
        with pytest.raises(ValueError, match="Błąd parsowania YAML"):
            store.import_yaml_bundle_validated({"config.yaml": broken})
        assert not store.has("config")

    def test_non_dict_yaml_raises(self, tmp_path):
        store = ConfigStore(tmp_path / "config.db")
        with pytest.raises(ValueError, match="nie zawiera słownika"):
            store.import_yaml_bundle_validated({"config.yaml": "- just\n- a\n- list\n"})

    def test_no_recognized_files_raises(self, tmp_path):
        store = ConfigStore(tmp_path / "config.db")
        with pytest.raises(ValueError, match="Archiwum nie zawiera"):
            store.import_yaml_bundle_validated({"random_file.yaml": "a: 1"})

    def test_missing_satel_section_raises_and_writes_nothing(self, tmp_path):
        store = ConfigStore(tmp_path / "config.db")
        store.set("config", {"marker": "pre-existing"})
        broken = "tuya:\n  devices: {}\nreguły_ruchu: []\nebus:\n  mqtt_host: x\n"
        with pytest.raises(ValueError, match="nie przejdzie startu procesu"):
            store.import_yaml_bundle_validated({"config.yaml": broken})
        assert store.get("config") == {"marker": "pre-existing"}  # nietknięte

    def test_satel_missing_required_fields_raises_and_writes_nothing(self, tmp_path):
        store = ConfigStore(tmp_path / "config.db")
        store.set("config", {"marker": "pre-existing"})
        # "satel" jest obecne (przechodzi _validate), ale brakuje host/port/
        # integration_key - to jest dokładnie luka, której _validate() nie
        # łapie, a którą RulesEngine.__init__ i tak wywali KeyError-em.
        broken = "satel: {}\ntuya:\n  devices: {}\nreguły_ruchu: []\nebus:\n  mqtt_host: x\n"
        with pytest.raises(ValueError, match="satel.host"):
            store.import_yaml_bundle_validated({"config.yaml": broken})
        assert store.get("config") == {"marker": "pre-existing"}

    def test_partial_import_merges_with_existing_valid_config(self, tmp_path):
        """Import samego sensors.yaml (bez config.yaml w archiwum) musi
        zwalidować się WZGLĘDEM już zapisanej, poprawnej reszty configu -
        nie powinien wymagać przesyłania WSZYSTKICH dokumentów naraz."""
        store = ConfigStore(tmp_path / "config.db")
        store.set("config", yaml.safe_load(VALID_CONFIG_YAML))
        sensors_yaml = "sensors:\n  - name: kociol_temp\n    label: Temp\n    mqtt_topic: x\n"
        imported = store.import_yaml_bundle_validated({"sensors.yaml": sensors_yaml})
        assert imported == ["sensors"]
        assert store.get("sensors")["sensors"][0]["name"] == "kociol_temp"
        assert store.get("config")["satel"]["host"] == "1.2.3.4"  # nietknięte

    def test_unknown_filenames_are_ignored(self, tmp_path):
        store = ConfigStore(tmp_path / "config.db")
        imported = store.import_yaml_bundle_validated({
            "config.yaml": VALID_CONFIG_YAML,
            "nieznany_plik.yaml": "cokolwiek: 1",
        })
        assert imported == ["config"]

    def test_local_key_with_special_characters_survives_migration_roundtrip(self, tmp_path):
        """Powód migracji na bazę: local_key Tuya (i inne sekrety) to
        dowolne znaki - JSON w SQLite nie ma niejawnego typowania YAML-a,
        na które podatna jest ręczna edycja pliku. Regresja dla tego
        konkretnego niepokoju."""
        import yaml

        tricky_key = 'aB3+/==xY7"quote\'s\\back\ttab'
        data = yaml.safe_load(MINIMAL_CONFIG)
        data["tuya"]["devices"] = {
            "x": {"id": "d1", "ip": "1.2.3.4", "local_key": tricky_key, "version": 3.4, "type": "socket"},
        }
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        config = BridgeConfig.load(tmp_path / "config.yaml")
        assert config.raw["tuya"]["devices"]["x"]["local_key"] == tricky_key
        # Ponowne wczytanie z już zmigrowanej bazy - to samo zachowanie.
        config2 = BridgeConfig.load(tmp_path / "config.db")
        assert config2.raw["tuya"]["devices"]["x"]["local_key"] == tricky_key
