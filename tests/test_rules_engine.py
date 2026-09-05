import asyncio
import contextlib
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
    inputs:
      test_input: 1
  partitions:
    Dom: 1
    Garaz: 2
tuya:
  devices:
    gniazdko_salon: {{id: "d1", ip: "1.2.3.4", local_key: "k", type: "socket"}}
reguły_ruchu:
  - source: satel_zone
    zone_number: 12
    name: "regula_zone_12"
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

  - source: satel_zone
    zone_number: 15
    trigger_on: restored
    target_device: gniazdko_salon
    action: turn_off

  - source: satel_zone
    zone_number: 16
    target_device: gniazdko_salon
    dimmer_level_percent: 40

  - source: satel_zone
    zone_number: 17
    name: "regula_wylaczona"
    enabled: false
    target_device: gniazdko_salon
    duration_seconds: 30

  - source: satel_zone
    zone_number: 18
    name: "regula_lancuch_do_wylaczonej"
    target_device: gniazdko_salon
    duration_seconds: 30
    then_trigger_rule: "regula_wylaczona"

  - source: satel_zone
    zone_number: 19
    target_system: satel_output
    target_device: "Brama"
    duration_seconds: 0.01

  - source: satel_zone
    zone_number: 20
    target_system: satel_output
    target_device: "Brama"
    action: turn_off

  - source: satel_zone
    zone_number: 21
    target_system: satel_output
    target_device: "Brama"

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

  - name: "scena_wylaczona"
    enabled: false
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
async def engine(tmp_path, fake_tuya, fake_satel):
    (tmp_path / "sensors.yaml").write_text(SENSORS_YAML, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        CONFIG_TEMPLATE.format(db_path=str(tmp_path / "history.db")), encoding="utf-8",
    )
    config = BridgeConfig.load(tmp_path / "config.yaml")
    rules_engine = RulesEngine(config)
    rules_engine.tuya = fake_tuya
    rules_engine.satel = fake_satel
    rules_engine.scenes.tuya = fake_tuya
    rules_engine.scenes.satel = fake_satel
    return rules_engine


def _repatch_fakes(engine, fake_tuya=None, fake_satel=None, fake_virtual_inputs=None):
    if fake_tuya is not None:
        engine.tuya = fake_tuya
        engine.scenes.tuya = fake_tuya
    if fake_satel is not None:
        engine.satel = fake_satel
        engine.scenes.satel = fake_satel
    if fake_virtual_inputs is not None:
        engine.satel_virtual_inputs = fake_virtual_inputs
        engine.scenes.satel_virtual_inputs = fake_virtual_inputs


class TestMotionRules:
    async def test_basic_motion_turns_on_device(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 12)
        await asyncio.sleep(0)
        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_unmatched_zone_does_nothing(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 999)
        assert fake_tuya.calls == []

    async def test_default_violated_rule_ignores_restored_trigger(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 12, trigger="restored")
        assert fake_tuya.calls == []

    async def test_turn_off_action_fires_on_restored_trigger(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 15, trigger="restored")
        assert ("off", "gniazdko_salon") in fake_tuya.calls

    async def test_turn_off_rule_does_not_fire_on_violated_trigger(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 15, trigger="violated")
        assert fake_tuya.calls == []

    async def test_turn_off_action_replaces_pending_on_timer(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 12)
        await asyncio.sleep(0)
        assert "gniazdko_salon" in engine._active_timers

        await engine.handle_motion("satel_zone", 15, trigger="restored")
        assert ("off", "gniazdko_salon") in fake_tuya.calls

    async def test_blank_duration_turns_on_immediately_on_violated(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 16, trigger="violated")
        assert ("on", "gniazdko_salon", 40) in fake_tuya.calls
        assert not any(c[0] == "on_for" for c in fake_tuya.calls)

    async def test_blank_duration_turns_off_on_restored(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 16, trigger="restored")
        assert ("off", "gniazdko_salon") in fake_tuya.calls

    async def test_duration_zero_is_equivalent_to_blank(self, engine, fake_tuya):
        engine.config.store.set("automations", {
            "scenes": [], "presence_simulation": {"enabled": False},
            "reguły_ruchu": [{
                "source": "satel_zone", "zone_number": 16,
                "target_device": "gniazdko_salon", "duration_seconds": 0,
            }],
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya=fake_tuya)

        await engine.handle_motion("satel_zone", 16, trigger="violated")
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls
        assert not any(c[0] == "on_for" for c in fake_tuya.calls)

        await engine.handle_motion("satel_zone", 16, trigger="restored")
        assert ("off", "gniazdko_salon") in fake_tuya.calls

    async def test_blank_duration_rule_reacts_to_both_triggers_regardless_of_trigger_on(
        self, engine, fake_tuya,
    ):
        await engine.handle_motion("satel_zone", 16, trigger="violated")
        await engine.handle_motion("satel_zone", 16, trigger="restored")
        assert ("on", "gniazdko_salon", 40) in fake_tuya.calls
        assert ("off", "gniazdko_salon") in fake_tuya.calls

    async def test_disabled_rule_does_not_fire(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 17)
        assert fake_tuya.calls == []

    async def test_chain_trigger_skips_disabled_target_rule(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 18)
        await asyncio.sleep(0)
        assert fake_tuya.calls == [("on_for", "gniazdko_salon", 30, None)]

    async def test_time_window_outside_blocks_rule(self, engine, fake_tuya, monkeypatch):
        import datetime as dt
        import bridge.rules_engine as re_module

        class FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 1, 15, 20, 0)

        monkeypatch.setattr(re_module, "datetime", FixedDatetime)
        await engine.handle_motion("satel_zone", 13)
        assert fake_tuya.calls == []

    async def test_time_window_inside_allows_rule(self, engine, fake_tuya, monkeypatch):
        import datetime as dt
        import bridge.rules_engine as re_module

        class FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 1, 15, 8, 30)

        monkeypatch.setattr(re_module, "datetime", FixedDatetime)
        await engine.handle_motion("satel_zone", 13)
        await asyncio.sleep(0)
        assert any(c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_escalation_triggers_scene_after_threshold(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        fake_tuya.calls.clear()
        await engine.handle_motion("satel_zone", 14)
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls

    async def test_escalation_logs_event(self, engine):
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        events = engine.history.get_events("escalation", 0, time.time() + 1)
        assert len(events) == 1


class TestVirtualInputMotionRuleAction:

    async def _reload_with_rule(self, engine, fake_tuya, fake_satel, rule):
        engine.config.store.set("automations", {
            "scenes": [], "presence_simulation": {"enabled": False},
            "reguły_ruchu": [rule],
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya=fake_tuya, fake_satel=fake_satel)
        fake_satel.virtual_inputs = {"motion_hall_camera": 17}

    async def test_no_duration_fires_on_violated(self, engine, fake_tuya, fake_satel):
        await self._reload_with_rule(engine, fake_tuya, fake_satel, {
            "source": "satel_zone", "zone_number": 12, "virtual_input": "motion_hall_camera",
        })
        await engine.handle_motion("satel_zone", 12, trigger="violated")
        assert fake_satel.calls == [("output", 17, True)]
        assert fake_tuya.calls == []

    async def test_no_duration_toggles_off_on_restored(self, engine, fake_tuya, fake_satel):
        await self._reload_with_rule(engine, fake_tuya, fake_satel, {
            "source": "satel_zone", "zone_number": 12, "virtual_input": "motion_hall_camera",
        })
        await engine.handle_motion("satel_zone", 12, trigger="violated")
        await engine.handle_motion("satel_zone", 12, trigger="restored")
        assert fake_satel.calls == [("output", 17, True), ("output", 17, False)]

    async def test_duration_zero_behaves_like_blank(self, engine, fake_tuya, fake_satel):
        await self._reload_with_rule(engine, fake_tuya, fake_satel, {
            "source": "satel_zone", "zone_number": 12, "virtual_input": "motion_hall_camera",
            "duration_seconds": 0,
        })
        await engine.handle_motion("satel_zone", 12, trigger="violated")
        await engine.handle_motion("satel_zone", 12, trigger="restored")
        assert fake_satel.calls == [("output", 17, True), ("output", 17, False)]

    async def test_explicit_duration_turns_on_then_off_after_timeout(self, engine, fake_tuya, fake_satel):
        await self._reload_with_rule(engine, fake_tuya, fake_satel, {
            "source": "satel_zone", "zone_number": 12, "virtual_input": "motion_hall_camera",
            "duration_seconds": 0.02,
        })
        await engine.handle_motion("satel_zone", 12, trigger="restored")
        await asyncio.sleep(0)
        assert fake_satel.calls == []

        await engine.handle_motion("satel_zone", 12, trigger="violated")
        await asyncio.sleep(0)
        assert fake_satel.calls == [("output", 17, True)]
        await asyncio.sleep(0.05)
        assert fake_satel.calls == [("output", 17, True), ("output", 17, False)]

    async def test_works_for_device_state_source(self, engine, fake_tuya, fake_satel):
        await self._reload_with_rule(engine, fake_tuya, fake_satel, {
            "source": "tuya_state", "source_device": "gniazdko_salon", "trigger_value": "włączone",
            "virtual_input": "motion_hall_camera",
        })
        await engine.handle_device_state("tuya_state", "gniazdko_salon", "włączone")
        assert ("output", 17, True) in fake_satel.calls

    async def test_unknown_virtual_input_name_is_skipped_without_crash(self, engine, fake_tuya, fake_satel):
        engine.config.store.set("automations", {
            "scenes": [], "presence_simulation": {"enabled": False},
            "reguły_ruchu": [{
                "source": "satel_zone", "zone_number": 12, "virtual_input": "nieznane_wejscie",
            }],
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya=fake_tuya, fake_satel=fake_satel)
        fake_satel.virtual_inputs = {}
        await engine.handle_motion("satel_zone", 12, trigger="violated")
        assert fake_satel.calls == []


class TestVirtualInputHttpMode:

    async def _reload_with_rule(self, engine, fake_tuya, fake_satel, fake_virtual_inputs, rule):
        engine.config.store.set("automations", {
            "scenes": [], "presence_simulation": {"enabled": False},
            "reguły_ruchu": [rule],
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya=fake_tuya, fake_satel=fake_satel, fake_virtual_inputs=fake_virtual_inputs)
        engine._vi_mode = "http"

    async def test_no_duration_fires_trigger_twice(self, engine, fake_tuya, fake_satel, fake_virtual_inputs):
        await self._reload_with_rule(engine, fake_tuya, fake_satel, fake_virtual_inputs, {
            "source": "satel_zone", "zone_number": 12, "virtual_input": "motion_hall_camera",
        })
        await engine.handle_motion("satel_zone", 12, trigger="violated")
        await engine.handle_motion("satel_zone", 12, trigger="restored")
        assert fake_virtual_inputs.calls == [
            ("trigger", "motion_hall_camera"), ("trigger", "motion_hall_camera"),
        ]
        assert fake_satel.calls == []

    async def test_explicit_duration_fires_once_now_and_once_after_timeout(
        self, engine, fake_tuya, fake_satel, fake_virtual_inputs,
    ):
        await self._reload_with_rule(engine, fake_tuya, fake_satel, fake_virtual_inputs, {
            "source": "satel_zone", "zone_number": 12, "virtual_input": "motion_hall_camera",
            "duration_seconds": 0.02,
        })
        await engine.handle_motion("satel_zone", 12, trigger="violated")
        await asyncio.sleep(0)
        assert fake_virtual_inputs.calls == [("trigger", "motion_hall_camera")]
        await asyncio.sleep(0.05)
        assert fake_virtual_inputs.calls == [
            ("trigger", "motion_hall_camera"), ("trigger", "motion_hall_camera"),
        ]


class TestSatelOutputTarget:

    async def test_fixed_duration_turns_output_on_then_off(self, engine, fake_satel, fake_tuya):
        fake_satel.monitored_outputs = {"Brama": 5}
        await engine.handle_motion("satel_zone", 19)
        await asyncio.sleep(0.05)
        assert fake_satel.calls == [("output", 5, True), ("output", 5, False)]
        assert fake_tuya.calls == []

    async def test_turn_off_action_sets_output_false(self, engine, fake_satel):
        fake_satel.monitored_outputs = {"Brama": 5}
        await engine.handle_motion("satel_zone", 20)
        assert ("output", 5, False) in fake_satel.calls

    async def test_until_restored_toggles_output_with_zone(self, engine, fake_satel):
        fake_satel.monitored_outputs = {"Brama": 5}
        await engine.handle_motion("satel_zone", 21, trigger="violated")
        assert ("output", 5, True) in fake_satel.calls
        await engine.handle_motion("satel_zone", 21, trigger="restored")
        assert ("output", 5, False) in fake_satel.calls

    async def test_unknown_output_name_is_skipped_without_crash(self, engine, fake_satel):
        fake_satel.monitored_outputs = {}
        await engine.handle_motion("satel_zone", 20)
        assert fake_satel.calls == []


class TestAutomationRunningStatus:

    async def test_seeded_as_idle_for_named_scene_and_duration_rule(self, engine):
        await engine.start()
        assert engine.history.get_status_value("automation:regula_zone_12") == "bezczynna"
        assert engine.history.get_status_value("automation:wykryto_intruza") == "bezczynna"

    async def test_seed_does_not_overwrite_already_running_status(self, engine):
        engine.history.set_status("automation:regula_zone_12", "automation", "uruchomiona", label="regula_zone_12")
        engine._seed_automation_status_labels()
        assert engine.history.get_status_value("automation:regula_zone_12") == "uruchomiona"

    async def test_unnamed_rule_is_not_seeded(self, engine):
        await engine.start()
        assert engine.history.get_status_value("automation:13") is None

    async def test_named_duration_rule_ends_idle_after_firing(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 12)
        await asyncio.sleep(0)
        assert engine.history.get_status_value("automation:regula_zone_12") == "bezczynna"

    async def test_scene_fired_via_rule_ends_idle_after_firing(self, engine, fake_tuya):
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        await engine.handle_motion("satel_zone", 14)
        assert engine.history.get_status_value("automation:wykryto_intruza") == "bezczynna"


class TestMotionRuleCondition:

    async def _reload_with_rule(self, engine, rule: dict, fake_tuya=None, fake_satel=None, fake_virtual_inputs=None):
        engine.config.store.set("automations", {
            "scenes": [], "reguły_ruchu": [rule], "presence_simulation": {"enabled": False},
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya, fake_satel, fake_virtual_inputs)

    async def test_state_condition_blocks_when_not_met(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "satel_zone", "zone_number": 20,
            "target_device": "gniazdko_salon", "duration_seconds": 5,
            "condition": {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
        }, fake_tuya=fake_tuya)
        await engine.handle_motion("satel_zone", 20)
        assert fake_tuya.calls == []

    async def test_state_condition_allows_when_met(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "satel_zone", "zone_number": 20,
            "target_device": "gniazdko_salon", "duration_seconds": 5,
            "condition": {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
        }, fake_tuya=fake_tuya)
        engine.history.set_status("satel_input:9", "satel_input", "naruszone")
        await engine.handle_motion("satel_zone", 20)
        await asyncio.sleep(0)
        assert any(c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_nested_and_not_condition(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "satel_zone", "zone_number": 20,
            "target_device": "gniazdko_salon", "duration_seconds": 5,
            "condition": {
                "type": "and",
                "conditions": [
                    {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
                    {"type": "not", "conditions": [
                        {"type": "state", "key": "satel_output:5", "equals": "włączone"},
                    ]},
                ],
            },
        }, fake_tuya=fake_tuya)
        engine.history.set_status("satel_input:9", "satel_input", "naruszone")
        engine.history.set_status("satel_output:5", "satel_output", "wyłączone")
        await engine.handle_motion("satel_zone", 20)
        await asyncio.sleep(0)
        assert any(c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_nested_and_not_condition_blocks_when_negated_part_true(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "satel_zone", "zone_number": 20,
            "target_device": "gniazdko_salon", "duration_seconds": 5,
            "condition": {
                "type": "and",
                "conditions": [
                    {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
                    {"type": "not", "conditions": [
                        {"type": "state", "key": "satel_output:5", "equals": "włączone"},
                    ]},
                ],
            },
        }, fake_tuya=fake_tuya)
        await engine.handle_motion("satel_zone", 20)
        assert fake_tuya.calls == []

    async def test_malformed_condition_fails_safe_without_crashing(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "satel_zone", "zone_number": 20,
            "target_device": "gniazdko_salon", "duration_seconds": 5,
            "condition": {"type": "state"},
        }, fake_tuya=fake_tuya)
        await engine.handle_motion("satel_zone", 20)
        assert fake_tuya.calls == []


class TestDeviceStateTrigger:

    async def _reload_with_rule(self, engine, rule: dict, fake_tuya=None, fake_satel=None, fake_virtual_inputs=None):
        engine.config.store.set("automations", {
            "scenes": [], "reguły_ruchu": [rule], "presence_simulation": {"enabled": False},
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya, fake_satel, fake_virtual_inputs)

    async def test_tuya_state_matching_value_fires_action(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": 5,
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "włączone")
        await asyncio.sleep(0)
        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_tuya_state_non_matching_value_does_not_fire(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": 5,
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "wyłączone")
        assert fake_tuya.calls == []

    async def test_wrong_source_device_does_not_fire(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": 5,
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("tuya_state", "inne_urzadzenie", "włączone")
        assert fake_tuya.calls == []

    async def test_repeated_identical_value_does_not_refire(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": 5,
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "włączone")
        await asyncio.sleep(0)
        fake_tuya.calls.clear()
        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "włączone")
        await asyncio.sleep(0)
        assert fake_tuya.calls == []

    async def test_homewhiz_state_matching_value_fires_action(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "homewhiz_state", "source_device": "Zmywarka", "trigger_value": "finished",
            "target_device": "gniazdko_salon", "duration_seconds": 5,
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("homewhiz_state", "Zmywarka", "finished")
        await asyncio.sleep(0)
        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_until_restored_turns_on_then_off_on_value_change(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon",
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "włączone")
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls
        assert not any(c[0] == "on_for" for c in fake_tuya.calls)

        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "wyłączone")
        assert ("off", "gniazdko_salon") in fake_tuya.calls

    async def test_duration_zero_is_equivalent_to_blank_for_until_restored(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": 0,
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "włączone")
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls
        assert not any(c[0] == "on_for" for c in fake_tuya.calls)

    async def test_disabled_rule_is_not_matched(self, engine, fake_tuya):
        await self._reload_with_rule(engine, {
            "source": "tuya_state", "source_device": "czujnik_drzwi", "trigger_value": "włączone",
            "target_device": "gniazdko_salon", "duration_seconds": 5, "enabled": False,
        }, fake_tuya=fake_tuya)
        await engine.handle_device_state("tuya_state", "czujnik_drzwi", "włączone")
        assert fake_tuya.calls == []


class TestMotionRuleChaining:

    async def _reload_with_rules(self, engine, rules: list[dict], fake_tuya=None, fake_satel=None, fake_virtual_inputs=None):
        engine.config.store.set("automations", {
            "scenes": [{"name": "wykryto_intruza", "actions": [{"type": "tuya_on", "device": "gniazdko_salon"}]}],
            "reguły_ruchu": rules, "presence_simulation": {"enabled": False},
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya, fake_satel, fake_virtual_inputs)

    async def test_chain_triggers_second_rule(self, engine, fake_tuya):
        await self._reload_with_rules(engine, [
            {
                "source": "satel_zone", "zone_number": 21, "name": "regula_a",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
                "then_trigger_rule": "regula_b",
            },
            {
                "source": "satel_zone", "zone_number": 22, "name": "regula_b",
                "scene": "wykryto_intruza",
            },
        ], fake_tuya=fake_tuya)
        await engine.handle_motion("satel_zone", 21)
        await asyncio.sleep(0)
        assert any(c == ("on_for", "gniazdko_salon", 5, None) for c in fake_tuya.calls)
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls

    async def test_chain_logs_event_with_chain_source(self, engine):
        await self._reload_with_rules(engine, [
            {
                "source": "satel_zone", "zone_number": 21, "name": "regula_a",
                "scene": "wykryto_intruza", "then_trigger_rule": "regula_b",
            },
            {
                "source": "satel_zone", "zone_number": 22, "name": "regula_b",
                "scene": "wykryto_intruza",
            },
        ])
        await engine.handle_motion("satel_zone", 21)
        events = engine.history.get_events("motion", 0, time.time() + 1)
        assert any(e["source"] == "chain:regula_a" for e in events)

    async def test_unknown_chain_target_logs_warning_but_first_rule_still_fires(self, engine, fake_tuya, caplog):
        await self._reload_with_rules(engine, [
            {
                "source": "satel_zone", "zone_number": 21, "name": "regula_a",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
                "then_trigger_rule": "nie_istnieje",
            },
        ], fake_tuya=fake_tuya)
        with caplog.at_level("WARNING", logger="bridge.rules"):
            await engine.handle_motion("satel_zone", 21)
        await asyncio.sleep(0)
        assert any(c[0] == "on_for" for c in fake_tuya.calls)
        assert any("nieznaną/usuniętą" in r.message for r in caplog.records)

    async def test_chain_respects_target_rule_time_window(self, engine, fake_tuya, monkeypatch):
        import datetime as dt
        import bridge.rules_engine as re_module

        class FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 1, 15, 20, 0)

        monkeypatch.setattr(re_module, "datetime", FixedDatetime)
        await self._reload_with_rules(engine, [
            {
                "source": "satel_zone", "zone_number": 21, "name": "regula_a",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
                "then_trigger_rule": "regula_b",
            },
            {
                "source": "satel_zone", "zone_number": 22, "name": "regula_b",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
                "time_window": {"start": "08:00", "end": "09:00"},
            },
        ], fake_tuya=fake_tuya)
        await engine.handle_motion("satel_zone", 21)
        await asyncio.sleep(0)
        assert len([c for c in fake_tuya.calls if c[0] == "on_for"]) == 1

    async def test_cycle_is_bounded_by_depth_guard(self, engine, caplog):
        await self._reload_with_rules(engine, [
            {
                "source": "satel_zone", "zone_number": 21, "name": "regula_a",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
                "then_trigger_rule": "regula_b",
            },
            {
                "source": "satel_zone", "zone_number": 22, "name": "regula_b",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
                "then_trigger_rule": "regula_a",
            },
        ])
        with caplog.at_level("WARNING", logger="bridge.rules"):
            await engine.handle_motion("satel_zone", 21)
        assert any("zbyt duża głębokość" in r.message for r in caplog.records)


class TestSatelZoneOutputCallbackShape:

    async def test_zone_change_updates_correct_zone_not_bogus_key(self, engine, fake_satel):
        fake_satel.monitored_zones = {"Czujka salon": 12}
        await engine.start()
        await fake_satel._zone_cb({12: 1})

        statuses = {s["key"]: s["value"] for s in engine.history.get_status("satel_input")}
        assert statuses["satel_input:12"] == "naruszone"
        assert "satel_input:zones" not in statuses

    async def test_output_change_updates_correct_output_not_bogus_key(self, engine, fake_satel):
        fake_satel.monitored_outputs = {"Brama": 9}
        await engine.start()
        await fake_satel._output_cb({9: 1})

        statuses = {s["key"]: s["value"] for s in engine.history.get_status("satel_output")}
        assert statuses["satel_output:9"] == "włączone"
        assert "satel_output:outputs" not in statuses

    async def test_zone_violation_still_triggers_motion_rule(self, engine, fake_satel, fake_tuya):
        fake_satel.monitored_zones = {"Czujka": 12}
        await engine.start()
        await fake_satel._zone_cb({12: 1})
        await asyncio.sleep(0)
        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)

    async def test_zone_restore_triggers_restored_rule(self, engine, fake_satel, fake_tuya):
        fake_satel.monitored_zones = {"Czujka": 15}
        await engine.start()
        await fake_satel._zone_cb({15: 0})
        await asyncio.sleep(0)
        assert ("off", "gniazdko_salon") in fake_tuya.calls

    async def test_zone_violation_does_not_fire_restored_only_rule(self, engine, fake_satel, fake_tuya):
        fake_satel.monitored_zones = {"Czujka": 15}
        await engine.start()
        await fake_satel._zone_cb({15: 1})
        await asyncio.sleep(0)
        assert fake_tuya.calls == []


class TestEncryptionEnabledGatesEncryptionKey:

    def _build_engine(self, tmp_path, extra_satel_yaml: str) -> RulesEngine:
        (tmp_path / "sensors.yaml").write_text("", encoding="utf-8")
        (tmp_path / "config.yaml").write_text(
            f"""
satel:
  host: "127.0.0.1"
  port: 7094
  integration_key: "x"
{extra_satel_yaml}
tuya:
  devices: {{}}
ebus:
  sensors: []
dashboard:
  db_path: "{tmp_path / 'history.db'}"
""",
            encoding="utf-8",
        )
        config = BridgeConfig.load(tmp_path / "config.yaml")
        return RulesEngine(config)

    async def test_key_not_passed_when_encryption_disabled(self, tmp_path):
        engine = self._build_engine(tmp_path, '  encryption_key: "AESKEY"\n  encryption_enabled: false\n')
        assert engine.satel.encryption_key is None

    async def test_key_not_passed_when_encryption_enabled_missing(self, tmp_path):
        engine = self._build_engine(tmp_path, '  encryption_key: "AESKEY"\n')
        assert engine.satel.encryption_key is None

    async def test_key_passed_when_encryption_enabled(self, tmp_path):
        engine = self._build_engine(tmp_path, '  encryption_key: "AESKEY"\n  encryption_enabled: true\n')
        assert engine.satel.encryption_key == "AESKEY"


class TestIntegrationKeyOptional:

    def _build_engine(self, tmp_path, satel_yaml: str) -> RulesEngine:
        (tmp_path / "sensors.yaml").write_text("", encoding="utf-8")
        (tmp_path / "config.yaml").write_text(
            f"""
satel:
  host: "127.0.0.1"
  port: 7094
{satel_yaml}
tuya:
  devices: {{}}
ebus:
  sensors: []
dashboard:
  db_path: "{tmp_path / 'history.db'}"
""",
            encoding="utf-8",
        )
        config = BridgeConfig.load(tmp_path / "config.yaml")
        return RulesEngine(config)

    async def test_construction_succeeds_with_empty_key(self, tmp_path):
        engine = self._build_engine(tmp_path, '  integration_key: ""\n')
        assert engine.satel.integration_key == ""

    async def test_construction_succeeds_with_key_field_absent(self, tmp_path):
        engine = self._build_engine(tmp_path, "")
        assert engine.satel.integration_key == ""


class TestLightToSatelRule:

    async def test_light_on_triggers_virtual_input(self, engine, fake_satel):
        fake_satel.virtual_inputs = {"test_input": 1}
        await engine.handle_light_state_change("gniazdko_salon", True)
        assert ("output", 1, True) in fake_satel.calls

    async def test_light_off_does_not_trigger(self, engine, fake_satel):
        fake_satel.virtual_inputs = {"test_input": 1}
        await engine.handle_light_state_change("gniazdko_salon", False)
        assert fake_satel.calls == []


class TestCameraRules:
    async def test_camera_motion_triggers_action(self, engine, fake_satel):
        fake_satel.virtual_inputs = {"test_input": 1}
        await engine.handle_camera_motion("unifi_protect", "cam1")
        assert ("output", 1, True) in fake_satel.calls

    async def test_camera_cooldown_suppresses_repeat(self, engine, fake_satel):
        fake_satel.virtual_inputs = {"test_input": 1}
        await engine.handle_camera_motion("unifi_protect", "cam1")
        fake_satel.calls.clear()
        await engine.handle_camera_motion("unifi_protect", "cam1")
        assert fake_satel.calls == []

    async def test_camera_scene_action(self, engine, fake_tuya):
        await engine.handle_camera_motion("unifi_protect", "cam2")
        assert ("on", "gniazdko_salon", None) in fake_tuya.calls

    async def test_unmatched_camera_does_nothing(self, engine, fake_satel, fake_tuya):
        fake_satel.virtual_inputs = {"test_input": 1}
        await engine.handle_camera_motion("unifi_protect", "nieznana_kamera")
        assert fake_satel.calls == []
        assert fake_tuya.calls == []


class TestBoilerValueRouting:
    def test_history_only_sensor_not_published(self, engine):
        sensor_cfg = {
            "name": "kociol_temp", "label": "Temperatura",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False},
        }
        engine.handle_boiler_value(sensor_cfg, 72.5)
        history_data = engine.history.get_history("kociol_temp", 0, time.time() + 1)
        assert len(history_data) == 1
        assert history_data[0]["v"] == 72.5
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

    async def test_marks_command_done_and_schedules_exit(self, engine, monkeypatch):
        import bridge.rules_engine as re_module

        exit_calls = []
        monkeypatch.setattr(re_module.os, "_exit", lambda code: exit_calls.append(code))

        cmd_id = engine.history.enqueue_command("restart_process", {})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert engine.history.get_command(cmd_id)["status"] == "done"
        await asyncio.sleep(0.6)
        assert exit_calls == [0]


class TestExecuteCommandRunScene:

    async def test_runs_enabled_scene_and_marks_done(self, engine, fake_tuya):
        cmd_id = engine.history.enqueue_command("run_scene", {"name": "wykryto_intruza"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert ("on", "gniazdko_salon", None) in fake_tuya.calls
        assert engine.history.get_command(cmd_id)["status"] == "done"

    async def test_disabled_scene_marks_done_but_does_not_run_actions(self, engine, fake_tuya):
        cmd_id = engine.history.enqueue_command("run_scene", {"name": "scena_wylaczona"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert fake_tuya.calls == []
        assert engine.history.get_command(cmd_id)["status"] == "done"

    async def test_unknown_scene_marks_command_error(self, engine):
        cmd_id = engine.history.enqueue_command("run_scene", {"name": "nie_istnieje"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        result = engine.history.get_command(cmd_id)
        assert result["status"] == "error"
        assert "nie_istnieje" in result["result"]["error"]


class TestExecuteCommandTuya:

    async def test_tuya_on_calls_turn_on_and_marks_done(self, engine, fake_tuya):
        cmd_id = engine.history.enqueue_command("tuya_on", {"name": "gniazdko_salon"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert ("on", "gniazdko_salon", None) in fake_tuya.calls
        assert engine.history.get_command(cmd_id)["status"] == "done"

    async def test_tuya_off_calls_turn_off_and_marks_done(self, engine, fake_tuya):
        cmd_id = engine.history.enqueue_command("tuya_off", {"name": "gniazdko_salon"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert ("off", "gniazdko_salon") in fake_tuya.calls
        assert engine.history.get_command(cmd_id)["status"] == "done"

    async def test_tuya_on_failure_marks_command_error(self, engine, fake_tuya):
        fake_tuya.fail_devices.add("gniazdko_salon")
        cmd_id = engine.history.enqueue_command("tuya_on", {"name": "gniazdko_salon"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        result = engine.history.get_command(cmd_id)
        assert result["status"] == "error"
        assert "symulowana awaria" in result["result"]["error"]

    async def test_tuya_on_schedules_status_refresh(self, engine, fake_tuya, monkeypatch):
        scheduled = []
        monkeypatch.setattr(engine, "_schedule_tuya_status_refresh", lambda name: scheduled.append(name))
        cmd_id = engine.history.enqueue_command("tuya_on", {"name": "gniazdko_salon"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert scheduled == ["gniazdko_salon"]

    async def test_tuya_off_schedules_status_refresh(self, engine, fake_tuya, monkeypatch):
        scheduled = []
        monkeypatch.setattr(engine, "_schedule_tuya_status_refresh", lambda name: scheduled.append(name))
        cmd_id = engine.history.enqueue_command("tuya_off", {"name": "gniazdko_salon"})
        cmd = engine.history.get_command(cmd_id)
        await engine._execute_command(cmd)

        assert scheduled == ["gniazdko_salon"]


class TestScheduleTuyaStatusRefresh:

    async def test_refreshes_status_in_history_after_delay(self, engine, fake_tuya):
        engine._schedule_tuya_status_refresh("gniazdko_salon", delay_seconds=0)
        await asyncio.sleep(0.05)

        statuses = engine.history.get_status("tuya")
        assert any(s["key"] == "tuya:gniazdko_salon" and s["value"] == "włączone" for s in statuses)

    async def test_get_status_error_is_logged_not_raised(self, engine, fake_tuya, monkeypatch, caplog):
        async def boom(device):
            raise ConnectionError("symulowana awaria odczytu")

        monkeypatch.setattr(fake_tuya, "get_status", boom)
        with caplog.at_level("ERROR"):
            engine._schedule_tuya_status_refresh("gniazdko_salon", delay_seconds=0)
            await asyncio.sleep(0.05)

        assert "odświeżyć statusu" in caplog.text


class TestBuildTuyaController:

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
        from bridge.rules_engine import _build_tuya_controller

        controller = _build_tuya_controller({"devices": {}})
        assert controller.cloud.devices_cfg == {}
        assert controller.local.devices_cfg == {}


class TestReloadConfig:

    async def test_ebus_listener_none_does_not_crash_reload(self, engine):
        assert engine.ebus_listener is None
        await engine.reload_config()

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
        assert "lampa_taras" in engine.tuya.devices_cfg

    async def test_picks_up_new_location_without_restart(self, engine):
        config = engine.config.store.get("config")
        config["location"] = {"latitude": 52.23, "longitude": 21.01}
        engine.config.store.set("config", config)
        await engine.reload_config()
        assert engine._location_cfg == {"latitude": 52.23, "longitude": 21.01}

    async def test_reload_updates_presence_simulation_location_cfg(self, engine):
        config = engine.config.store.get("config")
        config["location"] = {"latitude": 52.23, "longitude": 21.01}
        engine.config.store.set("config", config)
        await engine.reload_config()
        assert engine.presence_simulation.location_cfg == {"latitude": 52.23, "longitude": 21.01}

    async def test_local_device_takes_precedence_over_cloud_after_local_configured(self, engine, tmp_path):
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
        old_satel = engine.satel
        await engine.reload_config()
        assert engine.satel is old_satel

    async def test_reload_config_updates_satel_poll_interval_live(self, engine, tmp_path, fake_satel):
        raw = engine.config.store.get("config")
        raw["satel"]["status_poll_interval_seconds"] = 5
        engine.config.store.set("config", raw)

        await engine.reload_config()
        assert engine.satel.status_poll_interval_seconds == 5

    async def test_reload_config_updates_tuya_poll_interval_live(self, engine, tmp_path):
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


class TestStaleStatusPruning:

    async def test_removed_satel_zone_prunes_stale_status(self, engine):
        engine.config.store.set("satel_fields", {
            "monitored_zones": {"Wejscie_A": 50}, "monitored_outputs": {},
            "partitions": {}, "virtual_inputs": {},
        })
        await engine.reload_config()
        assert engine.history.get_status_value("satel_input:50") is not None

        engine.config.store.set("satel_fields", {
            "monitored_zones": {}, "monitored_outputs": {},
            "partitions": {}, "virtual_inputs": {},
        })
        await engine.reload_config()
        assert engine.history.get_status_value("satel_input:50") is None

    async def test_renamed_satel_zone_removes_old_number_but_keeps_new(self, engine):
        engine.config.store.set("satel_fields", {
            "monitored_zones": {"Wejscie_A": 50}, "monitored_outputs": {},
            "partitions": {}, "virtual_inputs": {},
        })
        await engine.reload_config()
        engine.config.store.set("satel_fields", {
            "monitored_zones": {"Wejscie_A": 51}, "monitored_outputs": {},
            "partitions": {}, "virtual_inputs": {},
        })
        await engine.reload_config()
        assert engine.history.get_status_value("satel_input:50") is None
        assert engine.history.get_status_value("satel_input:51") is not None

    async def test_removed_satel_output_prunes_stale_status(self, engine):
        engine.config.store.set("satel_fields", {
            "monitored_zones": {}, "monitored_outputs": {"Brama_garaz": 7},
            "partitions": {}, "virtual_inputs": {},
        })
        await engine.reload_config()
        assert engine.history.get_status_value("satel_output:7") is not None

        engine.config.store.set("satel_fields", {
            "monitored_zones": {}, "monitored_outputs": {},
            "partitions": {}, "virtual_inputs": {},
        })
        await engine.reload_config()
        assert engine.history.get_status_value("satel_output:7") is None

    async def test_removed_tuya_device_prunes_stale_status(self, engine):
        await engine.reload_config()
        assert engine.history.get_status_value("tuya:gniazdko_salon") is not None

        engine.config.store.set("tuya_devices", {"devices": {}})
        await engine.reload_config()
        assert engine.history.get_status_value("tuya:gniazdko_salon") is None

    async def test_tuya_brightness_key_survives_reload_of_same_device(self, engine):
        engine.history.set_status("tuya:gniazdko_salon:brightness", "tuya", "50")
        await engine.reload_config()
        assert engine.history.get_status_value("tuya:gniazdko_salon:brightness") == "50"

    async def test_removed_scene_prunes_stale_automation_status(self, engine):
        await engine.start()
        assert engine.history.get_status_value("automation:wykryto_intruza") is not None

        engine.config.store.set("automations", {
            "scenes": [], "reguły_ruchu": [], "presence_simulation": {"enabled": False},
        })
        await engine.reload_config()
        assert engine.history.get_status_value("automation:wykryto_intruza") is None


class TestPluginDisabledHealthStatus:

    async def test_disabled_satel_writes_neutral_status_without_connecting(self, engine):
        engine.config.satel["enabled"] = False
        await engine.start()
        assert engine.history.get_status_value("health:satel") == "wyłączono (wtyczka)"

    async def test_disabled_tuya_writes_neutral_status(self, engine):
        engine.config.tuya["enabled"] = False
        await engine.start()
        assert engine.history.get_status_value("health:tuya_local") == "wyłączono (wtyczka)"

    async def test_disabled_ebus_writes_neutral_status(self, engine):
        engine.config.ebus["enabled"] = False
        await engine.start()
        assert engine.history.get_status_value("health:ebus_mqtt") == "wyłączono (wtyczka)"

    async def test_enabled_plugins_do_not_get_neutral_status(self, engine):
        await engine.start()
        assert engine.history.get_status_value("health:satel") != "wyłączono (wtyczka)"
        assert engine.history.get_status_value("health:tuya_local") != "wyłączono (wtyczka)"
        assert engine.history.get_status_value("health:ebus_mqtt") != "wyłączono (wtyczka)"

    async def test_disabled_satel_overwrites_frozen_zone_status(self, engine, fake_satel):
        fake_satel.monitored_zones = {"Zmywarka": 1}
        engine.history.set_status("satel_input:1", "satel_input", "OK", label="Zmywarka")
        engine.config.satel["enabled"] = False
        await engine.start()
        assert engine.history.get_status_value("satel_input:1") == "brak danych (wtyczka wyłączona)"

    async def test_disabled_satel_overwrites_frozen_output_status(self, engine, fake_satel):
        fake_satel.monitored_outputs = {"Brama": 7}
        engine.history.set_status("satel_output:7", "satel_output", "włączone", label="Brama")
        engine.config.satel["enabled"] = False
        await engine.start()
        assert engine.history.get_status_value("satel_output:7") == "brak danych (wtyczka wyłączona)"

    async def test_disabled_satel_overwrites_frozen_partition_status(self, engine):
        engine.history.set_status("satel_partition:1", "satel_partition", "rozbrojona", label="Dom")
        engine.config.satel["enabled"] = False
        await engine.start()
        assert engine.history.get_status_value("satel_partition:1") == "brak danych (wtyczka wyłączona)"

    async def test_enabled_satel_seeds_ok_not_placeholder(self, engine, fake_satel):
        fake_satel.monitored_zones = {"Zmywarka": 1}
        await engine.start()
        assert engine.history.get_status_value("satel_input:1") == "OK"

    async def test_enabled_satel_does_not_overwrite_live_zone_status(self, engine, fake_satel):
        fake_satel.monitored_zones = {"Zmywarka": 1}
        engine.history.set_status("satel_input:1", "satel_input", "naruszone", label="Zmywarka")
        await engine.start()
        assert engine.history.get_status_value("satel_input:1") == "naruszone"


class TestHomeWhizStatusSeeding:

    def _set_homewhiz_cfg(self, engine, appliances: dict) -> None:
        raw = engine.config.store.get("config")
        raw["homewhiz"] = {"username": "u", "password": "p", "appliances": appliances}
        engine.config.store.set("config", raw)

    async def test_seeded_as_unknown_for_configured_appliance(self, engine):
        self._set_homewhiz_cfg(engine, {"Zmywarka": {"appliance_id": "abc123"}})
        await engine.reload_config()
        assert engine.history.get_status_value("homewhiz:Zmywarka") == "nieznany"

    async def test_seed_does_not_overwrite_already_running_status(self, engine):
        self._set_homewhiz_cfg(engine, {"Zmywarka": {"appliance_id": "abc123"}})
        await engine.reload_config()
        engine.history.set_status("homewhiz:Zmywarka", "homewhiz", "running", label="Zmywarka")
        engine._seed_homewhiz_status_labels()
        assert engine.history.get_status_value("homewhiz:Zmywarka") == "running"

    async def test_removed_appliance_prunes_stale_status(self, engine):
        self._set_homewhiz_cfg(engine, {"Zmywarka": {"appliance_id": "abc123"}})
        await engine.reload_config()
        assert engine.history.get_status_value("homewhiz:Zmywarka") is not None

        self._set_homewhiz_cfg(engine, {})
        await engine.reload_config()
        assert engine.history.get_status_value("homewhiz:Zmywarka") is None

    async def test_no_homewhiz_config_is_a_noop(self, engine):
        await engine.reload_config()
        assert engine.history.get_status("homewhiz") == []


class TestHomeWhizStateCallback:

    async def test_publishes_state_as_status(self, engine):
        await engine._on_homewhiz_state("Zmywarka", "running")
        assert engine.history.get_status_value("homewhiz:Zmywarka") == "running"
        rows = engine.history.get_status("homewhiz")
        assert rows[0]["label"] == "Zmywarka"

    async def test_also_dispatches_to_device_state_motion_rules(self, engine, fake_tuya):
        engine.config.store.set("automations", {
            "scenes": [], "presence_simulation": {"enabled": False},
            "reguły_ruchu": [{
                "source": "homewhiz_state", "source_device": "Zmywarka", "trigger_value": "finished",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
            }],
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya=fake_tuya)

        await engine._on_homewhiz_state("Zmywarka", "finished")
        await asyncio.sleep(0)
        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)


class TestApplyTuyaDeviceStatus:

    async def test_local_device_without_schema_still_uses_raw_key_1(self, engine):
        await engine._apply_tuya_device_status("gniazdko_salon", {"1": True})
        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert statuses["tuya:gniazdko_salon"] == "włączone"

    async def test_sensor_device_without_schema_shows_raw_dp_values_instead_of_question_mark(self, engine):
        await engine._apply_tuya_device_status("gniazdko_salon", {"4": 235, "9": "auto"})
        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert statuses["tuya:gniazdko_salon"] != "?"
        assert "4: 235" in statuses["tuya:gniazdko_salon"]

    async def test_local_device_with_schema_shows_readable_labels(self, engine):
        engine.config.tuya["devices"]["gniazdko_salon"]["dp_schema"] = {
            "1": {"code": "switch_1", "type": "Boolean", "values": "{}"},
            "4": {"code": "va_temperature", "type": "Value", "values": '{"scale":1,"unit":"C"}'},
        }
        await engine._apply_tuya_device_status("gniazdko_salon", {"1": False, "4": 235})
        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert statuses["tuya:gniazdko_salon"] == "wyłączone"

    async def test_pure_sensor_device_with_schema_has_no_toggle_state(self, engine):
        engine.config.tuya["devices"]["gniazdko_salon"]["dp_schema"] = {
            "2": {"code": "co2_value", "type": "Value", "values": "{}"},
        }
        await engine._apply_tuya_device_status("gniazdko_salon", {"2": 420})
        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert statuses["tuya:gniazdko_salon"] == "CO2: 420"

    async def test_cloud_device_uses_configured_switch_code(self, engine):
        engine.config.tuya.setdefault("cloud_devices", {})["kamera"] = {"id": "c1", "switch_code": "power_toggle"}
        await engine._apply_tuya_device_status("kamera", {"power_toggle": True, "other_field": 5})
        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert statuses["tuya:kamera"] == "włączone"

    async def test_unreachable_device_still_marked_niedostepne(self, engine):
        await engine._apply_tuya_device_status("gniazdko_salon", None)
        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert statuses["tuya:gniazdko_salon"] == "niedostępne"


class TestTuyaStatusPollLoop:

    async def test_cloud_only_device_does_not_crash_loop_for_other_devices(self, engine, fake_tuya):
        fake_tuya.devices_cfg = {
            "T & H Sensor": {"id": "cloud1"},
            "gniazdko_salon": {"id": "dev1", "ip": "192.168.1.50", "local_key": "key1"},
        }
        try:
            await asyncio.wait_for(engine._tuya_status_poll_loop(), timeout=0.05)
        except asyncio.TimeoutError:
            pass

        statuses = {s["key"]: s["value"] for s in engine.history.get_status("tuya")}
        assert "tuya:gniazdko_salon" in statuses
        assert "tuya:T & H Sensor" in statuses

    async def test_poll_loop_dispatches_to_device_state_motion_rules(self, engine, fake_tuya):
        engine.config.store.set("automations", {
            "scenes": [], "presence_simulation": {"enabled": False},
            "reguły_ruchu": [{
                "source": "tuya_state", "source_device": "czujnik_ruchu", "trigger_value": "włączone",
                "target_device": "gniazdko_salon", "duration_seconds": 5,
            }],
        })
        await engine.reload_config()
        _repatch_fakes(engine, fake_tuya=fake_tuya)
        fake_tuya.devices_cfg = {"czujnik_ruchu": {"id": "dev2", "ip": "1.2.3.4", "local_key": "k"}}

        try:
            await asyncio.wait_for(engine._tuya_status_poll_loop(), timeout=0.05)
        except asyncio.TimeoutError:
            pass

        assert any(c[0] == "on_for" and c[1] == "gniazdko_salon" for c in fake_tuya.calls)


class TestStaleCheckLoop:

    async def test_survives_exception_in_check_stale_sensors_and_keeps_looping(self, engine, monkeypatch):
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real_sleep(0))

        calls = {"count": 0}

        async def _raising_check():
            calls["count"] += 1
            raise RuntimeError("baza zablokowana")

        monkeypatch.setattr(engine.alarms, "check_stale_sensors", _raising_check)

        task = asyncio.create_task(engine._stale_check_loop())
        for _ in range(30):
            await real_sleep(0.001)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert calls["count"] >= 2, (
            "Pętla sprawdzania nieaktywnych czujników zatrzymała się trwale po jednym błędzie"
        )


class TestStateMqttPublisher:

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
        engine = self._build_engine(tmp_path)
        assert engine.ha_mqtt is None
        assert engine.state_mqtt is not None


class TestAlarmStatusCallback:

    async def test_callback_accepts_no_arguments(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._handle_alarm_status_change()

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

    async def test_does_not_apply_immediately(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._on_alarm_status_change()
        assert engine.history.get_status("satel_partition") == []

    async def test_applies_after_debounce_delay(self, engine, fake_satel):
        fake_satel.armed_partition_numbers = {1}
        await engine._on_alarm_status_change()
        await asyncio.sleep(0.25)
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
