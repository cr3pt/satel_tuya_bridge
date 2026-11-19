# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from bridge import energy_meters


class TestMarkAndUnmark:
    def test_marked_sensor_is_reported_as_energy_meter(self, history):
        energy_meters.mark_energy_meter(history, "esphome_gama350_power", "Gama 350: Moc")
        assert energy_meters.is_energy_meter(history, "esphome_gama350_power") is True

    def test_unmarked_sensor_is_not_an_energy_meter(self, history):
        assert energy_meters.is_energy_meter(history, "nigdy_nie_oznaczony") is False

    def test_unmark_removes_the_flag(self, history):
        energy_meters.mark_energy_meter(history, "wmbus_dom_total_energy_consumption_kwh", "Dom")
        energy_meters.unmark_energy_meter(history, "wmbus_dom_total_energy_consumption_kwh")
        assert energy_meters.is_energy_meter(history, "wmbus_dom_total_energy_consumption_kwh") is False

    def test_unmark_on_never_marked_sensor_is_a_noop(self, history):
        energy_meters.unmark_energy_meter(history, "cos_co_nigdy_nie_istnialo")


class TestListEnergyMeterSensors:
    def test_lists_all_marked_sensors_with_labels(self, history):
        energy_meters.mark_energy_meter(history, "esphome_gama350_power", "Gama 350: Moc")
        energy_meters.mark_energy_meter(history, "wmbus_dom_total_energy_consumption_kwh", "Dom: zużycie")
        result = {d["sensor"]: d["label"] for d in energy_meters.list_energy_meter_sensors(history)}
        assert result == {
            "esphome_gama350_power": "Gama 350: Moc",
            "wmbus_dom_total_energy_consumption_kwh": "Dom: zużycie",
        }

    def test_does_not_leak_other_status_categories(self, history):
        history.set_status("esphome:gama350:power", "esphome", "123.0", label="Gama 350: Moc")
        assert energy_meters.list_energy_meter_sensors(history) == []


class TestPermanentEnergyReadings:
    def test_readings_of_registered_sensor_are_copied_to_energy_table(self, history):
        history.set_energy_sensors({"licznik"})
        history.write_reading("licznik", 100.0, ts=1000.0)
        history.write_reading("inny", 5.0, ts=1000.0)
        assert history.get_energy_history("licznik", 0, 2000) == [{"t": 1000.0, "v": 100.0}]
        assert history.get_energy_sensors() == ["licznik"]

    def test_registering_backfills_readings_collected_before(self, history):
        history.write_reading("licznik", 100.0, ts=1000.0)
        history.write_reading("licznik", 101.5, ts=2000.0)
        history.set_energy_sensors({"licznik"})
        history.set_energy_sensors({"licznik"})
        assert [r["v"] for r in history.get_energy_history("licznik", 0, 3000)] == [100.0, 101.5]

    def test_unregistering_keeps_collected_readings_but_stops_copying(self, history):
        history.set_energy_sensors({"licznik"})
        history.write_reading("licznik", 100.0, ts=1000.0)
        history.set_energy_sensors(set())
        history.write_reading("licznik", 101.0, ts=2000.0)
        assert [r["v"] for r in history.get_energy_history("licznik", 0, 3000)] == [100.0]

    def test_never_registered_sensor_falls_back_to_regular_history(self, history):
        history.write_reading("zwykly", 3.0, ts=1000.0)
        assert history.get_energy_history("zwykly", 0, 2000) == [{"t": 1000.0, "v": 3.0}]

    def test_purge_of_old_data_does_not_touch_energy_readings(self, history):
        import time
        from bridge.maintenance import purge_old_data
        old = time.time() - 400 * 86400
        history.set_energy_sensors({"licznik"})
        history.write_reading("licznik", 100.0, ts=old)
        history.write_reading("licznik", 150.0, ts=time.time())
        purge_old_data(history.db_path, retention_days=90)
        assert len(history.get_history("licznik", 0, time.time() + 1)) == 1
        assert [r["v"] for r in history.get_energy_history("licznik", 0, time.time() + 1)] == [100.0, 150.0]


class TestSyncEnergySensors:
    def test_registers_flagged_and_configured_meter_sensors(self, history):
        energy_meters.mark_energy_meter(history, "esphome_gama350_total", "Gama")
        energy_cfg = {"meters": [
            {"name": "a", "sensor": "wmbus_dom_total_kwh"},
            {"name": "b", "zone_sensors": {"dzienna": "s_dzien", "nocna": "s_noc"}},
        ]}
        energy_meters.sync_energy_sensors(history, energy_cfg)
        assert history.get_energy_sensors() == sorted(
            ["esphome_gama350_total", "wmbus_dom_total_kwh", "s_dzien", "s_noc"])

    def test_no_energy_config_registers_only_flagged(self, history):
        energy_meters.mark_energy_meter(history, "esphome_gama350_total", "Gama")
        energy_meters.sync_energy_sensors(history, None)
        assert history.get_energy_sensors() == ["esphome_gama350_total"]
