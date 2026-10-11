# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from bridge.boiler_output import MqttBoilerPublisher


def _publisher(history=None):
    return MqttBoilerPublisher({"host": "127.0.0.1", "port": 1883}, history=history)


class TestMqttBoilerPublisherRecordsTopics:

    async def test_publish_records_topic_with_prefix(self, history):
        publisher = _publisher(history=history)
        publisher.publish("temp_kotla", 55.5)
        entries = history.get_status("mqtt_topic")
        assert len(entries) == 1
        assert entries[0]["label"] == "home/boiler/temp_kotla"
        assert entries[0]["value"] == "55.5"

    async def test_publish_without_history_does_not_crash(self):
        publisher = _publisher(history=None)
        publisher.publish("temp_kotla", 55.5)

    async def test_publish_raw_records_topic(self, history):
        publisher = _publisher(history=history)
        publisher.publish_raw("home/boiler/manual", "42", retain=True)
        entries = history.get_status("mqtt_topic")
        assert entries[0]["label"] == "home/boiler/manual"
        assert entries[0]["value"] == "42"
