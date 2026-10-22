# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio

import pytest

from bridge.scenes import SceneRunner
from tests.test_flow_engine import _engine
from tests.test_rules_engine import CONFIG_TEMPLATE, SENSORS_YAML, engine


def _scene(actions, name="s"):
    return [{"name": name, "actions": actions}]


def _offs(fake_tuya, device="lampa"):
    return [c for c in fake_tuya.calls if c == ("off", device)]


class TestSceneDelays:
    async def test_rerun_restarts_scene(self, fake_tuya, fake_satel, history):
        cfg = _scene([{"type": "tuya_on", "device": "lampa"}, {"type": "delay", "seconds": 0.15},
                      {"type": "tuya_off", "device": "lampa"}])
        runner = SceneRunner(cfg, fake_tuya, fake_satel, history=history)
        first = asyncio.create_task(runner.run("s"))
        await asyncio.sleep(0.08)
        second = asyncio.create_task(runner.run("s"))
        await asyncio.gather(first, second)
        assert len(_offs(fake_tuya)) == 1
        assert history.get_status_value("automation:s") == "bezczynna"

    async def test_single_run_completes(self, fake_tuya, fake_satel, history):
        cfg = _scene([{"type": "delay", "seconds": 0.02}, {"type": "tuya_off", "device": "lampa"}])
        await SceneRunner(cfg, fake_tuya, fake_satel, history=history).run("s")
        assert len(_offs(fake_tuya)) == 1

    async def test_external_change_of_tuya_target_cancels(self, fake_tuya, fake_satel, history):
        cfg = _scene([{"type": "tuya_on", "device": "lampa"}, {"type": "delay", "seconds": 0.1},
                      {"type": "tuya_off", "device": "lampa"}])
        runner = SceneRunner(cfg, fake_tuya, fake_satel, history=history)
        task = asyncio.create_task(runner.run("s"))
        await asyncio.sleep(0.03)
        runner.invalidate_device("tuya", "lampa")
        await task
        assert _offs(fake_tuya) == []
        assert history.get_status_value("automation:s") == "bezczynna"

    async def test_external_change_of_satel_target_cancels(self, fake_tuya, fake_satel, history):
        cfg = _scene([{"type": "satel_output", "output_number": 4, "state": True}, {"type": "delay", "seconds": 0.1},
                      {"type": "satel_output", "output_number": 4, "state": False}])
        runner = SceneRunner(cfg, fake_tuya, fake_satel, history=history)
        task = asyncio.create_task(runner.run("s"))
        await asyncio.sleep(0.03)
        runner.invalidate_device("satel_output", 4)
        await task
        assert fake_satel.calls == [("output", 4, True)]

    async def test_other_device_does_not_cancel(self, fake_tuya, fake_satel, history):
        cfg = _scene([{"type": "delay", "seconds": 0.05}, {"type": "tuya_off", "device": "lampa"}])
        runner = SceneRunner(cfg, fake_tuya, fake_satel, history=history)
        task = asyncio.create_task(runner.run("s"))
        await asyncio.sleep(0.01)
        runner.invalidate_device("tuya", "inna")
        await task
        assert len(_offs(fake_tuya)) == 1

    async def test_satel_action_notifies_callback(self, fake_tuya, fake_satel, history):
        seen = []
        cfg = _scene([{"type": "satel_output", "output_number": 7, "state": True}])
        await SceneRunner(cfg, fake_tuya, fake_satel, history=history, on_satel_output=seen.append).run("s")
        assert seen == [7]


@pytest.fixture
def satel_engine(engine):
    engine.satel.monitored_outputs = {"syrena": 5}
    engine.satel.virtual_inputs = {"wirtualne": 6}
    engine.config.satel["monitored_outputs"] = {"syrena": 5}
    return engine


def _satel_offs(engine, number=5):
    return [c for c in engine.satel.calls if c == ("output", number, False)]


class TestSatelTimedTargets:
    async def test_retrigger_runs_single_off(self, satel_engine):
        first = asyncio.create_task(satel_engine._run_on_for_duration("satel_output", "syrena", 0.12, None, None))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(satel_engine._run_on_for_duration("satel_output", "syrena", 0.12, None, None))
        await asyncio.gather(first, second)
        assert len(_satel_offs(satel_engine)) == 1

    async def test_external_change_cancels_off(self, satel_engine):
        task = asyncio.create_task(satel_engine._run_on_for_duration("satel_output", "syrena", 0.1, None, None))
        await asyncio.sleep(0.03)
        satel_engine._external_satel_change(5)
        await task
        assert _satel_offs(satel_engine) == []

    async def test_explicit_command_supersedes_pending_off(self, satel_engine):
        task = asyncio.create_task(satel_engine._run_on_for_duration("satel_output", "syrena", 0.1, None, None))
        await asyncio.sleep(0.03)
        await satel_engine._set_target_state("satel_output", "syrena", True)
        await task
        assert _satel_offs(satel_engine) == []

    async def test_normal_run_turns_off(self, satel_engine):
        await satel_engine._run_on_for_duration("satel_output", "syrena", 0.02, None, None)
        assert satel_engine.satel.calls == [("output", 5, True), ("output", 5, False)]

    async def test_virtual_input_library_mode_also_guarded(self, satel_engine):
        satel_engine._vi_mode = "library"
        task = asyncio.create_task(
            satel_engine._run_on_for_duration("satel_virtual_input", "wirtualne", 0.1, None, None))
        await asyncio.sleep(0.03)
        satel_engine._external_satel_change(6)
        await task
        assert _satel_offs(satel_engine, 6) == []


class TestExternalOutputDetection:
    async def test_foreign_change_detected(self, satel_engine):
        await satel_engine._on_satel_output_change({5: False})
        before = satel_engine._target_gen.get(("satel_output", 5), 0)
        await satel_engine._on_satel_output_change({5: True})
        assert satel_engine._target_gen[("satel_output", 5)] == before + 1

    async def test_own_command_echo_not_external(self, satel_engine):
        await satel_engine._on_satel_output_change({5: False})
        await satel_engine.satel.set_output(5, True)
        before = satel_engine._target_gen.get(("satel_output", 5), 0)
        await satel_engine._on_satel_output_change({5: True})
        assert satel_engine._target_gen.get(("satel_output", 5), 0) == before

    async def test_first_report_is_only_a_baseline(self, satel_engine):
        await satel_engine._on_satel_output_change({5: True})
        assert ("satel_output", 5) not in satel_engine._target_gen

    async def test_external_change_invalidates_flows_and_scenes(self, satel_engine, monkeypatch):
        seen = []
        monkeypatch.setattr(satel_engine.flow_engine, "invalidate_device", lambda s, d: seen.append(("flow", s, d)))
        monkeypatch.setattr(satel_engine.scenes, "invalidate_device", lambda s, d: seen.append(("scene", s, d)))
        await satel_engine._on_satel_output_change({5: False})
        await satel_engine._on_satel_output_change({5: True})
        assert ("flow", "satel_output", "syrena") in seen and ("scene", "satel_output", 5) in seen

    async def test_status_is_still_written(self, satel_engine):
        await satel_engine._on_satel_output_change({5: True})
        assert satel_engine.history.get_status_value("satel_output:5") == "włączone"


class TestFlowsWithSatelTargets:
    def _flow(self, system):
        return {"id": "f", "name": "syrena", "enabled": True, "nodes": [
            {"id": "t", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 1, "op": "violated"},
             "wires": [["d"]]},
            {"id": "d", "type": "delay", "config": {"seconds": 0.1}, "wires": [["x"]]},
            {"id": "x", "type": "device", "config": {"target_device": "syrena", "target_system": system, "value": "off"},
             "wires": []},
        ]}

    @pytest.mark.parametrize("system", ["satel_output", "satel_virtual_input"])
    async def test_invalidate_cancels_pending_satel_action(self, system):
        engine, calls = _engine([self._flow(system)])
        await engine.handle_motion("satel_zone", 1, trigger="violated")
        await asyncio.sleep(0.03)
        engine.invalidate_device(system, "syrena")
        await asyncio.sleep(0.2)
        assert calls["set_target_state"] == []

    async def test_other_system_name_does_not_cancel(self):
        engine, calls = _engine([self._flow("satel_output")])
        await engine.handle_motion("satel_zone", 1, trigger="violated")
        engine.invalidate_device("tuya", "syrena")
        await asyncio.sleep(0.2)
        assert len(calls["set_target_state"]) == 1

    async def test_retrigger_restarts_non_light_action_too(self):
        engine, calls = _engine([self._flow("satel_output")])
        await engine.handle_motion("satel_zone", 1, trigger="violated")
        await asyncio.sleep(0.06)
        await engine.handle_motion("satel_zone", 1, trigger="violated")
        await asyncio.sleep(0.07)
        assert calls["set_target_state"] == []
        await asyncio.sleep(0.1)
        assert len(calls["set_target_state"]) == 1
