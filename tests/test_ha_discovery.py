from bridge.ha_discovery import (
    build_all_entities,
    build_boiler_sensor_entities,
    build_satel_input_entities,
    build_satel_output_entities,
    build_satel_partition_entities,
    build_tuya_switch_entities,
)

HA_CFG = {"discovery_prefix": "homeassistant", "device_name": "Test Bridge"}


class TestBoilerSensorEntities:
    def test_only_mqtt_publish_sensors_included(self):
        sensors = [
            {"name": "temp_a", "label": "Temp A", "targets": {"mqtt_publish": True}},
            {"name": "temp_b", "label": "Temp B", "targets": {"mqtt_publish": False}},
        ]
        entities = build_boiler_sensor_entities(sensors, HA_CFG, "home/boiler")
        assert len(entities) == 1
        assert entities[0]["payload"]["state_topic"] == "home/boiler/temp_a"

    def test_temperature_sensor_gets_device_class(self):
        sensors = [{"name": "kociol_temp_zasilania", "label": "Temperatura", "targets": {"mqtt_publish": True}}]
        entities = build_boiler_sensor_entities(sensors, HA_CFG, "home/boiler")
        assert entities[0]["payload"]["device_class"] == "temperature"
        assert entities[0]["payload"]["unit_of_measurement"] == "°C"

    def test_non_temperature_sensor_has_no_device_class(self):
        sensors = [{"name": "status_palnika", "label": "Status palnika", "targets": {"mqtt_publish": True}}]
        entities = build_boiler_sensor_entities(sensors, HA_CFG, "home/boiler")
        assert "device_class" not in entities[0]["payload"]

    def test_discovery_topic_format(self):
        sensors = [{"name": "temp_a", "label": "A", "targets": {"mqtt_publish": True}}]
        entities = build_boiler_sensor_entities(sensors, HA_CFG, "home/boiler")
        assert entities[0]["discovery_topic"] == "homeassistant/sensor/satel_tuya_bridge/boiler_temp_a/config"

    def test_unique_ids_are_unique(self):
        sensors = [
            {"name": "a", "label": "A", "targets": {"mqtt_publish": True}},
            {"name": "b", "label": "B", "targets": {"mqtt_publish": True}},
        ]
        entities = build_boiler_sensor_entities(sensors, HA_CFG, "home/boiler")
        ids = [e["payload"]["unique_id"] for e in entities]
        assert len(ids) == len(set(ids))


class TestTuyaSwitchEntities:
    def test_creates_switch_with_command_topic(self):
        devices = {"gniazdko_salon": {"id": "x", "ip": "1.2.3.4", "local_key": "k"}}
        entities = build_tuya_switch_entities(devices, HA_CFG, "home/bridge")
        assert len(entities) == 1
        assert entities[0]["command_topic"] == "home/bridge/tuya/gniazdko_salon/set"
        assert entities[0]["state_topic"] == "home/bridge/tuya/gniazdko_salon/state"
        assert entities[0]["payload"]["command_topic"] is not None

    def test_multiple_devices(self):
        devices = {"a": {}, "b": {}, "c": {}}
        entities = build_tuya_switch_entities(devices, HA_CFG, "home/bridge")
        assert len(entities) == 3


class TestSatelInputEntities:
    def test_input_is_read_only_binary_sensor(self):
        zones = {"Salon": 12}
        entities = build_satel_input_entities(zones, HA_CFG, "home/bridge")
        assert entities[0]["command_topic"] is None
        assert entities[0]["payload"]["device_class"] == "motion"

    def test_discovery_topic_uses_binary_sensor_component(self):
        entities = build_satel_input_entities({"Salon": 12}, HA_CFG, "home/bridge")
        assert "/binary_sensor/" in entities[0]["discovery_topic"]


class TestSatelOutputEntities:
    def test_output_is_controllable_switch(self):
        outputs = {"Syrena": 1}
        entities = build_satel_output_entities(outputs, HA_CFG, "home/bridge")
        assert entities[0]["command_topic"] == "home/bridge/satel/output/1/set"
        assert "/switch/" in entities[0]["discovery_topic"]


class TestSatelPartitionEntities:
    def test_partition_is_read_only_never_controllable(self):
        partitions = {"Dom": 1, "Garaż": 2}
        entities = build_satel_partition_entities(partitions, HA_CFG, "home/bridge")
        assert len(entities) == 2
        for entity in entities:
            assert entity["command_topic"] is None
            assert "command_topic" not in entity["payload"]

    def test_partition_uses_safety_device_class(self):
        entities = build_satel_partition_entities({"Dom": 1}, HA_CFG, "home/bridge")
        assert entities[0]["payload"]["device_class"] == "safety"


class TestBuildAllEntities:
    def test_aggregates_all_sources(self):
        class FakeConfig:
            raw = {"home_assistant": HA_CFG}
            ebus = {"sensors": [{"name": "t", "label": "T", "targets": {"mqtt_publish": True}}]}
            tuya = {"devices": {"gniazdko": {}}}
            satel = {
                "monitored_zones": {"Salon": 12},
                "monitored_outputs": {"Syrena": 1},
                "partitions": {"Dom": 1},
            }

        entities = build_all_entities(FakeConfig(), "home/boiler")
        assert len(entities) == 5

    def test_all_discovery_topics_unique(self):
        class FakeConfig:
            raw = {"home_assistant": HA_CFG}
            ebus = {"sensors": []}
            tuya = {"devices": {"a": {}, "b": {}}}
            satel = {"monitored_zones": {"X": 1}, "monitored_outputs": {"Y": 2}, "partitions": {"Z": 3}}

        entities = build_all_entities(FakeConfig(), "home/boiler")
        topics = [e["discovery_topic"] for e in entities]
        assert len(topics) == len(set(topics))
