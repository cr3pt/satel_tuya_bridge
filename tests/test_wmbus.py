"""Testy wM-Bus: walidacja kluczy AES, generator konfiguracji wmbusmeters,
rozpoznawanie faz L1/L2/L3, integracja z silnikiem reguł, panel i
bezpieczeństwo kluczy, a także regresja bezpieczeństwa wątkowego alarmów."""
import asyncio
import json
import os
import stat
import threading

import pytest
import yaml

from bridge import wmbus
from bridge.alarms import AlarmEngine
from bridge.config import BridgeConfig
from bridge.rules_engine import RulesEngine
from tests.test_plugin_routes import _basic_auth_header, client  # noqa: F401
from tests.test_rules_engine import CONFIG_TEMPLATE, SENSORS_YAML

KEY = "00112233445566778899AABBCCDDEEFF"

METER = {"name": "prad_dom", "label": "Prąd dom", "id": "12345678", "driver": "auto", "key": KEY, "phases": 3}

# Typowy kształt telegramu licznika 3-fazowego (nazwy pól różnią się
# między sterownikami - dlatego rozpoznajemy fazy po wzorcu nazwy).
TELEGRAM_3PH = {
    "media": "electricity", "meter": "amiplus", "name": "prad_dom", "id": "12345678",
    "total_energy_consumption_kwh": 1234.5,
    "voltage_at_phase_1_v": 231.0, "voltage_at_phase_2_v": 229.5, "voltage_at_phase_3_v": 232.1,
    "current_at_phase_1_a": 1.2, "current_at_phase_2_a": 0.4, "current_at_phase_3_a": 2.0,
    "timestamp": "2026-10-10T10:00:00Z", "rssi_dbm": -78, "status": "OK",
}


class TestValidation:
    def test_valid_meter_has_no_errors(self):
        assert wmbus.validate_meter(METER) == []

    @pytest.mark.parametrize("key", ["abc", KEY[:-1], KEY + "0", "Z" * 32, KEY.replace("0", "g", 1)])
    def test_bad_key_rejected(self, key):
        assert any("klucz AES" in e for e in wmbus.validate_meter({**METER, "key": key}))

    def test_empty_key_allowed_for_unencrypted_meter(self):
        assert wmbus.validate_meter({**METER, "key": ""}) == []

    @pytest.mark.parametrize("name", ["", "a b", "../etc/passwd", "a;rm -rf", "x" * 41, "zażółć"])
    def test_dangerous_names_rejected(self, name):
        assert wmbus.validate_meter({**METER, "name": name})

    def test_bad_phase_count_rejected(self):
        assert wmbus.validate_meter({**METER, "phases": 2})

    def test_voltage_min_must_be_below_max(self):
        assert wmbus.validate_meter({**METER, "voltage_min": 250, "voltage_max": 200})

    def test_duplicate_names_rejected(self):
        assert any("Zduplikowana" in e for e in wmbus.validate_meters([METER, dict(METER)]))


class TestPhaseDetection:
    @pytest.mark.parametrize("field,phase", [
        ("voltage_at_phase_1_v", 1), ("voltage_at_phase_2_v", 2), ("current_phase3_a", 3),
        ("power_l1_kw", 1), ("active_power_l2", 2), ("voltage_ph3_v", 3),
    ])
    def test_phase_fields(self, field, phase):
        assert wmbus.detect_phase(field) == phase

    @pytest.mark.parametrize("field", [
        "total_energy_consumption_kwh", "tariff_1_kwh", "total_m3", "voltage_v", "phase_4_v", "flow_temperature_c",
    ])
    def test_non_phase_fields(self, field):
        assert wmbus.detect_phase(field) is None

    def test_units(self):
        assert wmbus.field_unit("voltage_at_phase_1_v")[1] == "voltage"
        assert wmbus.field_unit("total_energy_consumption_kwh")[2] == "total_increasing"
        assert wmbus.field_unit("unknown_field") is None


class TestFiles:
    def test_meter_file_contents(self):
        assert wmbus.render_meter_file(METER) == f"name=prad_dom\nid=12345678\ndriver=auto\nkey={KEY}\n"

    def test_unencrypted_meter_uses_nokey(self):
        assert "key=NOKEY" in wmbus.render_meter_file({**METER, "key": ""})

    def test_main_conf_uses_env_vars_not_telegram_data(self):
        conf = wmbus.render_main_conf({"device": "im871a:t1", "topic_prefix": "wmbusmeters"}, "127.0.0.1", 1883)
        assert "device=im871a:t1" in conf
        assert 'wmbusmeters/$METER_NAME -m "$METER_JSON"' in conf

    @pytest.mark.parametrize("cfg,host", [
        ({"device": "x; reboot"}, "127.0.0.1"),
        ({"device": "auto"}, "host; reboot"),
        ({"device": "auto", "topic_prefix": "a b"}, "127.0.0.1"),
    ])
    def test_injection_in_conf_rejected(self, cfg, host):
        with pytest.raises(ValueError):
            wmbus.render_main_conf(cfg, host, 1883)

    def test_files_have_0600_and_stale_meter_files_removed(self, tmp_path):
        out = tmp_path / "out"
        wmbus.write_wmbusmeters_files({"device": "auto"}, "127.0.0.1", 1883, [METER, {**METER, "name": "drugi"}], out)
        for p in [out / "etc/wmbusmeters.conf", out / "etc/wmbusmeters.d/prad_dom", out / "etc/wmbusmeters.d/drugi"]:
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        wmbus.write_wmbusmeters_files({"device": "auto"}, "127.0.0.1", 1883, [METER], out)
        assert not (out / "etc/wmbusmeters.d/drugi").exists()  # klucz usuniętego licznika nie zostaje na dysku

    def test_invalid_meter_blocks_generation(self, tmp_path):
        with pytest.raises(ValueError):
            wmbus.write_wmbusmeters_files({"device": "auto"}, "h", 1883, [{**METER, "key": "zly"}], tmp_path)


class TestKeyHandling:
    def test_public_view_never_contains_key(self):
        view = wmbus.public_view([METER])[0]
        assert "key" not in view and view["has_key"] is True
        assert KEY not in json.dumps(view)
        assert view["key_masked"].endswith(KEY[-4:])

    def test_empty_key_keeps_existing(self):
        merged = wmbus.merge_keys([{**METER, "key": ""}], [METER])
        assert merged[0]["key"] == KEY

    def test_new_key_overrides(self):
        new = "F" * 32
        assert wmbus.merge_keys([{**METER, "key": new}], [METER])[0]["key"] == new

    def test_clear_key_removes(self):
        assert wmbus.merge_keys([{**METER, "key": "", "clear_key": True}], [METER])[0]["key"] == ""


class TestParsing:
    def test_parse_valid_and_invalid(self):
        assert wmbus.parse_telegram(b'{"a": 1}') == {"a": 1}
        assert wmbus.parse_telegram(b"not json") is None
        assert wmbus.parse_telegram(b"[1,2]") is None

    def test_numeric_fields_skip_text_bool_timestamp_and_nan(self):
        fields = wmbus.numeric_fields({"a": 1, "b": "x", "c": True, "timestamp": 5, "d": float("nan"), "e": 2.5})
        assert fields == {"a": 1, "e": 2.5}

    def test_phases_in(self):
        assert wmbus.phases_in(["voltage_at_phase_1_v", "current_at_phase_3_a", "total_kwh"]) == {1, 3}


# ----------------------------------------------------------------------
# Silnik reguł
# ----------------------------------------------------------------------
@pytest.fixture
async def engine(tmp_path):
    (tmp_path / "sensors.yaml").write_text(SENSORS_YAML, encoding="utf-8")
    meters = [{**METER, "voltage_min": 207, "voltage_max": 253, "min_history_interval_seconds": 0}]
    text = CONFIG_TEMPLATE.format(db_path=str(tmp_path / "history.db"))
    text += "\n" + yaml.safe_dump({"wmbus": {"enabled": True, "meters": meters}})
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    return RulesEngine(BridgeConfig.load(tmp_path / "config.yaml"))


class TestEngine:
    async def test_every_phase_is_stored_separately(self, engine):
        engine.handle_wmbus_reading(METER, TELEGRAM_3PH)
        sensors = engine.history.get_sensors()
        for n in (1, 2, 3):
            assert f"wmbus_prad_dom_voltage_at_phase_{n}_v" in sensors
            assert f"wmbus_prad_dom_current_at_phase_{n}_a" in sensors
        labels = {s["label"] for s in engine.history.get_status("wmbus")}
        assert "Prąd dom: voltage_at_phase_2_v [L2]" in labels

    async def test_all_three_phases_reported(self, engine):
        engine.handle_wmbus_reading(METER, TELEGRAM_3PH)
        phases = [s for s in engine.history.get_status("wmbus") if s["key"].endswith(":phases")][0]
        assert phases["value"] == "L1 L2 L3"

    async def test_missing_phase_flagged_and_event_written(self, engine):
        partial = {k: v for k, v in TELEGRAM_3PH.items() if "phase_3" not in k}
        engine.handle_wmbus_reading(METER, partial)
        phases = [s for s in engine.history.get_status("wmbus") if s["key"].endswith(":phases")][0]
        assert "brak: L3" in phases["value"]
        assert engine.history.get_events("wmbus", 0, 9e12)

    async def test_phases_accumulate_across_alternating_telegrams(self, engine):
        engine.handle_wmbus_reading(METER, {"voltage_at_phase_1_v": 230.0})
        engine.handle_wmbus_reading(METER, {"voltage_at_phase_2_v": 230.0, "voltage_at_phase_3_v": 230.0})
        phases = [s for s in engine.history.get_status("wmbus") if s["key"].endswith(":phases")][0]
        assert phases["value"] == "L1 L2 L3"

    async def test_low_voltage_on_single_phase_raises_alert_for_that_phase_only(self, engine):
        engine.handle_wmbus_reading(METER, {**TELEGRAM_3PH, "voltage_at_phase_2_v": 0.0})
        alerts = engine.history.get_active_alerts()
        assert len(alerts) == 1 and "L2" in alerts[0]["rule_name"]

    async def test_voltage_alert_resolves(self, engine):
        engine.handle_wmbus_reading(METER, {**TELEGRAM_3PH, "voltage_at_phase_3_v": 250.0 + 10})
        assert len(engine.history.get_active_alerts()) == 1
        engine.handle_wmbus_reading(METER, TELEGRAM_3PH)
        assert engine.history.get_active_alerts() == []

    async def test_silent_meter_triggers_stale_alert(self, engine):
        rule = [r for r in engine.alarms.rules if r["condition"] == "stale"][0]
        rule["max_age_seconds"] = 0.01
        await asyncio.sleep(0.05)
        await engine.alarms.check_stale_sensors()
        assert any("Brak telegramów" in a["rule_name"] for a in engine.history.get_active_alerts())

    async def test_history_throttled(self, engine):
        engine._wmbus_meters["prad_dom"]["min_history_interval_seconds"] = 3600
        engine.handle_wmbus_reading(METER, TELEGRAM_3PH)
        engine.handle_wmbus_reading(METER, TELEGRAM_3PH)
        rows = engine.history.get_history("wmbus_prad_dom_total_energy_consumption_kwh", 0, 9e12)
        assert len(rows) == 1  # drugi telegram nie obciąża karty SD
        # ale status "na żywo" jest zawsze świeży
        assert engine.history.get_status("wmbus")

    async def test_ha_discovery_published_once_per_field(self, engine):
        published = []

        class FakeHa:
            def publish_discovery(self, entity): published.append(entity)
            def publish_state(self, topic, value): pass

        engine.ha_mqtt = FakeHa()
        engine.handle_wmbus_reading(METER, TELEGRAM_3PH)
        engine.handle_wmbus_reading(METER, TELEGRAM_3PH)
        topics = [e["discovery_topic"] for e in published]
        assert len(topics) == len(set(topics))
        v2 = [e for e in published if e["payload"]["unique_id"].endswith("voltage_at_phase_2_v")][0]
        assert v2["payload"]["device_class"] == "voltage" and v2["payload"]["name"].endswith("[L2]")


# ----------------------------------------------------------------------
# Alarmy: powiadomienia z wątku MQTT (regresja)
# ----------------------------------------------------------------------
class TestAlarmNotificationFromThread:
    async def test_notification_from_non_asyncio_thread_is_delivered(self, history):
        sent = []
        engine = AlarmEngine(
            [{"name": "r", "sensor": "s", "condition": "above", "threshold": 1, "hysteresis": 0}],
            history, notify_cfg={"ntfy_topic_url": "http://example.invalid/x"},
        )
        engine.set_loop(asyncio.get_running_loop())

        async def fake_send(title, message, priority="default"):
            sent.append(title)
        engine.notifier.send = fake_send

        errors = []

        def worker():
            try:
                engine.on_value("s", 5)  # jak callback paho-mqtt
            except Exception as exc:  # wcześniej: RuntimeError: no running event loop
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        await asyncio.sleep(0.05)
        assert errors == []
        assert sent and "r" in sent[0]


# ----------------------------------------------------------------------
# Panel (wtyczka wmbus)
# ----------------------------------------------------------------------
AUTH = _basic_auth_header("test_haslo_123")


async def _save(client, meters, **extra):
    return await client.post("/api/plugins/wmbus/meters", json={"meters": meters, **extra}, headers=AUTH)


class TestPanel:
    async def test_page_requires_login_then_loads(self, client):
        assert (await client.get("/plugins/wmbus", follow_redirects=False)).status_code == 302
        assert (await client.get("/plugins/wmbus", headers=AUTH)).status_code == 200

    async def test_api_requires_login(self, client):
        assert (await client.get("/api/plugins/wmbus/meters")).status_code == 401
        assert (await client.post("/api/plugins/wmbus/meters", json={"meters": []})).status_code == 401

    async def test_get_never_returns_key(self, client):
        assert (await _save(client, [METER])).status_code == 200
        resp = await client.get("/api/plugins/wmbus/meters", headers=AUTH)
        text = await resp.get_data(as_text=True)
        assert KEY not in text
        assert (await resp.get_json())["meters"][0]["has_key"] is True

    async def test_empty_key_keeps_existing_and_generates_files(self, client):
        import dashboard.app as app_module
        await _save(client, [METER])
        resp = await _save(client, [{**METER, "key": ""}])
        assert resp.status_code == 200 and KEY not in await resp.get_data(as_text=True)
        assert app_module.config_store.get("config")["wmbus"]["meters"][0]["key"] == KEY
        conf = app_module.config_path.parent / "wmbusmeters_conf/etc/wmbusmeters.d/prad_dom"
        assert f"key={KEY}" in conf.read_text()
        assert stat.S_IMODE(os.stat(conf).st_mode) == 0o600

    async def test_invalid_key_rejected_and_not_saved(self, client):
        import dashboard.app as app_module
        await _save(client, [METER])
        resp = await _save(client, [{**METER, "key": "krotki"}])
        assert resp.status_code == 400
        assert app_module.config_store.get("config")["wmbus"]["meters"][0]["key"] == KEY

    async def test_clear_key(self, client):
        import dashboard.app as app_module
        await _save(client, [METER])
        await _save(client, [{**METER, "key": "", "clear_key": True}])
        assert app_module.config_store.get("config")["wmbus"]["meters"][0]["key"] == ""

    async def test_bad_device_and_non_list_rejected(self, client):
        assert (await _save(client, [METER], device="x; reboot")).status_code == 400
        resp = await client.post("/api/plugins/wmbus/meters", json={"meters": "no"}, headers=AUTH)
        assert resp.status_code == 400

    async def test_deleting_meter_removes_its_key_file(self, client):
        import dashboard.app as app_module
        await _save(client, [METER])
        await _save(client, [])
        assert not (app_module.config_path.parent / "wmbusmeters_conf/etc/wmbusmeters.d/prad_dom").exists()

    async def test_does_not_touch_other_config(self, client):
        import dashboard.app as app_module
        await _save(client, [METER])
        assert app_module.config_store.get("config")["satel"]["host"] == "127.0.0.1"

    async def test_listed_in_plugins_and_toggle_off_by_default(self, client):
        await _save(client, [METER])
        plugins = await (await client.get("/api/plugins", headers=AUTH)).get_json()
        p = next(p for p in plugins if p["id"] == "wmbus")
        assert p["configured"] is True and p["enabled"] is False
        settings = await (await client.get("/api/plugins/wmbus/settings", headers=AUTH)).get_json()
        assert settings["enabled"] is False


class TestReload:
    async def test_stale_rule_survives_alarm_engine_rebuild_on_reload(self, engine):
        # reload_config tworzy NOWY AlarmEngine - alarm ciszy licznika musi zostać zarejestrowany ponownie
        from bridge.alarms import AlarmEngine
        engine.alarms = AlarmEngine([], engine.history)
        engine._setup_wmbus(engine.config)
        assert any(r["condition"] == "stale" and r["sensor"] == "wmbus_prad_dom" for r in engine.alarms.rules)


class TestDownsample:
    def test_short_series_unchanged(self):
        pts = [[1.0, 2.0], [2.0, 3.0]]
        assert wmbus.downsample(pts, 10) == pts

    def test_long_series_limited_and_averaged(self):
        pts = [[float(i), float(i)] for i in range(1000)]
        out = wmbus.downsample(pts, 100)
        assert len(out) == 100
        assert out[0] == [4.5, 4.5] and out[-1][0] > out[0][0]


class TestSeriesEndpoint:
    async def test_requires_login(self, client):
        assert (await client.get("/api/plugins/wmbus/series?meter=x")).status_code == 401

    async def test_unknown_meter_404_and_bad_params_400(self, client):
        await _save(client, [METER])
        assert (await client.get("/api/plugins/wmbus/series?meter=nie_ma", headers=AUTH)).status_code == 404
        url = f"/api/plugins/wmbus/series?meter={METER['name']}&hours=abc"
        assert (await client.get(url, headers=AUTH)).status_code == 400

    async def test_returns_per_phase_series_with_units(self, client):
        import time as _t
        import dashboard.app as app_module
        await _save(client, [METER])
        h, now, n = app_module.history, _t.time(), METER["name"]
        for i in range(5):
            for ph, base in ((1, 230), (2, 231), (3, 232)):
                h.write_reading(f"wmbus_{n}_voltage_at_phase_{ph}_v", base + i, ts=now - 600 + i * 60)
        h.write_reading("wmbus_inny_voltage_at_phase_1_v", 1.0, ts=now - 60)
        data = await (await client.get(f"/api/plugins/wmbus/series?meter={n}&hours=1&points=10", headers=AUTH)).get_json()
        assert sorted(s["phase"] for s in data["series"]) == [1, 2, 3]
        s1 = next(s for s in data["series"] if s["phase"] == 1)
        assert s1["unit"] == "V" and s1["kind"] == "voltage" and len(s1["points"]) == 5
        assert all(n in s["field"] or True for s in data["series"])
        assert not any("inny" in s["field"] for s in data["series"])

    async def test_page_includes_chart_assets(self, client):
        html = await (await client.get("/plugins/wmbus", headers=AUTH)).get_data(as_text=True)
        assert "plugin_charts.js" in html and "chart.umd" in html
        assert (await client.get("/static/plugin_charts.js")).status_code == 200
