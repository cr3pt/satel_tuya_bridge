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
