# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio

import pytest

from bridge import rules_engine as rules_mod
from bridge.tuya_dispatch import TuyaController
from dashboard.setup import build_flows, validate_flows
from tests.test_flow_engine import _engine
from tests.test_rules_engine import CONFIG_TEMPLATE, SENSORS_YAML, engine
from tests.test_tuya_cloud import FakeTinytuyaCloud, _set_fake_client
from tests.test_tuya_cloud import controller as cloud_controller
from tests.test_tuya_local import FlakyDevice
from tests.test_tuya_local import controller as local_controller


def _light_flow(retrigger=None, trigger_value="włączone", delay=0.15):
    flow = {"id": "f1", "name": "swiatlo", "enabled": True, "nodes": [
        {"id": "trig", "type": "trigger", "config": {"source": "tuya_state", "source_device": "lampa",
                                                      "op": "equals", "trigger_value": trigger_value}, "wires": [["dly"]]},
        {"id": "dly", "type": "delay", "config": {"seconds": delay}, "wires": [["off"]]},
        {"id": "off", "type": "device", "config": {"target_device": "lampa", "value": "off"}, "wires": [["end"]]},
        {"id": "end", "type": "stop", "config": {}, "wires": []},
    ]}
    if retrigger:
        flow["retrigger"] = retrigger
    return flow


def _offs(calls):
    return [c for c in calls["set_target_state"] if c[1] == "lampa" and c[2] is False]


class TestFlowRestart:
    async def test_retrigger_restarts_countdown(self):
        engine, calls = _engine([_light_flow()])
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.08)
        await engine.handle_device_state("tuya_state", "lampa", "wyłączone")
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.12)
        assert _offs(calls) == []
        await asyncio.sleep(0.2)
        assert len(_offs(calls)) == 1

    async def test_parallel_mode_keeps_all_runs(self):
        engine, calls = _engine([_light_flow(retrigger="parallel")])
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.05)
        await engine.handle_device_state("tuya_state", "lampa", "wyłączone")
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.35)
        assert len(_offs(calls)) == 2

    async def test_single_run_still_turns_off(self):
        engine, calls = _engine([_light_flow()])
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.3)
        assert len(_offs(calls)) == 1


class TestExternalChangeCancels:
    async def test_manual_change_cancels_pending_off(self):
        engine, calls = _engine([_light_flow()])
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.05)
        await engine.handle_device_state("tuya_state", "lampa", "wyłączone", external=True)
        await asyncio.sleep(0.3)
        assert _offs(calls) == []

    async def test_manual_on_after_off_restarts_instead_of_instant_off(self):
        engine, calls = _engine([_light_flow()])
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.05)
        await engine.handle_device_state("tuya_state", "lampa", "wyłączone", external=True)
        await engine.handle_device_state("tuya_state", "lampa", "włączone", external=True)
        await asyncio.sleep(0.07)
        assert _offs(calls) == []
        await asyncio.sleep(0.2)
        assert len(_offs(calls)) == 1

    async def test_own_change_does_not_cancel(self):
        engine, calls = _engine([_light_flow()])
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.05)
        await engine.handle_device_state("tuya_state", "lampa", "wyłączone", external=False)
        await asyncio.sleep(0.3)
        assert len(_offs(calls)) == 1

    async def test_other_device_does_not_cancel(self):
        engine, calls = _engine([_light_flow()])
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await engine.handle_device_state("tuya_state", "inna", "wyłączone", external=True)
        await asyncio.sleep(0.3)
        assert len(_offs(calls)) == 1

    async def test_cancel_is_written_to_events(self):
        events = []
        engine, _calls = _engine([_light_flow(delay=0.05)], write_event=lambda t, s, d: events.append((t, d)))
        await engine.handle_device_state("tuya_state", "lampa", "włączone")
        await asyncio.sleep(0.02)
        await engine.handle_device_state("tuya_state", "lampa", "wyłączone", external=True)
        await asyncio.sleep(0.15)
        assert any(d.get("cancelled") for _t, d in events)


class _Backend:
    def __init__(self):
        self.devices_cfg = {"lampa": {}}
        self.offs = []

    async def turn_on(self, device, dimmer=None, reason=None):
        pass

    async def turn_off(self, device, reason=None):
        self.offs.append(device)

    async def toggle(self, device, reason=None):
        pass

    async def turn_on_for_duration(self, device, duration, dimmer=None, reason=None, on_state_change=None,
                                    still_valid=None):
        await asyncio.sleep(duration)
        if still_valid is not None and not still_valid():
            return
        self.offs.append(device)


class TestDispatchTimers:
    @pytest.fixture
    def dispatch(self):
        local, cloud = _Backend(), _Backend()
        cloud.devices_cfg = {}
        return TuyaController(local, cloud), local

    async def test_new_timer_supersedes_previous(self, dispatch):
        d, local = dispatch
        first = asyncio.create_task(d.turn_on_for_duration("lampa", 0.1))
        await asyncio.sleep(0.04)
        second = asyncio.create_task(d.turn_on_for_duration("lampa", 0.1))
        await asyncio.gather(first, second)
        assert local.offs == ["lampa"]

    async def test_explicit_commands_cancel_pending_off(self, dispatch):
        d, local = dispatch
        for command in (d.turn_on, d.turn_off, d.toggle):
            local.offs.clear()
            task = asyncio.create_task(d.turn_on_for_duration("lampa", 0.05))
            await asyncio.sleep(0.01)
            await command("lampa")
            await task
            assert [o for o in local.offs] == (["lampa"] if command == d.turn_off else [])

    async def test_cancel_pending_off(self, dispatch):
        d, local = dispatch
        task = asyncio.create_task(d.turn_on_for_duration("lampa", 0.05))
        await asyncio.sleep(0.01)
        d.cancel_pending_off("lampa")
        await task
        assert local.offs == []

    async def test_other_device_unaffected(self, dispatch):
        d, local = dispatch
        local.devices_cfg["druga"] = {}
        task = asyncio.create_task(d.turn_on_for_duration("lampa", 0.05))
        await asyncio.sleep(0.01)
        d.cancel_pending_off("druga")
        await task
        assert local.offs == ["lampa"]


class TestBackendsHonourStillValid:
    async def test_local_skips_turn_off(self, local_controller):
        device = FlakyDevice(fail_times=0)
        local_controller._devices["gniazdko_salon"] = device
        await local_controller.turn_on_for_duration("gniazdko_salon", duration_seconds=0.01, still_valid=lambda: False)
        assert device.turned_on is True

    async def test_cloud_skips_turn_off(self, cloud_controller):
        fake = FakeTinytuyaCloud()
        _set_fake_client(cloud_controller, fake)
        calls = []

        async def on_state_change(is_on):
            calls.append(is_on)
        await cloud_controller.turn_on_for_duration(
            "lampa_taras", duration_seconds=0.01, on_state_change=on_state_change, still_valid=lambda: False,
        )
        assert [c[1]["commands"][0]["value"] for c in fake.sent_commands] == [True]
        assert calls == [True]


class TestEngineDetectsExternalChange:
    async def _states(self, engine, *values, own=False):
        for value in values:
            await engine.handle_device_state("tuya_state", "lampa", value, own=own)

    async def test_manual_change_cancels_timers(self, engine):
        await self._states(engine, "włączone", own=True)
        engine._own_change_ts.clear()
        await self._states(engine, "wyłączone")
        assert engine.tuya.cancelled == ["lampa"]

    async def test_own_change_does_not_cancel(self, engine):
        await self._states(engine, "włączone", "wyłączone", own=True)
        assert engine.tuya.cancelled == []

    async def test_echo_shortly_after_own_command_ignored(self, engine):
        await self._states(engine, "włączone", own=True)
        await self._states(engine, "wyłączone")
        assert engine.tuya.cancelled == []

    async def test_offline_transitions_do_not_cancel(self, engine):
        await self._states(engine, "włączone", own=True)
        engine._own_change_ts.clear()
        await self._states(engine, "poza siecią")
        await self._states(engine, "włączone")
        assert engine.tuya.cancelled == []

    async def test_grace_constant_is_reasonable(self):
        assert 3 <= rules_mod.OWN_ECHO_GRACE_SECONDS <= 30


class TestRetriggerConfig:
    def test_build_keeps_only_parallel(self):
        node = {"id": "t", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 1}, "wires": [[]]}
        flows = build_flows([
            {"name": "a", "nodes": [node], "retrigger": "parallel"},
            {"name": "b", "nodes": [node], "retrigger": "restart"},
            {"name": "c", "nodes": [node]},
        ])
        assert flows[0]["retrigger"] == "parallel"
        assert "retrigger" not in flows[1] and "retrigger" not in flows[2]

    def test_validate_rejects_unknown_value(self):
        node = {"id": "t", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 1}, "wires": [[]]}
        errors = validate_flows([{"name": "a", "nodes": [node], "retrigger": "zle"}], set())
        assert any("retrigger" in e for e in errors)
        assert not any("retrigger" in e for e in validate_flows([{"name": "a", "nodes": [node], "retrigger": "parallel"}], set()))
