import os
import stat
import sys
import time

import pytest

from bridge.history_store import HistoryStore


@pytest.mark.skipif(sys.platform == "win32", reason="uprawnienia POSIX niedostępne na Windows")
class TestFilePermissions:
    def test_db_file_is_created_with_owner_only_permissions(self, tmp_path):
        db_path = tmp_path / "history.db"
        HistoryStore(str(db_path))
        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == 0o600

    def test_overly_permissive_existing_file_is_tightened_on_open(self, tmp_path):
        db_path = tmp_path / "history.db"
        HistoryStore(str(db_path))
        os.chmod(db_path, 0o644)
        HistoryStore(str(db_path))
        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == 0o600


class TestReadings:
    def test_write_and_read_reading(self, history):
        history.write_reading("kociol_temp", 72.5)
        result = history.get_history("kociol_temp", 0, time.time() + 1)
        assert len(result) == 1
        assert result[0]["v"] == 72.5

    def test_get_sensors_returns_distinct_names(self, history):
        history.write_reading("sensor_a", 1.0)
        history.write_reading("sensor_a", 2.0)
        history.write_reading("sensor_b", 3.0)
        assert set(history.get_sensors()) == {"sensor_a", "sensor_b"}

    def test_history_respects_time_range(self, history):
        now = time.time()
        history.write_reading("s", 1.0, ts=now - 1000)
        history.write_reading("s", 2.0, ts=now)
        result = history.get_history("s", now - 10, now + 10)
        assert len(result) == 1
        assert result[0]["v"] == 2.0


class TestEvents:
    def test_write_and_read_event(self, history):
        history.write_event("motion", source="satel_zone:12", detail={"device": "gniazdko"})
        events = history.get_events(None, 0, time.time() + 1)
        assert len(events) == 1
        assert events[0]["type"] == "motion"
        assert events[0]["detail"]["device"] == "gniazdko"

    def test_filter_events_by_type(self, history):
        history.write_event("motion", source="a")
        history.write_event("system", source="b")
        events = history.get_events("motion", 0, time.time() + 1)
        assert len(events) == 1
        assert events[0]["type"] == "motion"

    def test_events_limit(self, history):
        for i in range(10):
            history.write_event("motion", source=f"s{i}")
        events = history.get_events(None, 0, time.time() + 1, limit=3)
        assert len(events) == 3


class TestAlerts:
    def test_open_and_resolve_alert(self, history):
        alert_id = history.open_alert("Zbyt wysoka temp", "kociol", {"value": 90})
        active = history.get_active_alerts()
        assert len(active) == 1
        assert active[0]["id"] == alert_id

        history.resolve_alert(alert_id)
        assert history.get_active_alerts() == []

        past = history.get_alerts_history(0, time.time() + 1)
        assert len(past) == 1
        assert past[0]["resolved_ts"] is not None


class TestStatus:
    def test_set_and_get_status(self, history):
        history.set_status("tuya:swiatlo", "tuya", "włączone", label="Światło")
        result = history.get_status("tuya")
        assert len(result) == 1
        assert result[0]["value"] == "włączone"
        assert result[0]["label"] == "Światło"

    def test_status_upsert_does_not_duplicate(self, history):
        history.set_status("satel_input:12", "satel_input", "OK", label="Salon")
        history.set_status("satel_input:12", "satel_input", "naruszone", label="Salon")
        result = history.get_status("satel_input")
        assert len(result) == 1
        assert result[0]["value"] == "naruszone"

    def test_status_filter_by_category(self, history):
        history.set_status("a", "tuya", "1")
        history.set_status("b", "satel_input", "2")
        assert len(history.get_status("tuya")) == 1
        assert len(history.get_status()) == 2

    def test_get_status_value_returns_current_value(self, history):
        history.set_status("satel_input:3", "satel_input", "naruszone")
        assert history.get_status_value("satel_input:3") == "naruszone"

    def test_get_status_value_reflects_latest_update(self, history):
        history.set_status("satel_input:3", "satel_input", "naruszone")
        history.set_status("satel_input:3", "satel_input", "OK")
        assert history.get_status_value("satel_input:3") == "OK"

    def test_get_status_value_unknown_key_is_none(self, history):
        assert history.get_status_value("brak_takiego_klucza") is None

    def test_prune_status_removes_keys_not_in_keep_set(self, history):
        history.set_status("satel_input:1", "satel_input", "OK", label="Jeden")
        history.set_status("satel_input:2", "satel_input", "OK", label="Dwa")
        history.set_status("satel_input:3", "satel_input", "OK", label="Trzy")
        removed = history.prune_status("satel_input", {"satel_input:1", "satel_input:3"})
        assert removed == 1
        remaining = {s["key"] for s in history.get_status("satel_input")}
        assert remaining == {"satel_input:1", "satel_input:3"}

    def test_prune_status_only_touches_given_category(self, history):
        history.set_status("satel_input:1", "satel_input", "OK")
        history.set_status("tuya:swiatlo", "tuya", "włączone")
        history.prune_status("satel_input", set())
        assert history.get_status("satel_input") == []
        assert len(history.get_status("tuya")) == 1

    def test_prune_status_empty_keep_set_removes_all_in_category(self, history):
        history.set_status("satel_output:5", "satel_output", "wyłączone")
        history.set_status("satel_output:6", "satel_output", "wyłączone")
        removed = history.prune_status("satel_output", set())
        assert removed == 2
        assert history.get_status("satel_output") == []

    def test_prune_status_keeps_everything_when_all_keys_still_valid(self, history):
        history.set_status("satel_input:1", "satel_input", "OK")
        history.set_status("satel_input:2", "satel_input", "OK")
        removed = history.prune_status("satel_input", {"satel_input:1", "satel_input:2"})
        assert removed == 0
        assert len(history.get_status("satel_input")) == 2


class TestLogs:
    def test_get_log_loggers_returns_distinct_sorted_names(self, history):
        history.write_log("bridge", "bridge.satel", "INFO", "połączono")
        history.write_log("bridge", "bridge.tuya_local", "DEBUG", "odpytano")
        history.write_log("bridge", "bridge.satel", "WARNING", "rozłączono")
        assert history.get_log_loggers() == ["bridge.satel", "bridge.tuya_local"]

    def test_get_log_loggers_filters_by_component(self, history):
        history.write_log("bridge", "bridge.satel", "INFO", "a")
        history.write_log("dashboard", "dashboard.app", "INFO", "b")
        assert history.get_log_loggers("bridge") == ["bridge.satel"]
        assert history.get_log_loggers("dashboard") == ["dashboard.app"]

    def test_get_log_loggers_empty_when_no_logs(self, history):
        assert history.get_log_loggers() == []


class TestCommandQueue:
    def test_enqueue_and_fetch_pending(self, history):
        cmd_id = history.enqueue_command("satel_arm", {"partitions": [1], "mode": 0})
        pending = history.get_pending_commands()
        assert len(pending) == 1
        assert pending[0]["id"] == cmd_id
        assert pending[0]["params"]["partitions"] == [1]

    def test_mark_done_removes_from_pending(self, history):
        cmd_id = history.enqueue_command("satel_disarm", {"partitions": [1]})
        history.mark_command_done(cmd_id, {"message": "ok"})
        assert history.get_pending_commands() == []
        cmd = history.get_command(cmd_id)
        assert cmd["status"] == "done"
        assert cmd["result"]["message"] == "ok"

    def test_mark_error(self, history):
        cmd_id = history.enqueue_command("satel_arm", {"partitions": [1], "mode": 0})
        history.mark_command_error(cmd_id, "połączenie nieudane")
        cmd = history.get_command(cmd_id)
        assert cmd["status"] == "error"
        assert cmd["result"]["error"] == "połączenie nieudane"

    def test_get_nonexistent_command_returns_none(self, history):
        assert history.get_command(9999) is None
