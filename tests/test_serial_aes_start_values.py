# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause
"""Testy API: filtrowanie po numerze seryjnym, klucze AES i wartości startowe liczników."""
import os
import stat
import time

import pytest

from tests.test_plugin_routes import _basic_auth_header, client  # noqa: F401

AUTH = _basic_auth_header("test_haslo_123")

KEY_A = "00112233445566778899AABBCCDDEEFF"
KEY_B = "FFEEDDCCBBAA99887766554433221100"
METER_A = {"name": "prad_dom", "label": "Prąd dom", "id": "12345678", "driver": "auto", "key": KEY_A, "phases": 3}
METER_B = {"name": "woda", "label": "Woda", "id": "ABCDEF01", "driver": "auto", "key": KEY_B, "phases": 1}


async def _save_wmbus(client, meters):
    return await client.post("/api/plugins/wmbus/meters", json={"meters": meters}, headers=AUTH)


async def _get_json(client, url):
    resp = await client.get(url, headers=AUTH)
    assert resp.status_code == 200, url
    return await resp.get_json()


def _stored_wmbus_meters():
    import dashboard.app as app_module
    return app_module.config_store.get("config")["wmbus"]["meters"]


def _no_keys(text):
    lowered = text.lower()
    return KEY_A.lower() not in lowered and KEY_B.lower() not in lowered


class TestWmbusSerialFilter:

    async def test_meters_filtered_by_serial_case_insensitive(self, client):
        await _save_wmbus(client, [METER_A, METER_B])
        both = await _get_json(client, "/api/plugins/wmbus/meters")
        assert {m["name"] for m in both["meters"]} == {"prad_dom", "woda"}
        only_b = await _get_json(client, "/api/plugins/wmbus/meters?serial=abcdef01")
        assert [m["name"] for m in only_b["meters"]] == ["woda"]
        only_a = await _get_json(client, "/api/plugins/wmbus/meters?serial=12345678")
        assert [m["name"] for m in only_a["meters"]] == ["prad_dom"]

    async def test_unknown_serial_returns_no_meters(self, client):
        await _save_wmbus(client, [METER_A, METER_B])
        data = await _get_json(client, "/api/plugins/wmbus/meters?serial=00000000")
        assert data["meters"] == []

    async def test_readings_filtered_by_serial(self, client):
        import dashboard.app as app_module
        await _save_wmbus(client, [METER_A, METER_B])
        history = app_module.history
        history.set_status("wmbus:prad_dom:total_energy_consumption_kwh", "wmbus", "10", label="Prąd")
        history.set_status("wmbus:woda:total_m3", "wmbus", "20", label="Woda")
        everything = await _get_json(client, "/api/plugins/wmbus/readings")
        assert {r["key"] for r in everything} >= {"wmbus:prad_dom:total_energy_consumption_kwh", "wmbus:woda:total_m3"}
        only_b = await _get_json(client, "/api/plugins/wmbus/readings?serial=ABCDEF01")
        assert [r["key"] for r in only_b] == ["wmbus:woda:total_m3"]
        assert await _get_json(client, "/api/plugins/wmbus/readings?serial=00000000") == []

    async def test_meter_fields_requires_serial(self, client):
        resp = await client.get("/api/plugins/wmbus/meter_fields", headers=AUTH)
        assert resp.status_code == 400

    async def test_meter_fields_by_serial_hides_key(self, client):
        import dashboard.app as app_module
        await _save_wmbus(client, [METER_A, METER_B])
        app_module.history.write_reading("wmbus_woda_total_m3", 1.0, ts=time.time() - 120)
        resp = await client.get("/api/plugins/wmbus/meter_fields?serial=abcdef01", headers=AUTH)
        assert resp.status_code == 200
        assert _no_keys(await resp.get_data(as_text=True))
        data = await resp.get_json()
        assert data["serial"] == "ABCDEF01"
        assert data["meter"]["name"] == "woda" and "key" not in data["meter"] and data["meter"]["has_key"] is True
        assert [f["field"] for f in data["fields"]] == ["total_m3"]

    async def test_meter_fields_unknown_serial_has_no_meter(self, client):
        await _save_wmbus(client, [METER_A])
        data = await _get_json(client, "/api/plugins/wmbus/meter_fields?serial=00000000")
        assert data["meter"] is None and data["fields"] == []

    async def test_series_serial_takes_precedence_over_meter_name(self, client):
        import dashboard.app as app_module
        await _save_wmbus(client, [METER_A, METER_B])
        now = time.time()
        app_module.history.write_reading("wmbus_woda_total_m3", 1.0, ts=now - 120)
        app_module.history.write_reading("wmbus_prad_dom_voltage_at_phase_1_v", 230.0, ts=now - 120)
        data = await _get_json(client, "/api/plugins/wmbus/series?meter=prad_dom&serial=abcdef01&hours=1")
        assert data["meter"] == "woda" and data["serial"] == "ABCDEF01"
        assert [s["field"] for s in data["series"]] == ["total_m3"]

    async def test_series_unknown_serial_is_404_even_with_valid_name(self, client):
        await _save_wmbus(client, [METER_A])
        resp = await client.get("/api/plugins/wmbus/series?meter=prad_dom&serial=00000000", headers=AUTH)
        assert resp.status_code == 404


class TestAesKeysThroughApi:

    async def test_rename_with_same_serial_keeps_key_and_regenerates_files(self, client):
        import dashboard.app as app_module
        await _save_wmbus(client, [METER_A, METER_B])
        resp = await _save_wmbus(client, [{**METER_A, "name": "prad_dom2", "key": ""}, {**METER_B, "key": ""}])
        assert resp.status_code == 200 and _no_keys(await resp.get_data(as_text=True))
        stored = {m["id"]: m for m in _stored_wmbus_meters()}
        assert stored["12345678"]["name"] == "prad_dom2" and stored["12345678"]["key"] == KEY_A
        assert stored["ABCDEF01"]["key"] == KEY_B
        directory = app_module.config_path.parent / "wmbusmeters_conf/etc/wmbusmeters.d"
        assert not (directory / "prad_dom").exists()
        assert f"key={KEY_A}" in (directory / "prad_dom2").read_text()
        assert stat.S_IMODE(os.stat(directory / "prad_dom2").st_mode) == 0o600

    async def test_changed_serial_does_not_inherit_key_from_same_name(self, client):
        import dashboard.app as app_module
        await _save_wmbus(client, [METER_A])
        resp = await _save_wmbus(client, [{**METER_A, "id": "99999999", "key": ""}])
        assert resp.status_code == 200
        [meter] = _stored_wmbus_meters()
        assert meter["id"] == "99999999" and meter["key"] == ""
        conf = app_module.config_path.parent / "wmbusmeters_conf/etc/wmbusmeters.d/prad_dom"
        assert "key=NOKEY" in conf.read_text() and KEY_A not in conf.read_text()

    async def test_serial_case_does_not_matter_for_key_inheritance(self, client):
        await _save_wmbus(client, [METER_B])
        await _save_wmbus(client, [{**METER_B, "id": "abcdef01", "key": ""}])
        [meter] = _stored_wmbus_meters()
        assert meter["key"] == KEY_B

    async def test_duplicate_serial_is_rejected_without_leaking_keys(self, client):
        await _save_wmbus(client, [METER_A, METER_B])
        resp = await _save_wmbus(client, [METER_A, {**METER_B, "id": "12345678"}])
        assert resp.status_code == 400
        assert _no_keys(await resp.get_data(as_text=True))
        assert {m["id"] for m in _stored_wmbus_meters()} == {"12345678", "ABCDEF01"}

    async def test_no_get_endpoint_ever_returns_a_key(self, client):
        import dashboard.app as app_module
        await _save_wmbus(client, [METER_A, METER_B])
        app_module.history.set_status("wmbus:woda:total_m3", "wmbus", "1", label="Woda")
        paths = [
            "/api/plugins/wmbus/meters", "/api/plugins/wmbus/meters?serial=12345678",
            "/api/plugins/wmbus/readings", "/api/plugins/wmbus/meter_fields?serial=12345678",
            "/api/plugins/wmbus/discovered", "/api/plugins/energy_cost/available_meters",
            "/api/plugins/energy_cost/available_meters?serial=ABCDEF01",
        ]
        for path in paths:
            resp = await client.get(path, headers=AUTH)
            assert resp.status_code == 200, path
            assert _no_keys(await resp.get_data(as_text=True)), path


async def _set_energy_meters(meters):
    import dashboard.app as app_module
    current = app_module.config_store.get("config")
    current["energy"] = {
        "vat_percent": 23, "max_gap_minutes": 120,
        "tariffs": {"G11": {"prices": {"całodobowa": 1.0}}}, "meters": meters,
    }
    app_module.config_store.set("config", current)


ENERGY_METERS = [
    {"name": "dom", "label": "Dom", "sensor": "licznik_dom", "tariff": "G11", "serial": "aaa111"},
    {"name": "garaz", "label": "Garaż", "sensor": "licznik_garaz", "tariff": "G11", "serial": "bbb222"},
]


class TestEnergySerialFilter:

    async def test_available_meters_filtered_by_serial(self, client):
        await _save_wmbus(client, [METER_A, METER_B])
        everything = await _get_json(client, "/api/plugins/energy_cost/available_meters")
        assert {m["serial"] for m in everything["meters"]} == {"12345678", "ABCDEF01"}
        only_b = await _get_json(client, "/api/plugins/energy_cost/available_meters?serial=abcdef01")
        assert [m["name"] for m in only_b["meters"]] == ["woda"]
        assert (await _get_json(client, "/api/plugins/energy_cost/available_meters?serial=00000000"))["meters"] == []

    async def test_meter_start_found_by_serial_case_insensitive(self, client):
        await _set_energy_meters(ENERGY_METERS)
        assert (await _get_json(client, "/api/plugins/energy_cost/meter_start?serial=BBB222"))["meter"] == "garaz"
        assert (await _get_json(client, "/api/plugins/energy_cost/meter_start?serial=aaa111"))["meter"] == "dom"
        resp = await client.get("/api/plugins/energy_cost/meter_start?serial=000000", headers=AUTH)
        assert resp.status_code == 404


class TestMeterStartIsolation:

    async def test_start_value_saved_by_serial_does_not_touch_other_meter(self, client):
        await _set_energy_meters(ENERGY_METERS)
        resp = await client.post(
            "/api/plugins/energy_cost/meter_start", headers=AUTH, json={"serial": "BBB222", "start": {"_": 10}},
        )
        assert resp.status_code == 200
        garage = await _get_json(client, "/api/plugins/energy_cost/meter_start?meter=garaz")
        home = await _get_json(client, "/api/plugins/energy_cost/meter_start?meter=dom")
        assert {r["key"]: r["start"] for r in garage["registers"]} == {"_": 10}
        assert {r["key"]: r["start"] for r in home["registers"]} == {"_": None}

    async def test_start_date_set_rejected_when_invalid_and_cleared(self, client):
        await _set_energy_meters(ENERGY_METERS)
        url = "/api/plugins/energy_cost/meter_start"
        ok = await client.post(url, headers=AUTH, json={"meter": "dom", "start": {"_": 1}, "start_date": "2026-11-01"})
        assert ok.status_code == 200 and (await ok.get_json())["start_date"] == "2026-11-01"
        assert (await _get_json(client, f"{url}?meter=dom"))["start_date"] == "2026-11-01"
        bad = await client.post(url, headers=AUTH, json={"meter": "dom", "start": {"_": 1}, "start_date": "01.11.2026"})
        assert bad.status_code == 400
        assert (await _get_json(client, f"{url}?meter=dom"))["start_date"] == "2026-11-01"
        cleared = await client.post(url, headers=AUTH, json={"meter": "dom", "start": {"_": 1}, "start_date": ""})
        assert cleared.status_code == 200 and (await cleared.get_json())["start_date"] is None
        assert (await _get_json(client, f"{url}?meter=dom"))["start_date"] is None

    async def test_start_date_of_one_meter_does_not_leak_to_another(self, client):
        await _set_energy_meters(ENERGY_METERS)
        url = "/api/plugins/energy_cost/meter_start"
        await client.post(url, headers=AUTH, json={"meter": "dom", "start": {"_": 1}, "start_date": "2026-11-01"})
        assert (await _get_json(client, f"{url}?meter=garaz"))["start_date"] is None
