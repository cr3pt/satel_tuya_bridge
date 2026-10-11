# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio

import pytest

from bridge.flow_engine import FlowEngine


class FakeMqttClient:
    def __init__(self):
        self.published: list[tuple] = []
        self.subscribed: list[str] = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))

    def subscribe(self, topic):
        self.subscribed.append(topic)


class FakeMqttMessage:
    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


class FakeAiohttpResponse:
    def __init__(self, text: str):
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeAiohttpSession:
    def __init__(self, response_text="ok", capture=None):
        self._response_text = response_text
        self._capture = capture if capture is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def request(self, method, url, data=None, timeout=None):
        self._capture.append((method, url, data))
        return FakeAiohttpResponse(self._response_text)


def _engine(flows=None, **kwargs):
    calls = {"set_target_state": [], "run_scene": []}

    async def set_target_state(target_system, device, on, dimmer=None, reason=None):
        calls["set_target_state"].append((target_system, device, on, dimmer))
        calls.setdefault("reasons", []).append(reason)

    async def run_scene(name):
        calls["run_scene"].append(name)

    engine = FlowEngine(
        flows or [], set_target_state=set_target_state, run_scene=run_scene,
        notify_cfg=None, **kwargs,
    )
    return engine, calls


def _trigger_flow(target_wires=("cond",), extra_nodes=None, condition=None):
    nodes = [
        {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 1}, "wires": [list(target_wires)]},
    ]
    if condition is not None:
        nodes.append(condition)
    if extra_nodes:
        nodes.extend(extra_nodes)
    return {"id": "f1", "name": "test-flow", "enabled": True, "nodes": nodes}


class TestConditionNode:
    async def test_passes_when_matched(self):
        flow = _trigger_flow(
            target_wires=("cond",),
            condition={"id": "cond", "type": "condition", "config": {"op": "==", "value": "True"}, "wires": [["dev"], []]},
            extra_nodes=[{"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []}],
        )
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 1, trigger="violated")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_else_branch_when_not_matched(self):
        flow = _trigger_flow(
            target_wires=("cond",),
            condition={"id": "cond", "type": "condition", "config": {"op": "==", "value": "False"}, "wires": [["dev_then"], ["dev_else"]]},
            extra_nodes=[
                {"id": "dev_then", "type": "device", "config": {"target_device": "then_device", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "else_device", "value": "on"}, "wires": []},
            ],
        )
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 1, trigger="violated")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "else_device", True, None)]

    async def test_numeric_comparison(self):
        flow = _trigger_flow(
            target_wires=("cond",),
            condition={"id": "cond", "type": "condition", "config": {"op": ">", "value": "10"}, "wires": [["dev"], []]},
            extra_nodes=[{"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []}],
        )
        engine, calls = _engine([flow])
        flow["nodes"][0]["config"] = {"source": "tuya_state", "source_device": "sensor1", "trigger_value": "irrelevant", "op": "changes"}
        await engine.handle_device_state("tuya_state", "sensor1", "20")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]


class TestDeviceNode:
    async def test_turns_on_with_dimmer(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "target_system": "tuya", "value": "on", "dimmer_level_percent": 40}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5, trigger="violated")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, 40)]
        assert calls["reasons"] == ["flow 'f'"]

    async def test_turns_off(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5, "op": "restored"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "off"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5, trigger="restored")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", False, None)]

    async def test_toggle_value_passes_none_for_current_state(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "toggle"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5, trigger="violated")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", None, None)]

    async def test_activates_satel_virtual_input(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "kamera", "target_system": "satel_virtual_input", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("satel_virtual_input", "kamera", True, None)]


class TestTuyaDpNode:
    async def test_calls_set_tuya_dp_with_target_device_code_and_value(self):
        calls = []

        async def set_tuya_dp(device, dp_code, value, reason=None):
            calls.append((device, dp_code, value))

        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dp"]]},
                {"id": "dp", "type": "tuya_dp", "config": {"target_device": "nawadnianie", "dp_code": "manual_delay", "value": "1"}, "wires": []},
            ],
        }
        engine, _ = _engine([flow], set_tuya_dp=set_tuya_dp)
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls == [("nawadnianie", "manual_delay", "1")]

    async def test_value_supports_value_template_from_triggering_message(self):
        calls = []

        async def set_tuya_dp(device, dp_code, value, reason=None):
            calls.append((device, dp_code, value))

        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "tuya_state", "source_device": "wilgotnosc", "op": "changes"}, "wires": [["dp"]]},
                {"id": "dp", "type": "tuya_dp", "config": {"target_device": "nawadnianie", "dp_code": "manual_delay", "value": "{value}"}, "wires": []},
            ],
        }
        engine, _ = _engine([flow], set_tuya_dp=set_tuya_dp)
        await engine.handle_device_state("tuya_state", "wilgotnosc", "80")
        await asyncio.sleep(0.01)
        assert calls == [("nawadnianie", "manual_delay", "80")]

    async def test_no_handler_configured_does_not_crash(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dp"]]},
                {"id": "dp", "type": "tuya_dp", "config": {"target_device": "nawadnianie", "dp_code": "manual_delay", "value": "1"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []


class TestStopNode:
    async def test_has_no_outputs_and_does_not_crash(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["stop"]]},
                {"id": "stop", "type": "stop", "config": {}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []
        assert calls["run_scene"] == []

    async def test_stops_propagation_even_if_wires_configured(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["stop"]]},
                {"id": "stop", "type": "stop", "config": {}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []


class TestSceneNode:
    async def test_runs_named_scene(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["scn"]]},
                {"id": "scn", "type": "scene", "config": {"scene_name": "wykryto_intruza"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["run_scene"] == ["wykryto_intruza"]


class TestDelayNode:
    async def test_passes_through_after_delay(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dly"]]},
                {"id": "dly", "type": "delay", "config": {"seconds": 0.01}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        assert calls["set_target_state"] == []
        await asyncio.sleep(0.05)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]


class TestNotifyNode:
    async def test_sends_with_template_substitution(self, monkeypatch):
        sent = []

        async def fake_send(self, title, message, priority="default"):
            sent.append((title, message, priority))

        monkeypatch.setattr("bridge.flow_engine.Notifier.send", fake_send)
        flow = {
            "id": "f1", "name": "moja-flow", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "tuya_state", "source_device": "sensor1", "trigger_value": "alarm", "op": "changes"}, "wires": [["ntf"]]},
                {"id": "ntf", "type": "notify", "config": {"level": "critical", "message": "Wartość: {value}"}, "wires": []},
            ],
        }
        engine, _ = _engine([flow])
        await engine.handle_device_state("tuya_state", "sensor1", "alarm")
        await asyncio.sleep(0.01)
        assert len(sent) == 1
        title, message, priority = sent[0]
        assert "moja-flow" in title
        assert message == "Wartość: alarm"
        assert priority == "urgent"


class TestMqttOutNode:
    async def test_publishes_with_template(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["mo"]]},
                {"id": "mo", "type": "mqttOut", "config": {"topic": "home/test", "payload": "on"}, "wires": []},
            ],
        }
        engine, _ = _engine([flow])
        fake_client = FakeMqttClient()
        engine._ensure_mqtt_client = lambda: fake_client
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert fake_client.published == [("home/test", "on")]

    async def test_default_payload_is_trigger_value(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["mo"]]},
                {"id": "mo", "type": "mqttOut", "config": {"topic": "home/test"}, "wires": []},
            ],
        }
        engine, _ = _engine([flow])
        fake_client = FakeMqttClient()
        engine._ensure_mqtt_client = lambda: fake_client
        await engine.handle_motion("satel_zone", 5, trigger="violated")
        await asyncio.sleep(0.01)
        assert fake_client.published == [("home/test", "True")]


class TestMqttInNode:
    async def test_fires_on_matching_topic(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "mi", "type": "mqttIn", "config": {"topic": "home/in"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], mqtt_cfg={})
        engine.start()
        engine._on_mqtt_message(None, None, FakeMqttMessage("home/in", b"payload"))
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_ignores_unrelated_topic(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "mi", "type": "mqttIn", "config": {"topic": "home/in"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], mqtt_cfg={})
        engine.start()
        engine._on_mqtt_message(None, None, FakeMqttMessage("home/other", b"payload"))
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []


class TestHttpNode:
    async def test_get_stores_response_as_payload(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            "bridge.flow_engine.aiohttp.ClientSession",
            lambda: FakeAiohttpSession(response_text="hello world", capture=captured),
        )
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["http"]]},
                {"id": "http", "type": "http", "config": {"method": "GET", "url": "http://example.test"}, "wires": [["mo"]]},
                {"id": "mo", "type": "mqttOut", "config": {"topic": "home/http-result"}, "wires": []},
            ],
        }
        engine, _ = _engine([flow])
        fake_client = FakeMqttClient()
        engine._ensure_mqtt_client = lambda: fake_client
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert captured == [("GET", "http://example.test", None)]
        assert fake_client.published == [("home/http-result", "hello world")]

    async def test_failure_is_logged_and_notified_not_raised(self, monkeypatch):
        sent = []

        async def fake_send(self, title, message, priority="default"):
            sent.append((title, message))

        monkeypatch.setattr("bridge.flow_engine.Notifier.send", fake_send)

        class FailingSession(FakeAiohttpSession):
            def request(self, method, url, data=None, timeout=None):
                raise ConnectionError("boom")

        monkeypatch.setattr("bridge.flow_engine.aiohttp.ClientSession", lambda: FailingSession())
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["http"]]},
                {"id": "http", "type": "http", "config": {"method": "GET", "url": "http://example.test"}, "wires": []},
            ],
        }
        engine, _ = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert len(sent) == 1
        assert "http" in sent[0][1]


class TestDisabledFlow:
    async def test_disabled_flow_does_not_fire(self):
        flow = {
            "id": "f1", "name": "f", "enabled": False,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []


class TestUnmatchedTrigger:
    async def test_wrong_zone_does_not_fire(self):
        flow = _trigger_flow()
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 999)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []


class TestDeviceStateTriggerDedup:
    async def test_changes_op_fires_every_update(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "tuya_state", "source_device": "s1", "op": "changes"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_device_state("tuya_state", "s1", "a")
        await engine.handle_device_state("tuya_state", "s1", "b")
        await asyncio.sleep(0.01)
        assert len(calls["set_target_state"]) == 2

    async def test_edge_triggered_op_does_not_refire_on_duplicate(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "tuya_state", "source_device": "s1", "trigger_value": "on", "op": "violated"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_device_state("tuya_state", "s1", "on")
        await asyncio.sleep(0.01)
        await engine.handle_device_state("tuya_state", "s1", "off")
        await engine.handle_device_state("tuya_state", "s1", "on")
        await asyncio.sleep(0.01)
        assert len(calls["set_target_state"]) == 2


class TestDepthLimit:
    async def test_self_loop_stops_without_crashing(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["loop"]]},
                {"id": "loop", "type": "trigger", "config": {}, "wires": [["loop"]]},
            ],
        }
        engine, _ = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.2)


class TestRunFlow:
    async def test_manually_runs_all_entry_nodes(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.run_flow("f1")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_unknown_flow_id_raises(self):
        engine, _ = _engine([])
        with pytest.raises(ValueError):
            await engine.run_flow("nope")

    async def test_manually_runs_entry_condition_group(self):
        condition = {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"}
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        values = {"tuya:garaz": "zamknięte"}
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))
        await engine.run_flow("f1")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_manual_run_of_condition_group_reflects_real_state_not_forced_true(self):
        condition = {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"}
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        values = {"tuya:garaz": "otwarte"}
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))
        await engine.run_flow("f1")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "inna", True, None)]

    async def test_condition_group_with_incoming_wire_not_run_manually(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["cg"]]},
                {"id": "cg", "type": "condition_group", "config": {"condition": {"type": "state", "key": "k", "equals": "v"}}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "v")
        await engine.run_flow("f1")
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]


class TestStateNode:
    async def test_passes_when_status_matches(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "satel_input:9", "op": "equals", "value": "naruszone"}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "naruszone" if key == "satel_input:9" else None)
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_else_branch_when_status_does_not_match(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "satel_input:9", "op": "equals", "value": "naruszone"}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "OK")
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "inna", True, None)]

    async def test_not_equals_operator(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "tuya:x", "op": "not_equals", "value": "wyłączone"}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "włączone")
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_no_get_status_value_provided_defaults_to_none(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "satel_input:9", "op": "equals", "value": "naruszone"}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []

    async def test_gt_operator_passes_above_threshold(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "boiler:temp", "op": "gt", "value": "20"}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "25.6")
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_lt_operator_else_branch_when_not_below_threshold(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "boiler:temp", "op": "lt", "value": "20"}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "25.6")
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "inna", True, None)]

    async def test_gte_operator_equal_value_passes(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "boiler:temp", "op": "gte", "value": "20"}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "20")
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_lte_operator_equal_value_passes(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "boiler:temp", "op": "lte", "value": "20"}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "20")
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_gt_operator_non_numeric_status_treated_as_not_passed(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["st"]]},
                {"id": "st", "type": "state", "config": {"key": "satel_input:9", "op": "gt", "value": "20"}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], get_status_value=lambda key: "naruszone")
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "inna", True, None)]


class TestConditionGroupNode:
    async def test_and_group_passes_when_all_leaves_match(self):
        condition = {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
                {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"},
            ],
        }
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["cg"]]},
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        values = {"satel_input:9": "naruszone", "tuya:garaz": "zamknięte"}
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_and_group_else_branch_when_one_leaf_fails(self):
        condition = {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
                {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"},
            ],
        }
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["cg"]]},
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        values = {"satel_input:9": "naruszone", "tuya:garaz": "otwarte"}
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "inna", True, None)]

    async def test_nested_or_inside_and(self):
        condition = {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "tuya:swiatlo", "equals": "włączone"},
                {
                    "type": "or",
                    "conditions": [
                        {"type": "state", "key": "satel_input:1", "equals": "naruszone"},
                        {"type": "state", "key": "satel_input:2", "equals": "naruszone"},
                    ],
                },
            ],
        }
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["cg"]]},
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "off"}, "wires": []},
            ],
        }
        values = {"tuya:swiatlo": "włączone", "satel_input:1": "OK", "satel_input:2": "naruszone"}
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", False, None)]

    async def test_evaluation_error_treated_as_not_passed(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["cg"]]},
                {"id": "cg", "type": "condition_group", "config": {"condition": {"type": "nieznany_typ"}}, "wires": [["dev"], ["dev_else"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "dev_else", "type": "device", "config": {"target_device": "inna", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "inna", True, None)]


class TestConditionGroupAsTrigger:
    """"Warunek złożony" bez podłączonego wejścia (nikt do niego nie
    prowadzi przewodem w tym Flow) działa jak trigger - odpytywany
    samodzielnie przy każdym handle_motion/handle_device_state, odpala flow
    na zboczu niespełniony -> spełniony, nie ponownie dopóki trwa."""

    def _entry_flow(self, condition, values):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        return flow

    async def test_fires_on_transition_to_true(self):
        condition = {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "satel_input:9", "equals": "naruszone"},
                {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"},
            ],
        }
        values = {"satel_input:9": "OK", "tuya:garaz": "zamknięte"}
        flow = self._entry_flow(condition, values)
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))

        await engine.handle_motion("satel_zone", 99)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []

        values["satel_input:9"] = "naruszone"
        await engine.handle_motion("satel_zone", 99)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]

    async def test_does_not_refire_while_condition_stays_true(self):
        condition = {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"}
        values = {"tuya:garaz": "zamknięte"}
        flow = self._entry_flow(condition, values)
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))

        await engine.handle_device_state("tuya_state", "unrelated", "x")
        await engine.handle_device_state("tuya_state", "unrelated", "y")
        await asyncio.sleep(0.01)
        assert len(calls["set_target_state"]) == 1

    async def test_refires_after_condition_goes_false_and_true_again(self):
        condition = {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"}
        values = {"tuya:garaz": "otwarte"}
        flow = self._entry_flow(condition, values)
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))

        values["tuya:garaz"] = "zamknięte"
        await engine.handle_device_state("tuya_state", "x", "y")
        await asyncio.sleep(0.01)
        values["tuya:garaz"] = "otwarte"
        await engine.handle_device_state("tuya_state", "x", "y")
        await asyncio.sleep(0.01)
        values["tuya:garaz"] = "zamknięte"
        await engine.handle_device_state("tuya_state", "x", "y")
        await asyncio.sleep(0.01)
        assert len(calls["set_target_state"]) == 2

    async def test_node_fed_by_an_incoming_wire_is_not_independently_polled(self):
        condition = {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"}
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["cg"]]},
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        values = {"tuya:garaz": "zamknięte"}
        engine, calls = _engine([flow], get_status_value=lambda key: values.get(key))

        await engine.handle_motion("satel_zone", 99)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []

        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]


class TestScheduleNode:
    async def test_passes_through_when_within_window(self, monkeypatch):
        captured = []

        def fake_is_within_window(window, location_cfg, now):
            captured.append((window, location_cfg))
            return True

        monkeypatch.setattr("bridge.flow_engine.is_within_window", fake_is_within_window)
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["sched"]]},
                {"id": "sched", "type": "schedule", "config": {"start": "22:00", "end": "06:00"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow], location_cfg={"latitude": 1, "longitude": 2})
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]
        assert captured == [({"start": "22:00", "end": "06:00"}, {"latitude": 1, "longitude": 2})]

    async def test_drops_message_when_outside_window(self, monkeypatch):
        monkeypatch.setattr("bridge.flow_engine.is_within_window", lambda window, location_cfg, now: False)
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["sched"]]},
                {"id": "sched", "type": "schedule", "config": {"start": "22:00", "end": "06:00"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == []


class TestEscalationNode:
    def _flow(self, threshold_count=2, window_seconds=60):
        return {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["esc"]]},
                {
                    "id": "esc", "type": "escalation",
                    "config": {"threshold_count": threshold_count, "window_seconds": window_seconds},
                    "wires": [["dev_normal"], ["scn_escalated"]],
                },
                {"id": "dev_normal", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "scn_escalated", "type": "scene", "config": {"scene_name": "wykryto_intruza"}, "wires": []},
            ],
        }

    async def test_below_threshold_takes_normal_path(self):
        engine, calls = _engine([self._flow(threshold_count=3)])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["set_target_state"] == [("tuya", "lampa", True, None)]
        assert calls["run_scene"] == []

    async def test_threshold_reached_takes_escalation_path_and_resets(self):
        engine, calls = _engine([self._flow(threshold_count=2)])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["run_scene"] == ["wykryto_intruza"]
        assert len(calls["set_target_state"]) == 1

        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert len(calls["set_target_state"]) == 2
        assert calls["run_scene"] == ["wykryto_intruza"]

    async def test_window_seconds_prunes_old_entries(self):
        engine, calls = _engine([self._flow(threshold_count=2, window_seconds=0.05)])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.1)
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["run_scene"] == []
        assert len(calls["set_target_state"]) == 2


class TestUpdateFlows:
    async def test_replaces_flows_and_resets_trigger_state(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "tuya_state", "source_device": "s1", "trigger_value": "on", "op": "violated"}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_device_state("tuya_state", "s1", "on")
        await asyncio.sleep(0.01)
        assert len(calls["set_target_state"]) == 1

        engine.update_flows([flow])
        await engine.handle_device_state("tuya_state", "s1", "off")
        await engine.handle_device_state("tuya_state", "s1", "on")
        await asyncio.sleep(0.01)
        assert len(calls["set_target_state"]) == 2

    async def test_resets_escalation_history(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["esc"]]},
                {
                    "id": "esc", "type": "escalation", "config": {"threshold_count": 2, "window_seconds": 60},
                    "wires": [["dev_normal"], ["scn_escalated"]],
                },
                {"id": "dev_normal", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
                {"id": "scn_escalated", "type": "scene", "config": {"scene_name": "wykryto_intruza"}, "wires": []},
            ],
        }
        engine, calls = _engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)

        engine.update_flows([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)
        assert calls["run_scene"] == []


class TestFlowEventLogging:
    """FlowEngine wcześniej nie zapisywał żadnych zdarzeń do Historii zdarzeń
    (w odróżnieniu od reguł ruchu/scen) - nie dało się zobaczyć w panelu, KIEDY
    i CZY dany flow w ogóle się uruchomił/wykonał akcję. Te testy pokrywają
    nowe wywołania write_event dla odpalenia triggera i każdej akcji."""

    def _events_engine(self, flows):
        events = []

        def write_event(event_type, source, detail):
            events.append((event_type, source, detail))

        engine, calls = _engine(flows, write_event=write_event)
        return engine, calls, events

    async def test_trigger_fire_logs_flow_event(self):
        flow = {
            "id": "f1", "name": "moja-flow", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])
        await engine.handle_motion("satel_zone", 5, trigger="violated")
        await asyncio.sleep(0.01)

        trigger_events = [e for e in events if e[2].get("node_type") == "trigger"]
        assert len(trigger_events) == 1
        event_type, source, detail = trigger_events[0]
        assert event_type == "flow"
        assert source == "flow:moja-flow"
        assert detail["node_id"] == "trig"

    async def test_device_action_logs_flow_event(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "target_system": "tuya", "value": "on"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)

        device_events = [e for e in events if e[2].get("node") == "device"]
        assert len(device_events) == 1
        assert device_events[0][2] == {
            "node": "device", "target_device": "lampa", "target_system": "tuya", "value": "on",
        }

    async def test_scene_action_logs_flow_event(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["scn"]]},
                {"id": "scn", "type": "scene", "config": {"scene_name": "wykryto_intruza"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)

        scene_events = [e for e in events if e[2].get("node") == "scene"]
        assert scene_events == [("flow", "flow:f", {"node": "scene", "scene": "wykryto_intruza"})]

    async def test_tuya_dp_action_logs_flow_event(self):
        async def set_tuya_dp(device, dp_code, value, reason=None):
            pass

        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dp"]]},
                {"id": "dp", "type": "tuya_dp", "config": {"target_device": "nawadnianie", "dp_code": "manual_delay", "value": "1"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])
        engine._set_tuya_dp = set_tuya_dp
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)

        dp_events = [e for e in events if e[2].get("node") == "tuya_dp"]
        assert dp_events == [
            ("flow", "flow:f", {"node": "tuya_dp", "target_device": "nawadnianie", "dp_code": "manual_delay", "value": "1"}),
        ]

    async def test_mqtt_out_action_logs_flow_event(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["mo"]]},
                {"id": "mo", "type": "mqttOut", "config": {"topic": "home/test", "payload": "on"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])
        engine._ensure_mqtt_client = lambda: FakeMqttClient()
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)

        mqtt_events = [e for e in events if e[2].get("node") == "mqtt_out"]
        assert mqtt_events == [("flow", "flow:f", {"node": "mqtt_out", "topic": "home/test", "payload": "on"})]

    async def test_notify_action_logs_flow_event(self, monkeypatch):
        async def fake_send(self, title, message, priority="default"):
            pass

        monkeypatch.setattr("bridge.flow_engine.Notifier.send", fake_send)
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["ntf"]]},
                {"id": "ntf", "type": "notify", "config": {"level": "warning", "message": "uwaga"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])
        await engine.handle_motion("satel_zone", 5)
        await asyncio.sleep(0.01)

        notify_events = [e for e in events if e[2].get("node") == "notify"]
        assert notify_events == [("flow", "flow:f", {"node": "notify", "level": "warning", "message": "uwaga"})]

    async def test_condition_group_entrypoint_fire_logs_flow_event(self):
        condition = {"type": "state", "key": "tuya:garaz", "equals": "zamknięte"}
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "cg", "type": "condition_group", "config": {"condition": condition}, "wires": [["dev"], []]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        values = {"tuya:garaz": "otwarte"}
        engine, _calls, events = self._events_engine([flow])
        engine._get_status_value = lambda key: values.get(key)
        await engine.handle_motion("satel_zone", 99)
        assert not events

        values["tuya:garaz"] = "zamknięte"
        await engine.handle_motion("satel_zone", 99)
        await asyncio.sleep(0.01)

        cg_events = [e for e in events if e[2].get("node_type") == "condition_group"]
        assert len(cg_events) == 1
        assert cg_events[0][1] == "flow:f"

    async def test_node_failure_logs_flow_event_with_error(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["http"]]},
                {"id": "http", "type": "http", "config": {"method": "GET", "url": "http://example.invalid"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])

        class FailingSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def request(self, *a, **kw):
                raise RuntimeError("boom")

        import bridge.flow_engine as flow_engine_module
        original = flow_engine_module.aiohttp.ClientSession
        flow_engine_module.aiohttp.ClientSession = lambda: FailingSession()
        try:
            await engine.handle_motion("satel_zone", 5)
            await asyncio.sleep(0.01)
        finally:
            flow_engine_module.aiohttp.ClientSession = original

        error_events = [e for e in events if "error" in e[2]]
        assert len(error_events) == 1
        assert error_events[0][2]["node_type"] == "http"

    async def test_run_flow_manual_test_logs_flow_event(self):
        flow = {
            "id": "f1", "name": "f", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, _calls, events = self._events_engine([flow])
        await engine.run_flow("f1")
        await asyncio.sleep(0.01)

        trigger_events = [e for e in events if e[2].get("node_type") == "trigger"]
        assert len(trigger_events) == 1
        assert trigger_events[0][2]["source"] == "flow-test:f"


class TestFlowDebugLogging:
    """Zgłoszony problem: logger 'bridge.flow_engine' nigdy nie pojawiał się na
    liście loggerów w Historii logów (⚙ Ustawienia ogólne), bo silnik Flow
    logował dotąd WYŁĄCZNIE błędy - nie dawało się włączyć DEBUG i zobaczyć
    krok-po-kroku, który węzeł się wykonał i z jakim wynikiem. Te testy
    pokrywają nowe logi DEBUG (osobne od Historii zdarzeń/write_event)."""

    async def test_handle_motion_logs_debug_entry(self, caplog):
        engine, _calls = _engine([])
        with caplog.at_level("DEBUG", logger="bridge.flow_engine"):
            await engine.handle_motion("satel_zone", 5, trigger="violated")
        assert "handle_motion" in caplog.text
        assert "satel_zone" in caplog.text

    async def test_handle_device_state_logs_debug_entry(self, caplog):
        engine, _calls = _engine([])
        with caplog.at_level("DEBUG", logger="bridge.flow_engine"):
            await engine.handle_device_state("tuya_state", "gniazdko", "on")
        assert "handle_device_state" in caplog.text
        assert "gniazdko" in caplog.text

    async def test_node_execution_logs_debug_with_outputs(self, caplog):
        flow = {
            "id": "f1", "name": "moja-flow", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [["dev"]]},
                {"id": "dev", "type": "device", "config": {"target_device": "lampa", "value": "on"}, "wires": []},
            ],
        }
        engine, _calls = _engine([flow])
        with caplog.at_level("DEBUG", logger="bridge.flow_engine"):
            await engine.handle_motion("satel_zone", 5, trigger="violated")
            await asyncio.sleep(0.01)

        assert "wykonuję węzeł 'trig' (trigger)" in caplog.text
        assert "wykonuję węzeł 'dev' (device)" in caplog.text
        assert "zwrócił wyjścia" in caplog.text

    async def test_unconnected_output_logs_debug_branch_end(self, caplog):
        flow = {
            "id": "f1", "name": "moja-flow", "enabled": True,
            "nodes": [
                {"id": "trig", "type": "trigger", "config": {"source": "satel_zone", "zone_number": 5}, "wires": [[]]},
            ],
        }
        engine, _calls = _engine([flow])
        with caplog.at_level("DEBUG", logger="bridge.flow_engine"):
            await engine.handle_motion("satel_zone", 5, trigger="violated")
            await asyncio.sleep(0.01)

        assert "niepodłączone" in caplog.text
