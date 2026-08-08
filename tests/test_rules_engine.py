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
