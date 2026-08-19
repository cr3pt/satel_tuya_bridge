"""Testy integracyjne dla bridge/rules_engine.py - centralnego punktu
spinającego wszystkie reguły. Konstruujemy prawdziwy RulesEngine z
tymczasowej konfiguracji, ale podmieniamy jego kolaboratorów (tuya, satel,
scenes) na fałszywki z conftest.py, żeby nie dotykać sieci."""
import asyncio
import time

import pytest

from bridge.config import BridgeConfig
from bridge.rules_engine import RulesEngine

CONFIG_TEMPLATE = """
satel:
  host: "127.0.0.1"
  port: 7094
  integration_key: "x"
  virtual_inputs:
    port: 8888
    url_template: "http://{{host}}:{{port}}/x"
    inputs:
      test_input: {{number: 1, key: "k"}}
  partitions:
    Dom: 1
    Garaz: 2
tuya:
  devices:
    gniazdko_salon: {{id: "d1", ip: "1.2.3.4", local_key: "k", type: "socket"}}
reguły_ruchu:
  - source: satel_zone
    zone_number: 12
    target_device: gniazdko_salon
    duration_seconds: 60

  - source: satel_zone
    zone_number: 13
    target_device: gniazdko_salon
    duration_seconds: 60
    time_window: {{start: "08:00", end: "09:00"}}

  - source: satel_zone
    zone_number: 14
    target_device: gniazdko_salon
    duration_seconds: 60
    escalation: {{threshold_count: 3, window_seconds: 5, escalated_scene: "wykryto_intruza"}}

reguły_swiatlo_do_satela:
  - source_device: gniazdko_salon
    virtual_input: test_input

reguły_kamera_alarm:
  - source: unifi_protect
    camera_id: "cam1"
    action: virtual_input
    virtual_input: test_input
    cooldown_seconds: 5

  - source: unifi_protect
    camera_id: "cam2"
    scene: "wykryto_intruza"

scenes:
  - name: "wykryto_intruza"
    actions:
      - {{type: tuya_on, device: gniazdko_salon}}

ebus:
  mqtt_host: "127.0.0.1"
  sensors_file: "sensors.yaml"

dashboard:
  db_path: "{db_path}"
"""

SENSORS_YAML = """
sensors:
  - name: kociol_temp
    label: "Temperatura"
    mqtt_topic: "ebusd/x/y"
    targets:
      history: true
      mqtt_publish: false
      tuya_virtual_device: false
      tuya_dp: ""
"""


@pytest.fixture
async def engine(tmp_path, fake_tuya, fake_satel, fake_virtual_inputs):
    (tmp_path / "sensors.yaml").write_text(SENSORS_YAML, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        CONFIG_TEMPLATE.format(db_path=str(tmp_path / "history.db")), encoding="utf-8",
    )
    config = BridgeConfig.load(tmp_path / "config.yaml")
    rules_engine = RulesEngine(config)
    # Podmieniamy prawdziwych klientów na fałszywki - testujemy LOGIKĘ
    # routingu/warunków, nie faktyczną komunikację sieciową.
    rules_engine.tuya = fake_tuya
    rules_engine.satel = fake_satel
    rules_engine.satel_virtual_inputs = fake_virtual_inputs
    rules_engine.scenes.tuya = fake_tuya
    rules_engine.scenes.satel = fake_satel
    rules_engine.scenes.satel_virtual_inputs = fake_virtual_inputs
    return rules_engine


class TestMotionRules:
    async def test_basic_motion_turns_on_device(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 12)
        await asyncio.sleep(0)  # handle_motion planuje turn_on_for_duration jako Task w tle - oddaj sterowanie
        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_unmatched_zone_does_nothing(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 999)
        assert fake_tuya.calls == []

    async def test_time_window_outside_blocks_rule(self, engine, fake_tuya, monkeypatch):
        import datetime as dt
        import bridge.rules_engine as re_module

        class FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 1, 15, 20, 0)  # poza oknem 08:00-09:00

        monkeypatch.setattr(re_module, "datetime", FixedDatetime)
        await engine.handle_motion("satel_zone", 13)
        assert fake_tuya.calls == []

    async def test_time_window_inside_allows_rule(self, engine, fake_tuya, monkeypatch):
        import datetime as dt
        import bridge.rules_engine as re_module

        class FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 1, 15, 8, 30)  # wewnątrz okna 08:00-09:00

        monkeypatch.setattr(re_module, "datetime", FixedDatetime)
        await engine.handle_motion("satel_zone", 13)
        await asyncio.sleep(0)
        assert any(c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_escalation_triggers_scene_after_threshold(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        fake_tuya.calls.clear()
        await engine.handle_motion("satel_zone", 14)  # 3. naruszenie -> eskalacja
        # scena "wykryto_intruza" wywołuje tuya_on na gniazdko_salon
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls

    async def test_escalation_logs_event(self, engine):
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        events = engine.history.get_events("escalation", 0, time.time() + 1)
        assert len(events) == 1


class TestSatelZoneOutputCallbackShape:
    """Regresja dla zgłoszonego z prawdziwej centrali błędu: strefy/wyjścia
    na głównej stronie nigdy nie zmieniały stanu (zamrożone na wartości z
    zasiania), a zamiast tego pojawiał się jeden fałszywy wpis z dziwnym
    numerem. Przyczyna: satel_integra woła zarejestrowany callback z
    PEŁNYM obiektem `{"zones": {numer: 0/1, ...}}` / `{"outputs": {...}}`
    (patrz AsyncSatel._zone_violated()/_output_changed() w
    satel_integra/satel_integra.py), NIE z samym wewnętrznym słownikiem -
    _on_zone_change/_on_output_change w rules_engine.py (zarejestrowane w
    start()) muszą to rozpakować przez `.get("zones"/"outputs", {})`."""

    async def test_zone_change_updates_correct_zone_not_bogus_key(self, engine, fake_satel):
        fake_satel.monitored_zones = {"Czujka salon": 12}
        await engine.start()
        await fake_satel._zone_cb({"zones": {12: 1}})

        statuses = {s["key"]: s["value"] for s in engine.history.get_status("satel_input")}
        assert statuses["satel_input:12"] == "naruszone"
        assert "satel_input:zones" not in statuses

    async def test_output_change_updates_correct_output_not_bogus_key(self, engine, fake_satel):
        fake_satel.monitored_outputs = {"Brama": 9}
        await engine.start()
        await fake_satel._output_cb({"outputs": {9: 1}})

        statuses = {s["key"]: s["value"] for s in engine.history.get_status("satel_output")}
        assert statuses["satel_output:9"] == "włączone"
        assert "satel_output:outputs" not in statuses

    async def test_zone_violation_still_triggers_motion_rule(self, engine, fake_satel, fake_tuya):
        """_on_zone_change wywołuje też handle_motion() dla reguł ruchu -
        musi to nadal działać po rozpakowaniu "zones"."""
        fake_satel.monitored_zones = {"Czujka": 12}
        await engine.start()
        await fake_satel._zone_cb({"zones": {12: 1}})
        await asyncio.sleep(0)
        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)


class TestLightToSatelRule:
    async def test_light_on_triggers_virtual_input(self, engine, fake_virtual_inputs):
        await engine.handle_light_state_change("gniazdko_salon", True)
        assert ("trigger", "test_input") in fake_virtual_inputs.calls

    async def test_light_off_does_not_trigger(self, engine, fake_virtual_inputs):
        await engine.handle_light_state_change("gniazdko_salon", False)
        assert fake_virtual_inputs.calls == []


class TestCameraRules:
    async def test_camera_motion_triggers_action(self, engine, fake_virtual_inputs):
        await engine.handle_camera_motion("unifi_protect", "cam1")
        assert ("trigger", "test_input") in fake_virtual_inputs.calls

    async def test_camera_cooldown_suppresses_repeat(self, engine, fake_virtual_inputs):
        await engine.handle_camera_motion("unifi_protect", "cam1")
        fake_virtual_inputs.calls.clear()
        await engine.handle_camera_motion("unifi_protect", "cam1")  # w oknie karencji
        assert fake_virtual_inputs.calls == []

    async def test_camera_scene_action(self, engine, fake_tuya):
        await engine.handle_camera_motion("unifi_protect", "cam2")
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls

    async def test_unmatched_camera_does_nothing(self, engine, fake_virtual_inputs, fake_tuya):
        await engine.handle_camera_motion("unifi_protect", "nieznana_kamera")
        assert fake_virtual_inputs.calls == []
        assert fake_tuya.calls == []


class TestBoilerValueRouting:
    def test_history_only_sensor_not_published(self, engine):
        """Czujnik kociol_temp w sensors.yaml ma mqtt_publish=false,
        tuya_virtual_device=false - wartość powinna trafić TYLKO do historii."""
        sensor_cfg = {
            "name": "kociol_temp", "label": "Temperatura",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False},
        }
        engine.handle_boiler_value(sensor_cfg, 72.5)
        history_data = engine.history.get_history("kociol_temp", 0, time.time() + 1)
        assert len(history_data) == 1
        assert history_data[0]["v"] == 72.5
        # brak publisherów mqtt/tuya (żaden sensor ich nie potrzebował przy budowie)
        assert "mqtt" not in engine.boiler_publishers

    def test_text_value_updates_status_but_not_readings(self, engine):
        sensor_cfg = {
            "name": "kociol_status", "label": "Status",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False},
        }
        engine.handle_boiler_value(sensor_cfg, "heating")
        assert engine.history.get_history("kociol_status", 0, time.time() + 1) == []
        statuses = engine.history.get_status("boiler")
        assert any(s["value"] == "heating" for s in statuses)


class TestExecuteCommandRestartProcess:
    """_execute_command('restart_process') - patrz api_system_restart w
    dashboard/app.py: pełny restart obu kontenerów (Docker Compose ma
    restart: unless-stopped dla obu serwisów), w odróżnieniu od
    'reload_config' (patrz TestReloadConfig niżej), który celowo NIE dotyka
    żywych połączeń sieciowych. Monkeypatchuje os._exit - inaczej test
    zabiłby własny proces."""

    async def test_marks_command_done_and_schedules_exit(self, engine, monkeypatch):
        import bridge.rules_engine as re_module

        exit_calls = []
        monkeypatch.setattr(re_module.os, "_exit", lambda code: exit_calls.append(code))

        cmd_id = engine.history.enqueue_command("restart_process", {})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert engine.history.get_command(cmd_id)["status"] == "done"
        await asyncio.sleep(0.6)  # dłużej niż 0.5s opóźnienie przed os._exit(0)
        assert exit_calls == [0]


class TestBuildTuyaController:
    """_build_tuya_controller() - helper współdzielony przez
    RulesEngine.__init__ i reload_config(), buduje kontroler Tuya
    (lokalny + fallback chmurowy, patrz bridge/tuya_dispatch.py). Testowane
    bezpośrednio (nie przez fixture `engine`, która podmienia engine.tuya
    na fałszywkę zaraz po konstrukcji)."""

    def test_returns_dispatcher_wrapping_local_and_cloud(self):
        from bridge.rules_engine import _build_tuya_controller
        from bridge.tuya_dispatch import TuyaController
        from bridge.tuya_cloud import TuyaCloudController
        from bridge.tuya_local import TuyaLocalController

        tuya_cfg = {
            "devices": {"gniazdko": {"id": "d1", "ip": "1.2.3.4", "local_key": "k"}},
            "cloud_devices": {"lampa": {"id": "c1", "switch_code": "switch_1"}},
            "cloud": {"api_region": "eu", "access_id": "aid", "access_key": "akey"},
        }
        controller = _build_tuya_controller(tuya_cfg)
        assert isinstance(controller, TuyaController)
        assert isinstance(controller.local, TuyaLocalController)
        assert isinstance(controller.cloud, TuyaCloudController)
        assert "gniazdko" in controller.local.devices_cfg
        assert "lampa" in controller.cloud.devices_cfg
        assert controller.cloud.credentials == {"api_region": "eu", "access_id": "aid", "access_key": "akey"}

    def test_works_with_missing_cloud_devices_and_credentials(self):
        """Konfiguracje sprzed tej funkcji (bez tuya.cloud_devices/tuya.cloud)
        nie mogą się wywalić - patrz bridge/config.py (setdefault do {})."""
        from bridge.rules_engine import _build_tuya_controller

        controller = _build_tuya_controller({"devices": {}})
        assert controller.cloud.devices_cfg == {}
        assert controller.local.devices_cfg == {}


class TestReloadConfig:
    """RulesEngine.reload_config() - przeładowanie 'lekkiej' konfiguracji
    (czujniki eBUS/pola Satela/urządzenia Tuya/automatyzacje/alarmy) BEZ
    restartu procesu - wywoływane przez komendę 'reload_config' w kolejce
    (patrz dashboard/app.py: _enqueue_bridge_reload, dokładane po każdym
    zapisie z panelu). Fixture `engine` nigdy nie wywołuje start(), więc
    engine.ebus_listener zostaje None - dokładnie tak, jak w testach
    powyżej w tym pliku (testujemy logikę, nie żywe połączenia sieciowe)."""

    async def test_ebus_listener_none_does_not_crash_reload(self, engine):
        assert engine.ebus_listener is None
        await engine.reload_config()  # nie powinno rzucić wyjątku

    async def test_config_object_is_replaced(self, engine, tmp_path):
        old_config = engine.config
        await engine.reload_config()
        assert engine.config is not old_config

    async def test_picks_up_new_tuya_device_without_restart(self, engine, tmp_path):
        engine.config.store.set("tuya_devices", {
            "devices": {"nowe_urzadzenie": {"id": "dNEW", "ip": "1.2.3.9", "local_key": "k9", "type": "socket"}},
        })
        await engine.reload_config()
        assert "nowe_urzadzenie" in engine.tuya.devices_cfg

    async def test_picks_up_new_cloud_device_without_restart(self, engine, tmp_path):
        engine.config.store.set("tuya_devices", {
            "cloud_devices": {"lampa_taras": {"id": "cNEW", "category": "kg", "switch_code": "switch_1"}},
        })
        await engine.reload_config()
        assert "lampa_taras" in engine.tuya.cloud.devices_cfg
        assert "lampa_taras" in engine.tuya.devices_cfg  # widoczne też w sumie (dispatcher)

    async def test_local_device_takes_precedence_over_cloud_after_local_configured(self, engine, tmp_path):
        """Dokładnie scenariusz ze zgłoszenia: urządzenie zaczyna jako
        sterowane przez chmurę, a potem użytkownik konfiguruje je lokalnie
        pod TĄ SAMĄ nazwą - lokalne musi "wygrać"."""
        engine.config.store.set("tuya_devices", {
            "devices": {"gniazdko_hybryda": {"id": "dLOCAL", "ip": "1.2.3.8", "local_key": "klocal", "type": "socket"}},
            "cloud_devices": {"gniazdko_hybryda": {"id": "cCLOUD", "category": "kg", "switch_code": "switch_1"}},
        })
        await engine.reload_config()
        assert engine.tuya._resolve("gniazdko_hybryda") is engine.tuya.local

    async def test_picks_up_new_satel_zone_without_restart(self, engine, tmp_path):
        engine.config.store.set("satel_fields", {
            "monitored_zones": {"Nowe_wejscie": 99},
            "monitored_outputs": {},
            "partitions": {"Dom": 1},
            "virtual_inputs": {},
        })
        await engine.reload_config()
        assert engine.satel.monitored_zones == {"Nowe_wejscie": 99}

    async def test_satel_object_itself_is_not_replaced(self, engine, tmp_path):
        """Kluczowe dla bezpieczeństwa reloadu: NIE tworzymy nowego
        SatelIntegraClient (to zerwałoby żywe połączenie TCP) - tylko
        mutujemy monitored_zones/monitored_outputs na istniejącym obiekcie."""
        old_satel = engine.satel
        await engine.reload_config()
        assert engine.satel is old_satel

    async def test_reload_config_updates_satel_poll_interval_live(self, engine, tmp_path, fake_satel):
        """Interwał cyklicznego dopytywania Satela (zakładka 🧩 Wtyczki)
        MUSI zadziałać bez restartu - w odróżnieniu od host/portu/klucza,
        który nadal go wymaga (patrz satel_client.py:
        _periodic_status_sync, które czyta ten atrybut na bieżąco)."""
        raw = engine.config.store.get("config")
        raw["satel"]["status_poll_interval_seconds"] = 5
        engine.config.store.set("config", raw)

        await engine.reload_config()
        assert engine.satel.status_poll_interval_seconds == 5

    async def test_reload_config_updates_tuya_poll_interval_live(self, engine, tmp_path):
        """Analogicznie dla Tuya - _tuya_status_poll_loop() czyta
        self.config.tuya na bieżąco przy każdym obrocie, więc sama podmiana
        self.config (już robiona przez reload_config()) wystarcza."""
        raw = engine.config.store.get("config")
        raw["tuya"]["status_poll_interval_seconds"] = 7
        engine.config.store.set("config", raw)

        await engine.reload_config()
        assert engine.config.tuya.get("status_poll_interval_seconds") == 7

    async def test_picks_up_new_scene_without_restart(self, engine, tmp_path):
        engine.config.store.set("automations", {
            "scenes": [{"name": "nowa_scena", "actions": [{"type": "tuya_on", "device": "gniazdko_salon"}]}],
            "reguły_ruchu": [],
            "presence_simulation": {"enabled": False},
        })
        await engine.reload_config()
        assert engine.scenes.exists("nowa_scena")

    async def test_presence_simulation_object_identity_preserved(self, engine, tmp_path):
        """presence_simulation NIE jest podmieniane (tylko .cfg mutowane w
        miejscu) - jego run_forever() mogło już być uruchomione jako
        zadanie w tle przywiązane do TEGO obiektu (patrz reload_config)."""
        old_presence = engine.presence_simulation
        engine.config.store.set("automations", {
            "scenes": [], "reguły_ruchu": [],
            "presence_simulation": {
                "enabled": True, "devices": ["gniazdko_salon"],
                "min_interval_minutes": 5, "max_interval_minutes": 10,
            },
        })
        await engine.reload_config()
        assert engine.presence_simulation is old_presence
        assert engine.presence_simulation.cfg["enabled"] is True
        assert engine.presence_simulation.cfg["devices"] == ["gniazdko_salon"]

    async def test_picks_up_new_alarm_rule_without_restart(self, engine, tmp_path):
        raw = engine.config.store.get("config")
        raw["alarm_rules"] = [{"name": "test", "sensor": "kociol_temp", "condition": "above", "threshold": 90}]
        engine.config.store.set("config", raw)
        await engine.reload_config()
        assert len(engine.alarms.rules) == 1

    async def test_ebus_listener_update_sensors_called_when_present(self, engine, tmp_path):
        calls = []

        class FakeEbusListener:
            def update_sensors(self, cfg):
                calls.append(cfg)

        engine.ebus_listener = FakeEbusListener()
        await engine.reload_config()
        assert len(calls) == 1
        assert "sensors" in calls[0]


class TestTuyaStatusPollLoop:
    """Regresja dla zgłoszonego z prawdziwego wdrożenia błędu:
    `KeyError: 'T & H Sensor'` w _tuya_status_poll_loop. engine.tuya.
    devices_cfg (odpytywane przez get_all_statuses()) zawiera teraz też
    urządzenia sterowane przez Tuya Cloud (patrz bridge/tuya_dispatch.py) -
    ale `config.tuya["devices"]` to WYŁĄCZNIE urządzenia lokalne. Kod
    sprawdzający ściemniacz robił surowy dostęp przez [], co wywalało
    KeyError dla dowolnego urządzenia chmurowego (tu: czujnik temperatury/
    wilgotności) i PRZERYWAŁO resztę pętli for dla WSZYSTKICH pozostałych
    urządzeń w danym cyklu odpytywania."""

    async def test_cloud_only_device_does_not_crash_loop_for_other_devices(self, engine, fake_tuya):
        # Kolejność ma znaczenie - urządzenie chmurowe MUSI być przetworzone
        # PRZED "gniazdko_salon", żeby odtworzyć zgłoszony błąd (przerwana
        # pętla for nigdy nie docierała do kolejnych urządzeń).
        fake_tuya.devices_cfg = {
            "T & H Sensor": {"id": "cloud1"},  # chmurowe - BRAK wpisu w config.tuya["devices"]
            "gniazdko_salon": {"id": "dev1", "ip": "192.168.1.50", "local_key": "key1"},
        }
        try:
            await asyncio.wait_for(engine._tuya_status_poll_loop(), timeout=0.05)
        except asyncio.TimeoutError:
            pass  # oczekiwane - pętla działa w nieskończoność

        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert "tuya:gniazdko_salon" in statuses  # NIE zostało pominięte przez wcześniejszy crash
        assert "tuya:T & H Sensor" in statuses


class TestStateMqttPublisher:
    """self.state_mqtt - publikacja stanu Satel/Tuya do MQTT pod
    konfigurowalnym tematem PER INTEGRACJA, niezależnie od
    home_assistant.enabled (patrz bridge/ha_mqtt.py:HomeAssistantMqttBridge,
    użyty tu w trybie 'tylko publikacja', entities=[])."""

    def _build_engine(self, tmp_path, extra_yaml=""):
        (tmp_path / "sensors.yaml").write_text("sensors: []\n", encoding="utf-8")
        (tmp_path / "config.yaml").write_text(
            CONFIG_TEMPLATE.format(db_path=str(tmp_path / "history.db")) + extra_yaml, encoding="utf-8",
        )
        config = BridgeConfig.load(tmp_path / "config.yaml")
        return RulesEngine(config)

    async def test_created_by_default_when_ebus_mqtt_configured(self, tmp_path):
        engine = self._build_engine(tmp_path)
        assert engine.state_mqtt is not None
        assert engine._satel_mqtt_topic == "satel"
        assert engine._tuya_mqtt_topic == "tuya"

    async def test_can_be_disabled_via_config(self, tmp_path):
        engine = self._build_engine(tmp_path, "\nmqtt_publish:\n  enabled: false\n")
        assert engine.state_mqtt is None

    async def test_custom_topics_from_config(self, tmp_path):
        engine = self._build_engine(
            tmp_path,
            "\nmqtt_publish:\n  enabled: true\n  topics:\n    satel: alarm\n    tuya: smart_home\n",
        )
        assert engine.state_mqtt is not None
        assert engine._satel_mqtt_topic == "alarm"
        assert engine._tuya_mqtt_topic == "smart_home"

    async def test_independent_of_home_assistant_enabled(self, tmp_path):
        """Kluczowa różnica względem ha_mqtt: state_mqtt działa NAWET gdy
        home_assistant.enabled nie jest ustawione (albo false) - to był
        dokładnie zgłoszony brak ('stany urządzeń powinny odkładać się też
        w MQTT', niezależnie od Home Assistant)."""
        engine = self._build_engine(tmp_path)
        assert engine.ha_mqtt is None
        assert engine.state_mqtt is not None


class TestAlarmStatusCallback:
    """Regresja dla zgłoszonego z prawdziwej centrali błędu: '_on_alarm_status_change()
    missing 1 required positional argument: partition_states'. satel_integra
    woła callback zarejestrowany przez register_alarm_status_callback BEZ
    ŻADNYCH argumentów (patrz satel_client.py:register_alarm_status_callback
    i AsyncSatel._armed()/_read_data()/_update_status()) - to tylko
    powiadomienie "coś się zmieniło", nie niesie samego stanu.
    RulesEngine._handle_alarm_status_change() musi więc dać się wywołać BEZ
    ARGUMENTÓW i samodzielnie odczytać stan z self.satel.armed_partition_numbers
    (patrz CONFIG_TEMPLATE wyżej - satel.partitions: {{Dom: 1, Garaz: 2}})."""

    async def test_callback_accepts_no_arguments(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._handle_alarm_status_change()  # nie powinno rzucić TypeError

    async def test_updates_status_for_all_configured_partitions(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._handle_alarm_status_change()
        statuses = {s["label"]: s["value"] for s in engine.history.get_status("satel_partition")}
        assert statuses["Dom"] == "uzbrojona"
        assert statuses["Garaz"] == "rozbrojona"

    async def test_no_armed_partitions_marks_all_disarmed(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = set()
        await engine._handle_alarm_status_change()
        statuses = {s["label"]: s["value"] for s in engine.history.get_status("satel_partition")}
        assert statuses["Dom"] == "rozbrojona"
        assert statuses["Garaz"] == "rozbrojona"


class TestAlarmStatusDebounce:
    """_on_alarm_status_change() - to on (nie _handle_alarm_status_change
    bezpośrednio) jest rejestrowany jako właściwy callback (patrz start()
    w rules_engine.py). Centrala wysyła stan każdej strefy jako osobną
    ramkę, więc jedna zmiana potrafi wywołać kilka powiadomień pod rząd -
    _on_alarm_status_change() odbija (debounce'uje) je w jedno odświeżenie,
    tak samo jak integracja Home Assistant (PARTITION_UPDATE_DEBOUNCE_DELAY
    w homeassistant/components/satel_integra/coordinator.py)."""

    async def test_does_not_apply_immediately(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._on_alarm_status_change()
        # Debounce w toku (0.15s) - _handle_alarm_status_change() jeszcze
        # nie zdążyło nic zapisać do historii.
        assert engine.history.get_status("satel_partition") == []

    async def test_applies_after_debounce_delay(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._on_alarm_status_change()
        await asyncio.sleep(0.25)  # dłużej niż debounce (0.15s)
        statuses = {s["label"]: s["value"] for s in engine.history.get_status("satel_partition")}
        assert statuses["Dom"] == "uzbrojona"

    async def test_burst_of_calls_applies_only_the_final_state(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._on_alarm_status_change()
        fake_satel.armed_partition_numbers = set()
        await engine._on_alarm_status_change()
        fake_satel.armed_partition_numbers = {1, 2}
        await engine._on_alarm_status_change()

        await asyncio.sleep(0.25)

        statuses = {s["label"]: s["value"] for s in engine.history.get_status("satel_partition")}
        assert statuses["Dom"] == "uzbrojona"
        assert statuses["Garaz"] == "uzbrojona"
