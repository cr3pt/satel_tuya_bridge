"""Testy wtyczki plugins/energy_cost - API konfiguracji i raportu kosztów."""
import time
from datetime import datetime

import pytest

from bridge.tariffs import TZ
from tests.test_plugin_routes import _basic_auth_header, client  # noqa: F401

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
        # sobota 11.07.2026, co godzinę 1 kWh, 8:00-20:00 -> wszystko w taniej strefie G12w
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


class TestSensors:
    async def test_suggests_kwh_sensors(self, client):
        import dashboard.app as app_module
        app_module.history.write_reading("licznik_kwh", 1.0)
        app_module.history.write_reading("kociol_temp", 50.0)
        data = await (await client.get("/api/plugins/energy_cost/sensors", headers=AUTH)).get_json()
        assert data["suggested"] == ["licznik_kwh"] and "kociol_temp" in data["sensors"]


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
        # pn 13.07 i wt 14.07.2026: 1 kWh/h przez 48 h od północy
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
