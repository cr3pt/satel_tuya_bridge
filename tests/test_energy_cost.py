# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Testy wtyczki plugins/energy_cost - API konfiguracji i raportu kosztów."""
import time
from datetime import datetime

import pytest

from bridge.tariffs import TZ
from tests.test_plugin_routes import _basic_auth_header, client

AUTH = _basic_auth_header("test_haslo_123")


def ts(y, m, d, h=0, mi=0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=TZ).timestamp()


CFG = {
    "vat_percent": 23,
    "max_gap_minutes": 120,
    "tariffs": {
        "G11": {"prices": {"całodobowa": 1.0}},
        "G12w": {"prices": {"dzienna": 1.2, "nocna": 0.6}},
    },
    "meters": [{"name": "dom", "label": "Dom", "sensor": "licznik_kwh", "tariff": "G12w"}],
}


async def _save(client, cfg=CFG):
    return await client.post("/api/plugins/energy_cost/config", json=cfg, headers=AUTH)


class TestAuth:
    async def test_page_redirects_without_login(self, client):
        resp = await client.get("/plugins/energy_cost", follow_redirects=False)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]

    async def test_page_loads_when_logged_in(self, client):
        assert (await client.get("/plugins/energy_cost", headers=AUTH)).status_code == 200

    @pytest.mark.parametrize("path", [
        "/api/plugins/energy_cost/config", "/api/plugins/energy_cost/sensors",
        "/api/plugins/energy_cost/report?meter=x&period=today", "/api/plugins/energy_cost/schedule?tariff=G11",
    ])
    async def test_api_requires_login(self, client, path):
        assert (await client.get(path)).status_code == 401

    async def test_post_requires_login(self, client):
        assert (await client.post("/api/plugins/energy_cost/config", json=CFG)).status_code == 401


class TestConfig:
    async def test_get_defaults_and_catalog(self, client):
        data = await (await client.get("/api/plugins/energy_cost/config", headers=AUTH)).get_json()
        assert data["config"]["meters"] == []
        assert set(data["builtin"]) == {"G11", "G12", "G12w", "G13"}
        assert data["builtin"]["G13"]["zones"] == ["szczyt_przedpołudniowy", "szczyt_popołudniowy", "pozostałe"]
        assert "enea" in data["g12_variants"]

    async def test_save_roundtrip(self, client):
        resp = await _save(client)
        assert resp.status_code == 200
        data = await (await client.get("/api/plugins/energy_cost/config", headers=AUTH)).get_json()
        assert data["config"]["meters"][0]["sensor"] == "licznik_kwh"
        assert data["config"]["tariffs"]["G12w"]["prices"]["nocna"] == 0.6

    async def test_save_does_not_touch_other_config(self, client):
        import dashboard.app as app_module
        await _save(client)
        assert app_module.config_store.get("config")["satel"]["host"] == "127.0.0.1"

    async def test_invalid_config_rejected_and_not_saved(self, client):
        bad = {**CFG, "meters": [{"name": "dom", "sensor": "x", "tariff": "NIE_MA"}]}
        resp = await _save(client, bad)
        assert resp.status_code == 400 and "nie jest skonfigurowana" in (await resp.get_json())["error"]
        data = await (await client.get("/api/plugins/energy_cost/config", headers=AUTH)).get_json()
        assert data["config"]["meters"] == []


class TestReport:
    async def _seed(self, client):
        import dashboard.app as app_module
        await _save(client)
        h = app_module.history
        for i in range(13):
            h.write_reading("licznik_kwh", 100.0 + i, ts=ts(2026, 7, 11, 8 + i))

    async def test_report_for_explicit_range(self, client):
        await self._seed(client)
        url = (f"/api/plugins/energy_cost/report?meter=dom&from={ts(2026, 7, 11, 8)}&to={ts(2026, 7, 11, 20)}")
        resp = await client.get(url, headers=AUTH)
        assert resp.status_code == 200
        rep = await resp.get_json()
        assert rep["result"]["total_kwh"] == pytest.approx(12.0)
        zones = {z["zone"]: z for z in rep["result"]["zones"]}
        assert zones["nocna"]["kwh"] == pytest.approx(12.0) and zones["dzienna"]["kwh"] == 0
        assert rep["result"]["total_net"] == pytest.approx(7.2)
        assert rep["result"]["total_gross"] == pytest.approx(7.2 * 1.23, abs=0.01)
        by = {c["tariff"]: c for c in rep["comparison"]}
        assert by["G11"]["total_net"] == pytest.approx(12.0) and by["G12w"]["active"]

    async def test_report_with_named_period(self, client):
        await self._seed(client)
        resp = await client.get("/api/plugins/energy_cost/report?meter=dom&period=7d", headers=AUTH)
        assert resp.status_code == 200
        assert "result" in await resp.get_json()

    async def test_unknown_meter_404(self, client):
        await _save(client)
        assert (await client.get("/api/plugins/energy_cost/report?meter=nie&period=7d", headers=AUTH)).status_code == 404

    @pytest.mark.parametrize("query", ["meter=dom", "meter=dom&period=forever", "meter=dom&from=abc",
                                      "meter=dom&from=100&to=50", "meter=dom&from=0&to=999999999"])
    async def test_bad_params_400(self, client, query):
        await _save(client)
        assert (await client.get(f"/api/plugins/energy_cost/report?{query}", headers=AUTH)).status_code == 400

    async def test_no_readings_returns_zero_with_samples_zero(self, client):
        await _save(client)
        now = time.time()
        rep = await (await client.get(
            f"/api/plugins/energy_cost/report?meter=dom&from={now - 3600}&to={now}", headers=AUTH)).get_json()
        assert rep["result"]["samples"] == 0 and rep["result"]["total_kwh"] == 0


class TestSchedule:
    async def test_schedule_for_builtin(self, client):
        data = await (await client.get("/api/plugins/energy_cost/schedule?tariff=G12w&variant=enea", headers=AUTH)).get_json()
        winter = data["schedule"]["zima"]["roboczy"]
        assert winter[0] == {"od": "00:00", "do": "06:00", "strefa": "nocna"}

    async def test_unknown_tariff_400(self, client):
        assert (await client.get("/api/plugins/energy_cost/schedule?tariff=ZZ", headers=AUTH)).status_code == 400

    async def test_schedule_uses_saved_custom_tariff(self, client):
        cfg = {**CFG, "custom_tariffs": {"MOJA": {"zones": ["a", "b"], "rules": [{"zone": "b", "hours": ["10:00-11:00"]}]}}}
        assert (await _save(client, cfg)).status_code == 200
        data = await (await client.get("/api/plugins/energy_cost/schedule?tariff=MOJA", headers=AUTH)).get_json()
        assert data["zones"] == ["a", "b"]


class TestReportZoneSensors:
    ZONE_CFG = {
        "vat_percent": 23,
        "max_gap_minutes": 120,
        "tariffs": {
            "G12w": {"prices": {"dzienna": 1.2, "nocna": 0.6}},
            "G11": {"prices": {"całodobowa": 1.0}},
        },
        "meters": [{"name": "dom", "label": "Dom", "tariff": "G12w",
                   "zone_sensors": {"dzienna": "s_dzien", "nocna": "s_noc"}}],
    }

    async def test_report_uses_each_zones_own_sensor(self, client):
        import dashboard.app as app_module
        await _save(client, self.ZONE_CFG)
        h = app_module.history
        for i in range(4):
            h.write_reading("s_dzien", 10.0 + i, ts=ts(2026, 7, 11, 8 + i))
            h.write_reading("s_noc", 5.0 + i * 0.5, ts=ts(2026, 7, 11, 8 + i))

        url = f"/api/plugins/energy_cost/report?meter=dom&from={ts(2026, 7, 11, 8)}&to={ts(2026, 7, 11, 11)}"
        resp = await client.get(url, headers=AUTH)
        assert resp.status_code == 200
        rep = await resp.get_json()
        zones = {z["zone"]: z["kwh"] for z in rep["result"]["zones"]}
        assert zones["dzienna"] == pytest.approx(3.0)
        assert zones["nocna"] == pytest.approx(1.5)
        assert rep["meter"]["zone_sensors"] == {"dzienna": "s_dzien", "nocna": "s_noc"}

    async def test_comparison_with_incompatible_tariff_reports_error_not_crash(self, client):
        import dashboard.app as app_module
        await _save(client, self.ZONE_CFG)
        h = app_module.history
        h.write_reading("s_dzien", 10.0, ts=ts(2026, 7, 11, 8))
        h.write_reading("s_dzien", 12.0, ts=ts(2026, 7, 11, 9))

        url = f"/api/plugins/energy_cost/report?meter=dom&from={ts(2026, 7, 11, 8)}&to={ts(2026, 7, 11, 9)}"
        resp = await client.get(url, headers=AUTH)
        assert resp.status_code == 200
        by = {c["tariff"]: c for c in (await resp.get_json())["comparison"]}
        assert "error" in by["G11"]


class TestSensors:
    async def test_suggests_kwh_sensors(self, client):
        import dashboard.app as app_module
        app_module.history.write_reading("licznik_kwh", 1.0)
        app_module.history.write_reading("kociol_temp", 50.0)
        data = await (await client.get("/api/plugins/energy_cost/sensors", headers=AUTH)).get_json()
        assert data["suggested"] == ["licznik_kwh"] and "kociol_temp" in data["sensors"]

    async def test_flagged_sensor_is_suggested_even_without_kwh_in_name(self, client):
        import dashboard.app as app_module
        from bridge import energy_meters

        app_module.history.write_reading("esphome_gama350_zuzycie_energii", 1.0)
        energy_meters.mark_energy_meter(app_module.history, "esphome_gama350_zuzycie_energii", "Gama 350")
        data = await (await client.get("/api/plugins/energy_cost/sensors", headers=AUTH)).get_json()
        assert data["suggested"] == ["esphome_gama350_zuzycie_energii"]

    async def test_flagged_sensor_listed_before_kwh_matched_one(self, client):
        import dashboard.app as app_module
        from bridge import energy_meters

        app_module.history.write_reading("licznik_kwh", 1.0)
        app_module.history.write_reading("esphome_gama350_zuzycie_energii", 1.0)
        energy_meters.mark_energy_meter(app_module.history, "esphome_gama350_zuzycie_energii", "Gama 350")
        data = await (await client.get("/api/plugins/energy_cost/sensors", headers=AUTH)).get_json()
        assert data["suggested"] == ["esphome_gama350_zuzycie_energii", "licznik_kwh"]


class TestFlaggedSensors:
    async def test_requires_auth(self, client):
        assert (await client.get("/api/plugins/energy_cost/flagged_sensors")).status_code == 401

    async def test_empty_without_any_flagged_sensor(self, client):
        data = await (await client.get("/api/plugins/energy_cost/flagged_sensors", headers=AUTH)).get_json()
        assert data == {"flagged": []}

    async def test_lists_flagged_sensors_not_yet_used_as_a_meter(self, client):
        import dashboard.app as app_module
        from bridge import energy_meters

        energy_meters.mark_energy_meter(app_module.history, "esphome_gama350_zuzycie_energii", "Gama 350: zużycie")
        data = await (await client.get("/api/plugins/energy_cost/flagged_sensors", headers=AUTH)).get_json()
        assert data == {"flagged": [{"sensor": "esphome_gama350_zuzycie_energii", "label": "Gama 350: zużycie"}]}

    async def test_excludes_sensors_already_used_by_a_configured_meter(self, client):
        import dashboard.app as app_module
        from bridge import energy_meters

        energy_meters.mark_energy_meter(app_module.history, "licznik_kwh", "Dom")
        await _save(client)  # CFG's one meter uses sensor="licznik_kwh"
        data = await (await client.get("/api/plugins/energy_cost/flagged_sensors", headers=AUTH)).get_json()
        assert data == {"flagged": []}


class TestPluginsList:
    async def test_listed_but_not_toggleable(self, client):
        plugins = await (await client.get("/api/plugins", headers=AUTH)).get_json()
        p = next(p for p in plugins if p["id"] == "energy_cost")
        assert p["toggleable"] is False and p["configured"] is False
        await _save(client)
        plugins = await (await client.get("/api/plugins", headers=AUTH)).get_json()
        p = next(p for p in plugins if p["id"] == "energy_cost")
        assert p["configured"] is True and p["detail"] == "liczników: 1"


class TestDaily:
    async def test_daily_breakdown_sums_to_total(self, client):
        import dashboard.app as app_module
        await _save(client)
        h = app_module.history
        for i in range(49):
            h.write_reading("licznik_kwh", 100.0 + i, ts=ts(2026, 7, 13) + i * 3600)
        url = f"/api/plugins/energy_cost/report?meter=dom&from={ts(2026, 7, 13)}&to={ts(2026, 7, 15)}"
        rep = await (await client.get(url, headers=AUTH)).get_json()
        daily = rep["daily"]
        assert [d["date"] for d in daily] == ["2026-07-13", "2026-07-14"]
        assert all(d["total_kwh"] == pytest.approx(24.0) for d in daily)
        assert sum(d["total_kwh"] for d in daily) == pytest.approx(rep["result"]["total_kwh"])
        assert sum(d["cost_gross"] for d in daily) == pytest.approx(rep["result"]["total_gross"], abs=0.02)
        assert set(daily[0]["kwh_by_zone"]) == {"dzienna", "nocna"}

    async def test_partial_days_and_empty_history(self, client):
        await _save(client)
        url = f"/api/plugins/energy_cost/report?meter=dom&from={ts(2026, 7, 13, 12)}&to={ts(2026, 7, 14, 6)}"
        daily = (await (await client.get(url, headers=AUTH)).get_json())["daily"]
        assert len(daily) == 2 and daily[0]["from"] == ts(2026, 7, 13, 12) and daily[1]["to"] == ts(2026, 7, 14, 6)
        assert all(d["total_kwh"] == 0 for d in daily)

    async def test_dst_day_is_23_hours(self, client):
        await _save(client)
        url = f"/api/plugins/energy_cost/report?meter=dom&from={ts(2026, 3, 29)}&to={ts(2026, 3, 30)}"
        d = (await (await client.get(url, headers=AUTH)).get_json())["daily"][0]
        assert d["to"] - d["from"] == 23 * 3600


class TestPermanentMeterHistory:
    CFG = {"vat_percent": 23, "max_gap_minutes": 120,
           "tariffs": {"G11": {"prices": {"całodobowa": 1.0}}},
           "meters": [{"name": "dom", "label": "Dom", "tariff": "G11", "sensor": "licznik_kwh"}]}

    async def test_saving_config_registers_meter_and_report_survives_purge(self, client):
        import time as _time
        import dashboard.app as app_module
        from bridge.maintenance import purge_old_data
        h = app_module.history
        old = _time.time() - 400 * 86400
        h.write_reading("licznik_kwh", 100.0, ts=old)
        h.write_reading("licznik_kwh", 101.0, ts=old + 3600)
        await _save(client, self.CFG)
        assert "licznik_kwh" in h.get_energy_sensors()

        purge_old_data(h.db_path, retention_days=90)
        url = f"/api/plugins/energy_cost/report?meter=dom&from={old}&to={old + 3600}"
        rep = await (await client.get(url, headers=AUTH)).get_json()
        assert rep["result"]["total_kwh"] == pytest.approx(1.0)


class TestReportMeterState:
    """Okres "meter": koszt = najnowszy stan z MQTT x cena, bez sumowania."""

    async def test_uses_latest_mqtt_value_of_each_zone_register_as_is(self, client):
        import dashboard.app as app_module
        await _save(client, TestReportZoneSensors.ZONE_CFG)
        h = app_module.history
        h.write_reading("s_dzien", 1490.0, ts=ts(2026, 11, 17, 8))
        h.write_reading("s_dzien", 1496.492, ts=ts(2026, 11, 19, 18))
        h.write_reading("s_noc", 345.0, ts=ts(2026, 11, 17, 8))
        h.write_reading("s_noc", 350.219, ts=ts(2026, 11, 19, 18))

        rep = await (await client.get("/api/plugins/energy_cost/report?meter=dom&period=meter", headers=AUTH)).get_json()
        assert rep["mode"] == "meter"
        zones = {z["zone"]: z for z in rep["result"]["zones"]}
        assert zones["dzienna"]["kwh"] == pytest.approx(1496.492)
        assert zones["nocna"]["kwh"] == pytest.approx(350.219)
        assert rep["result"]["total_kwh"] == pytest.approx(1846.711)
        assert zones["dzienna"]["net"] == pytest.approx(round(1496.492 * 1.2, 2))
        assert rep["result"]["fixed_net"] == 0
        assert {r["zone"]: r["value"] for r in rep["latest"]} == {"dzienna": 1496.492, "nocna": 350.219}

    async def test_single_register_with_single_zone_tariff(self, client):
        import dashboard.app as app_module
        cfg = {**CFG, "meters": [{"name": "dom", "label": "Dom", "sensor": "licznik_kwh", "tariff": "G11"}]}
        await _save(client, cfg)
        app_module.history.write_reading("licznik_kwh", 1846.711, ts=ts(2026, 11, 19, 18))
        rep = await (await client.get("/api/plugins/energy_cost/report?meter=dom&period=meter", headers=AUTH)).get_json()
        assert rep["result"]["total_kwh"] == pytest.approx(1846.711)
        assert rep["result"]["total_net"] == pytest.approx(1846.71)

    async def test_single_register_with_multi_zone_tariff_is_400_not_a_guess(self, client):
        import dashboard.app as app_module
        await _save(client)
        app_module.history.write_reading("licznik_kwh", 1846.711, ts=ts(2026, 11, 19, 18))
        resp = await client.get("/api/plugins/energy_cost/report?meter=dom&period=meter", headers=AUTH)
        assert resp.status_code == 400
        assert "osobny czujnik per strefa" in (await resp.get_json())["error"]

    async def test_no_readings_gives_zero_samples(self, client):
        await _save(client, TestReportZoneSensors.ZONE_CFG)
        rep = await (await client.get("/api/plugins/energy_cost/report?meter=dom&period=meter", headers=AUTH)).get_json()
        assert rep["result"]["samples"] == 0 and rep["result"]["total_kwh"] == 0


class TestClearReadings:
    async def test_requires_login_and_exact_confirmation(self, client):
        url = "/api/plugins/energy_cost/clear_readings"
        assert (await client.post(url, json={"confirm": "CZYSC"})).status_code == 401
        for body in ({}, {"confirm": "czysc"}, {"confirm": "TAK"}):
            assert (await client.post(url, json=body, headers=AUTH)).status_code == 400

    async def test_clears_energy_readings_but_keeps_other_sensors_and_config(self, client):
        import dashboard.app as app_module
        await _save(client)
        h = app_module.history
        h.write_reading("licznik_kwh", 1496.0, ts=ts(2026, 11, 19, 8))
        h.write_reading("temperatura", 21.5, ts=ts(2026, 11, 19, 8))
        resp = await client.post("/api/plugins/energy_cost/clear_readings", json={"confirm": "CZYSC"}, headers=AUTH)
        assert resp.status_code == 200 and (await resp.get_json())["deleted"] >= 1
        assert h.get_latest_energy_reading("licznik_kwh") is None
        assert h.get_latest_energy_reading("temperatura") == {"t": ts(2026, 11, 19, 8), "v": 21.5}
        assert h.get_energy_sensors() == ["licznik_kwh"]
        h.write_reading("licznik_kwh", 1496.5, ts=ts(2026, 11, 19, 9))
        assert h.get_latest_energy_reading("licznik_kwh")["v"] == 1496.5


class TestMeterStart:
    """START LICZNIKA: odejmowany od stanu z MQTT przed mnożeniem przez cenę."""

    async def _seed(self, client):
        import dashboard.app as app_module
        await _save(client, TestReportZoneSensors.ZONE_CFG)
        h = app_module.history
        h.write_reading("s_dzien", 1496.492, ts=ts(2026, 11, 19, 18))
        h.write_reading("s_noc", 350.219, ts=ts(2026, 11, 19, 18))

    async def test_start_is_subtracted_before_multiplying_by_price(self, client):
        await self._seed(client)
        resp = await client.post("/api/plugins/energy_cost/meter_start", headers=AUTH,
                                 json={"meter": "dom", "start": {"dzienna": 1490.0, "nocna": "350"}})
        assert resp.status_code == 200
        rep = await (await client.get("/api/plugins/energy_cost/report?meter=dom&period=meter", headers=AUTH)).get_json()
        zones = {z["zone"]: z for z in rep["result"]["zones"]}
        assert zones["dzienna"]["kwh"] == pytest.approx(6.492)
        assert zones["dzienna"]["net"] == pytest.approx(round(6.492 * 1.2, 2))
        assert zones["nocna"]["kwh"] == pytest.approx(0.219)
        latest = {r["zone"]: r for r in rep["latest"]}
        assert latest["dzienna"]["value"] == 1496.492 and latest["dzienna"]["start"] == 1490.0
        assert latest["dzienna"]["used"] == pytest.approx(6.492)

    async def test_get_lists_registers_with_start_and_latest(self, client):
        await self._seed(client)
        await client.post("/api/plugins/energy_cost/meter_start", headers=AUTH,
                          json={"meter": "dom", "start": {"dzienna": 1490}})
        data = await (await client.get("/api/plugins/energy_cost/meter_start?meter=dom", headers=AUTH)).get_json()
        regs = {r["key"]: r for r in data["registers"]}
        assert regs["dzienna"]["start"] == 1490 and regs["dzienna"]["latest"] == 1496.492
        assert regs["nocna"]["start"] is None

    async def test_single_register_uses_underscore_key(self, client):
        import dashboard.app as app_module
        await _save(client, {**CFG, "meters": [{"name": "dom", "label": "Dom", "sensor": "licznik_kwh", "tariff": "G11"}]})
        app_module.history.write_reading("licznik_kwh", 1846.711, ts=ts(2026, 11, 19, 18))
        await client.post("/api/plugins/energy_cost/meter_start", headers=AUTH, json={"meter": "dom", "start": {"_": 1840}})
        rep = await (await client.get("/api/plugins/energy_cost/report?meter=dom&period=meter", headers=AUTH)).get_json()
        assert rep["result"]["total_kwh"] == pytest.approx(6.711)

    @pytest.mark.parametrize("body", [{"meter": "dom", "start": {"zla_strefa": 1}},
                                      {"meter": "dom", "start": {"dzienna": -5}},
                                      {"meter": "dom", "start": {"dzienna": "abc"}},
                                      {"meter": "dom", "start": [1]}])
    async def test_invalid_start_rejected(self, client, body):
        await self._seed(client)
        assert (await client.post("/api/plugins/energy_cost/meter_start", headers=AUTH, json=body)).status_code == 400

    async def test_requires_login_and_known_meter(self, client):
        await self._seed(client)
        body = {"meter": "dom", "start": {"dzienna": 1}}
        assert (await client.post("/api/plugins/energy_cost/meter_start", json=body)).status_code == 401
        resp = await client.post("/api/plugins/energy_cost/meter_start", headers=AUTH, json={**body, "meter": "nie"})
        assert resp.status_code == 404

    async def test_price_parts_shown_for_split_price(self, client):
        import dashboard.app as app_module
        cfg = {**CFG, "tariffs": {"G11": {"prices": {"całodobowa": {"energy_net": 0.5, "distribution_net": 0.3}},
                                          "other_per_kwh_net": 0.05}},
               "meters": [{"name": "dom", "label": "Dom", "sensor": "licznik_kwh", "tariff": "G11"}]}
        await _save(client, cfg)
        app_module.history.write_reading("licznik_kwh", 10.0, ts=ts(2026, 11, 19, 18))
        rep = await (await client.get("/api/plugins/energy_cost/report?meter=dom&period=meter", headers=AUTH)).get_json()
        [z] = rep["result"]["zones"]
        assert z["price_net_per_kwh"] == pytest.approx(0.85)
        assert z["price_parts"] == {"energy_net": 0.5, "distribution_net": 0.3, "other_per_kwh_net": 0.05}
        assert z["net"] == pytest.approx(8.5)


class TestMonthlyEndpoint:
    async def test_requires_login(self, client):
        assert (await client.get("/api/plugins/energy_cost/monthly?meter=dom")).status_code == 401

    async def test_monthly_rows(self, client):
        import dashboard.app as app_module
        await _save(client)
        now = time.time()
        for i in range(5):
            app_module.history.write_reading("licznik_kwh", 100.0 + i, ts=now - (5 - i) * 600)
        resp = await client.get("/api/plugins/energy_cost/monthly?meter=dom&months=3", headers=AUTH)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert 1 <= len(data["months"]) <= 2  # bez miesięcy sprzed pierwszego odczytu
        assert data["months"][0]["complete"] is False
        assert sum(m["total_kwh"] for m in data["months"]) == pytest.approx(4.0)

    async def test_unknown_meter_and_bad_count(self, client):
        await _save(client)
        assert (await client.get("/api/plugins/energy_cost/monthly?meter=x", headers=AUTH)).status_code == 404
        assert (await client.get("/api/plugins/energy_cost/monthly?meter=dom&months=abc", headers=AUTH)).status_code == 400

    async def test_report_for_month_period_has_invoice_lines(self, client):
        await _save(client)
        resp = await client.get("/api/plugins/energy_cost/report?meter=dom&period=month:2026-10", headers=AUTH)
        assert resp.status_code == 200
        assert "lines" in (await resp.get_json())["result"]
