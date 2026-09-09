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

    async def set_target_state(target_system, device, on, dimmer=None):
        calls["set_target_state"].append((target_system, device, on, dimmer))

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
        # simulate a numeric payload via device_state handler directly
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

        async def set_tuya_dp(device, dp_code, value):
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

        async def set_tuya_dp(device, dp_code, value):
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
        engine.start()  # no host configured -> logs warning, but subs are still populated
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
        # No exception raised, and it terminated (test itself completing is the assertion).


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
