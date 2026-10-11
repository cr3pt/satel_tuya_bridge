# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import sqlite3
import time

import pytest

from bridge.maintenance import backup_database, purge_old_data


class TestPurgeOldData:
    def test_old_readings_are_deleted(self, history, tmp_path):
        now = time.time()
        history.write_reading("s", 1.0, ts=now - 200 * 86400)
        history.write_reading("s", 2.0, ts=now)
        purge_old_data(history.db_path, retention_days=90)

        remaining = history.get_history("s", 0, now + 1)
        assert len(remaining) == 1
        assert remaining[0]["v"] == 2.0

    def test_old_events_are_deleted(self, history):
        now = time.time()
        history.write_event("motion", source="stary", ts=now - 200 * 86400)
        history.write_event("motion", source="nowy", ts=now)
        purge_old_data(history.db_path, retention_days=90)

        remaining = history.get_events(None, 0, now + 1)
        assert len(remaining) == 1
        assert remaining[0]["source"] == "nowy"

    def test_active_alerts_never_deleted_regardless_of_age(self, history):
        now = time.time()
        alert_id = history.open_alert("Stary aktywny alarm", "s", {})
        conn = sqlite3.connect(history.db_path)
        conn.execute("UPDATE alerts SET started_ts = ? WHERE id = ?", (now - 500 * 86400, alert_id))
        conn.commit()
        conn.close()

        purge_old_data(history.db_path, retention_days=90)
        active = history.get_active_alerts()
        assert len(active) == 1

    def test_resolved_old_alerts_are_deleted(self, history):
        now = time.time()
        alert_id = history.open_alert("Stary rozwiązany alarm", "s", {})
        conn = sqlite3.connect(history.db_path)
        conn.execute(
            "UPDATE alerts SET started_ts = ?, resolved_ts = ? WHERE id = ?",
            (now - 500 * 86400, now - 400 * 86400, alert_id),
        )
        conn.commit()
        conn.close()

        purge_old_data(history.db_path, retention_days=90)
        past = history.get_alerts_history(0, now + 1)
        assert past == []

    def test_pending_commands_never_deleted_regardless_of_age(self, history):
        cmd_id = history.enqueue_command("satel_arm", {"partitions": [1], "mode": 0})
        conn = sqlite3.connect(history.db_path)
        conn.execute("UPDATE commands SET ts = ? WHERE id = ?", (time.time() - 500 * 86400, cmd_id))
        conn.commit()
        conn.close()

        purge_old_data(history.db_path, retention_days=90)
        assert history.get_command(cmd_id) is not None
        assert history.get_command(cmd_id)["status"] == "pending"

    def test_done_old_commands_are_deleted(self, history):
        now = time.time()
        cmd_id = history.enqueue_command("satel_arm", {"partitions": [1], "mode": 0})
        history.mark_command_done(cmd_id, {"message": "ok"})
        conn = sqlite3.connect(history.db_path)
        conn.execute("UPDATE commands SET done_ts = ? WHERE id = ?", (now - 200 * 86400, cmd_id))
        conn.commit()
        conn.close()

        purge_old_data(history.db_path, retention_days=90)
        assert history.get_command(cmd_id) is None

    def test_purge_returns_row_counts(self, history):
        now = time.time()
        history.write_reading("s", 1.0, ts=now - 200 * 86400)
        history.write_reading("s", 2.0, ts=now - 200 * 86400)
        result = purge_old_data(history.db_path, retention_days=90)
        assert result["readings"] == 2
        assert "bytes_reclaimed" in result

    def test_purge_reclaims_disk_space_via_vacuum(self, history, tmp_path):
        import pathlib

        now = time.time()
        for i in range(500):
            history.write_reading(f"sensor_{i % 5}", float(i), ts=now - 200 * 86400)

        size_before = pathlib.Path(history.db_path).stat().st_size
        purge_old_data(history.db_path, retention_days=90)
        size_after = pathlib.Path(history.db_path).stat().st_size

        assert size_after <= size_before


class TestBackupDatabase:
    def test_backup_creates_file(self, history, tmp_path):
        history.write_reading("s", 1.0)
        backup_dir = tmp_path / "backups"
        result = backup_database(history.db_path, str(backup_dir), keep_count=7)

        assert result is not None
        assert result.exists()
        assert result.parent == backup_dir

    def test_backup_contains_same_data_as_source(self, history, tmp_path):
        history.write_reading("temperatura", 42.0)
        backup_dir = tmp_path / "backups"
        result = backup_database(history.db_path, str(backup_dir), keep_count=7)

        conn = sqlite3.connect(str(result))
        rows = conn.execute("SELECT sensor, value FROM readings").fetchall()
        conn.close()
        assert rows == [("temperatura", 42.0)]

    def test_old_backups_pruned_to_keep_count(self, history, tmp_path, mocker):
        backup_dir = tmp_path / "backups"
        counter = iter(range(10))
        mocker.patch("bridge.maintenance.time.strftime", side_effect=lambda fmt: f"{next(counter):04d}")

        created = [backup_database(history.db_path, str(backup_dir), keep_count=3) for _ in range(5)]

        remaining = list(backup_dir.glob("*.db"))
        assert len(remaining) == 3
        assert set(p.name for p in remaining) == {p.name for p in created[-3:]}

    def test_backup_dir_created_if_missing(self, history, tmp_path):
        backup_dir = tmp_path / "nieistniejacy" / "zagniezdzony" / "katalog"
        result = backup_database(history.db_path, str(backup_dir), keep_count=7)
        assert result.exists()


class TestLockRetry:

    def test_purge_retries_on_locked_database(self, history, mocker):
        import sqlite3 as sqlite3_module

        real_connect = sqlite3_module.connect
        call_count = {"n": 0}

        def flaky_connect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise sqlite3_module.OperationalError("database is locked")
            return real_connect(*args, **kwargs)

        mocker.patch("bridge.maintenance.sqlite3.connect", side_effect=flaky_connect)
        mocker.patch("bridge.maintenance.time.sleep")

        history.write_reading("s", 1.0, ts=time.time() - 200 * 86400)
        result = purge_old_data(history.db_path, retention_days=90)
        assert call_count["n"] == 2
        assert result["readings"] == 1

    def test_purge_gives_up_after_max_attempts(self, history, mocker):
        import sqlite3 as sqlite3_module

        mocker.patch(
            "bridge.maintenance.sqlite3.connect",
            side_effect=sqlite3_module.OperationalError("database is locked"),
        )
        mocker.patch("bridge.maintenance.time.sleep")

        with pytest.raises(sqlite3_module.OperationalError):
            purge_old_data(history.db_path, retention_days=90)
