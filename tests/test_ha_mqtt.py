from bridge.ha_mqtt import HomeAssistantMqttBridge


def _bridge(history=None, entities=None):
    return HomeAssistantMqttBridge(
        {"mqtt_host": "127.0.0.1", "mqtt_port": 1883}, entities or [], history=history,
    )


class TestPublishStateRecordsTopic:

    async def test_publish_state_records_topic_in_history(self, history):
        bridge = _bridge(history=history)
        bridge.publish_state("home/bridge/tuya/gniazdko/state", "ON")
        entries = history.get_status("mqtt_topic")
        assert len(entries) == 1
        assert entries[0]["label"] == "home/bridge/tuya/gniazdko/state"
        assert entries[0]["value"] == "ON"

    async def test_publish_state_without_history_does_not_crash(self):
        bridge = _bridge(history=None)
        bridge.publish_state("home/bridge/tuya/gniazdko/state", "ON")

    async def test_publish_state_overwrites_previous_value_for_same_topic(self, history):
        bridge = _bridge(history=history)
        bridge.publish_state("t", "ON")
        bridge.publish_state("t", "OFF")
        entries = history.get_status("mqtt_topic")
        assert len(entries) == 1
        assert entries[0]["value"] == "OFF"


class TestPublishRaw:

    async def test_publish_raw_records_topic(self, history):
        bridge = _bridge(history=history)
        bridge.publish_raw("satel/partition/1/state", "ARMED", retain=True)
        entries = history.get_status("mqtt_topic")
        assert entries[0]["label"] == "satel/partition/1/state"
        assert entries[0]["value"] == "ARMED"


class TestDiscoveryPublishRecordsTopics:

    async def test_on_connect_records_each_entity_discovery_topic(self, history):
        entities = [
            {"discovery_topic": "homeassistant/switch/x/config", "payload": {"name": "x"}},
            {"discovery_topic": "homeassistant/binary_sensor/y/config", "payload": {"name": "y"}},
        ]
        bridge = _bridge(history=history, entities=entities)
        bridge._on_connect(bridge._client, None, {}, 0, None)
        entries = {e["label"]: e["value"] for e in history.get_status("mqtt_topic")}
        assert "homeassistant/switch/x/config" in entries
        assert "homeassistant/binary_sensor/y/config" in entries
        assert '"name": "x"' in entries["homeassistant/switch/x/config"]


class TestIncomingMessageRecordsTopic:

    async def test_on_message_records_topic_even_without_handler(self, history):
        bridge = _bridge(history=history)

        class FakeMsg:
            topic = "homeassistant/switch/x/set"
            payload = b"ON"

        bridge._on_message(bridge._client, None, FakeMsg())
        entries = history.get_status("mqtt_topic")
        assert entries[0]["label"] == "homeassistant/switch/x/set"
        assert entries[0]["value"] == "ON"
