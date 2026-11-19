# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass


SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    sensor TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_sensor_ts ON readings(sensor, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    source TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name TEXT NOT NULL,
    started_ts REAL NOT NULL,
    resolved_ts REAL,
    sensor TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_resolved ON alerts(resolved_ts);

CREATE TABLE IF NOT EXISTS status (
    key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    label TEXT,
    value TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_category ON status(category);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    command TEXT NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    done_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    logger TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);

-- Liczniki energii: odczyty trzymane bezterminowo, poza `readings`, którą
-- czyści maintenance.purge_old_data (retention_days).
CREATE TABLE IF NOT EXISTS energy_sensors (
    sensor TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS energy_readings (
    sensor TEXT NOT NULL,
    ts REAL NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (sensor, ts)
) WITHOUT ROWID;
"""

_LOG_LEVEL_ORDER = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LOG_LEVELS_AT_OR_ABOVE = {
    level: _LOG_LEVEL_ORDER[i:] for i, level in enumerate(_LOG_LEVEL_ORDER)
}


@dataclass
class HistoryStore:
    db_path: str

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        # Ten sam plik jest otwierany RÓWNOCZEŚNIE przez dwa osobne procesy
        # (demon bridge'a i panel Quart) - domyślny tryb dziennika SQLite
        # (rollback journal) blokuje czytelników na czas zapisu, co przy
        # krótkiej, ale realnej kolizji dawało "database is locked" (patrz
        # /healthz -> history.get_status). WAL pozwala czytać w trakcie
        # zapisu z innego procesu; busy_timeout dodatkowo każe SQLite czekać
        # zamiast od razu zgłaszać błąd, gdyby mimo to doszło do kolizji
        # zapis-zapis.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        for suffix in ("", "-wal", "-shm"):
            # WAL tworzy pliki pomocnicze obok głównej bazy - trzymają też
            # dane do niedawnego zapisu (więc tak samo mogą zawierać klucze),
            # a domyślny umask zrobiłby je czytelnymi dla innych.
            try:
                os.chmod(f"{self.db_path}{suffix}", 0o600)
            except OSError:
                pass

    def write_reading(self, sensor: str, value: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO readings (ts, sensor, value) VALUES (?, ?, ?)",
                (ts, sensor, value),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO energy_readings (sensor, ts, value) "
                "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM energy_sensors WHERE sensor = ?)",
                (sensor, ts, value, sensor),
            )
            self._conn.commit()

    def write_event(self, event_type: str, source: str = "", detail: dict | None = None,
                     ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, type, source, detail) VALUES (?, ?, ?, ?)",
                (ts, event_type, source, json.dumps(detail or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def open_alert(self, rule_name: str, sensor: str, detail: dict) -> int:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts (rule_name, started_ts, sensor, detail) VALUES (?, ?, ?, ?)",
                (rule_name, ts, sensor, json.dumps(detail, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def resolve_alert(self, alert_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE alerts SET resolved_ts = ? WHERE id = ? AND resolved_ts IS NULL", (time.time(), alert_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_status(self, key: str, category: str, value, label: str | None = None,
                   ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO status (key, category, label, value, ts) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, ts = excluded.ts, "
                "label = COALESCE(excluded.label, status.label)",
                (key, category, label, str(value), ts),
            )
            self._conn.commit()

    def get_status_value(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM status WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def get_status(self, category: str | None = None) -> list[dict]:
        query = "SELECT key, category, label, value, ts FROM status"
        params: list = []
        if category:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY category, key"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"key": r[0], "category": r[1], "label": r[2] or r[0], "value": r[3], "ts": r[4]}
            for r in rows
        ]

    def prune_status(self, category: str, keep_keys) -> int:
        keep_keys = list(keep_keys)
        with self._lock:
            if not keep_keys:
                cur = self._conn.execute("DELETE FROM status WHERE category = ?", (category,))
            else:
                placeholders = ",".join("?" * len(keep_keys))
                cur = self._conn.execute(
                    f"DELETE FROM status WHERE category = ? AND key NOT IN ({placeholders})",
                    (category, *keep_keys),
                )
            self._conn.commit()
            return cur.rowcount

    def delete_status(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM status WHERE key = ?", (key,))
            self._conn.commit()

    def enqueue_command(self, command: str, params: dict) -> int:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO commands (ts, command, params, status) VALUES (?, ?, ?, 'pending')",
                (ts, command, json.dumps(params, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_pending_commands(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, command, params FROM commands WHERE status = 'pending' ORDER BY ts"
            ).fetchall()
        return [{"id": r[0], "ts": r[1], "command": r[2], "params": json.loads(r[3])} for r in rows]

    def mark_command_done(self, command_id: int, result: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE commands SET status = 'done', result = ?, done_ts = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), time.time(), command_id),
            )
            self._conn.commit()

    def mark_command_error(self, command_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE commands SET status = 'error', result = ?, done_ts = ? WHERE id = ?",
                (json.dumps({"error": error}, ensure_ascii=False), time.time(), command_id),
            )
            self._conn.commit()

    def get_command(self, command_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, ts, command, params, status, result, done_ts FROM commands WHERE id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "ts": row[1], "command": row[2], "params": json.loads(row[3]),
            "status": row[4], "result": json.loads(row[5]) if row[5] else None, "done_ts": row[6],
        }

    def get_history(self, sensor: str, ts_from: float, ts_to: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, value FROM readings WHERE sensor = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (sensor, ts_from, ts_to),
            ).fetchall()
        return [{"t": r[0], "v": r[1]} for r in rows]

    def set_energy_sensors(self, sensors) -> None:
        """Czujniki, których odczyty trafiają też do `energy_readings`. Nowo
        dodane dostają od razu kopię tego, co jeszcze jest w `readings`.
        Usunięcie z listy zatrzymuje tylko dalsze kopiowanie - zebrane
        odczyty zostają."""
        wanted = set(sensors)
        with self._lock:
            current = {r[0] for r in self._conn.execute("SELECT sensor FROM energy_sensors")}
            for sensor in current - wanted:
                self._conn.execute("DELETE FROM energy_sensors WHERE sensor = ?", (sensor,))
            for sensor in wanted - current:
                self._conn.execute("INSERT INTO energy_sensors (sensor) VALUES (?)", (sensor,))
                self._conn.execute(
                    "INSERT OR IGNORE INTO energy_readings (sensor, ts, value) "
                    "SELECT sensor, ts, value FROM readings WHERE sensor = ?",
                    (sensor,),
                )
            self._conn.commit()

    def get_energy_sensors(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT sensor FROM energy_sensors ORDER BY sensor").fetchall()
        return [r[0] for r in rows]

    def get_energy_history(self, sensor: str, ts_from: float, ts_to: float) -> list[dict]:
        """Odczyty licznika energii z bezterminowej tabeli; czujnik spoza niej
        (nigdy nie oznaczony) - z `readings`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, value FROM energy_readings WHERE sensor = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (sensor, ts_from, ts_to),
            ).fetchall()
            known = rows or self._conn.execute(
                "SELECT 1 FROM energy_readings WHERE sensor = ? LIMIT 1", (sensor,),
            ).fetchone()
        if not known:
            return self.get_history(sensor, ts_from, ts_to)
        return [{"t": r[0], "v": r[1]} for r in rows]

    def get_latest_energy_reading(self, sensor: str) -> dict | None:
        """Najnowszy zapisany odczyt czujnika (dokładnie wartość, która
        przyszła z MQTT/ESPHome) - z tabeli bezterminowej, a gdy czujnik nigdy
        nie był w niej zapisany - z `readings`. None = brak odczytów."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, value FROM energy_readings WHERE sensor = ? ORDER BY ts DESC LIMIT 1", (sensor,),
            ).fetchone() or self._conn.execute(
                "SELECT ts, value FROM readings WHERE sensor = ? ORDER BY ts DESC LIMIT 1", (sensor,),
            ).fetchone()
        return {"t": row[0], "v": row[1]} if row else None

    def get_sensors(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT sensor FROM readings ORDER BY sensor").fetchall()
        return [r[0] for r in rows]

    def get_events(self, event_type: str | None, ts_from: float, ts_to: float, limit: int = 500) -> list[dict]:
        query = "SELECT id, ts, type, source, detail FROM events WHERE ts BETWEEN ? AND ?"
        params: list = [ts_from, ts_to]
        if event_type:
            query += " AND type = ?"
            params.append(event_type)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"id": r[0], "ts": r[1], "type": r[2], "source": r[3], "detail": json.loads(r[4] or "{}")}
            for r in rows
        ]

    def get_active_alerts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, rule_name, started_ts, sensor, detail FROM alerts WHERE resolved_ts IS NULL ORDER BY started_ts DESC"
            ).fetchall()
        return [
            {"id": r[0], "rule_name": r[1], "started_ts": r[2], "sensor": r[3], "detail": json.loads(r[4] or "{}")}
            for r in rows
        ]

    def get_alerts_history(self, ts_from: float, ts_to: float, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, rule_name, started_ts, resolved_ts, sensor, detail FROM alerts "
                "WHERE started_ts BETWEEN ? AND ? ORDER BY started_ts DESC LIMIT ?",
                (ts_from, ts_to, limit),
            ).fetchall()
        return [
            {
                "id": r[0], "rule_name": r[1], "started_ts": r[2], "resolved_ts": r[3],
                "sensor": r[4], "detail": json.loads(r[5] or "{}"),
            }
            for r in rows
        ]

    def write_log(self, component: str, logger_name: str, level: str, message: str,
                   ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs (ts, component, logger, level, message) VALUES (?, ?, ?, ?, ?)",
                (ts, component, logger_name, level, message),
            )
            self._conn.commit()

    def get_logs(self, component: str | None = None, logger_name: str | None = None,
                  level: str | None = None, ts_from: float | None = None, limit: int = 300) -> list[dict]:
        query = "SELECT id, ts, component, logger, level, message FROM logs WHERE 1=1"
        params: list = []
        if component:
            query += " AND component = ?"
            params.append(component)
        if logger_name:
            query += " AND logger = ?"
            params.append(logger_name)
        if level:
            allowed = LOG_LEVELS_AT_OR_ABOVE.get(level.upper())
            if allowed:
                placeholders = ",".join("?" * len(allowed))
                query += f" AND level IN ({placeholders})"
                params.extend(allowed)
        if ts_from is not None:
            query += " AND ts >= ?"
            params.append(ts_from)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"id": r[0], "ts": r[1], "component": r[2], "logger": r[3], "level": r[4], "message": r[5]}
            for r in rows
        ]

    def get_log_loggers(self, component: str | None = None) -> list[str]:
        query = "SELECT DISTINCT logger FROM logs WHERE 1=1"
        params: list = []
        if component:
            query += " AND component = ?"
            params.append(component)
        query += " ORDER BY logger"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [r[0] for r in rows]

    def prune_logs(self, keep: int = 10000) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY ts DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()
