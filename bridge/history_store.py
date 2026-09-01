from __future__ import annotations

import json
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
    type TEXT NOT NULL,        -- motion | light_to_satel | camera_alarm | system | alert
    source TEXT,               -- np. 'satel_zone:12', 'tuya:swiatlo_korytarz'
    detail TEXT                -- dowolny JSON z dodatkowym kontekstem
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
    key TEXT PRIMARY KEY,       -- unikalny identyfikator, np. 'tuya:swiatlo_korytarz'
    category TEXT NOT NULL,     -- tuya | satel_input | satel_output | boiler
    label TEXT,                 -- czytelna nazwa do wyświetlenia
    value TEXT NOT NULL,        -- ostatnia wartość (jako tekst - bool/liczba/string)
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_category ON status(category);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    command TEXT NOT NULL,      -- np. 'satel_arm', 'satel_disarm'
    params TEXT NOT NULL,       -- JSON, np. {"partitions": [1], "mode": 0}
    status TEXT NOT NULL,       -- pending | done | error
    result TEXT,                -- JSON z wynikiem/błędem, wypełniane po wykonaniu
    done_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,   -- 'bridge' | 'dashboard' - który proces
    logger TEXT NOT NULL,      -- np. 'bridge.satel'
    level TEXT NOT NULL,       -- DEBUG | INFO | WARNING | ERROR | CRITICAL
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
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
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def write_reading(self, sensor: str, value: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO readings (ts, sensor, value) VALUES (?, ?, ?)",
                (ts, sensor, value),
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

    def resolve_alert(self, alert_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET resolved_ts = ? WHERE id = ?", (time.time(), alert_id)
            )
            self._conn.commit()

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
