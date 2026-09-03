from __future__ import annotations

import logging
import pathlib
import sqlite3
import time

logger = logging.getLogger("bridge.maintenance")

_LOCK_RETRY_ATTEMPTS = 3
_LOCK_RETRY_DELAY_SECONDS = 5


def _connect_with_retry(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=30)


def purge_old_data(db_path: str, retention_days: int) -> dict:
    cutoff = time.time() - retention_days * 86400
    last_error = None

    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        conn = None
        try:
            conn = _connect_with_retry(db_path)
            counts = {}
            cur = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
            counts["readings"] = cur.rowcount
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            counts["events"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM alerts WHERE resolved_ts IS NOT NULL AND resolved_ts < ?", (cutoff,)
            )
            counts["alerts"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM commands WHERE status != 'pending' AND done_ts IS NOT NULL AND done_ts < ?",
                (cutoff,),
            )
            counts["commands"] = cur.rowcount
            conn.commit()

            size_before = pathlib.Path(db_path).stat().st_size
            conn.execute("VACUUM")
            size_after = pathlib.Path(db_path).stat().st_size
            counts["bytes_reclaimed"] = size_before - size_after

            logger.info("Czyszczenie bazy zakończone: %s", counts)
            return counts
        except sqlite3.OperationalError as exc:
            last_error = exc
            logger.warning(
                "Baza zajęta podczas czyszczenia (próba %s/%s): %s",
                attempt + 1, _LOCK_RETRY_ATTEMPTS, exc,
            )
            time.sleep(_LOCK_RETRY_DELAY_SECONDS)
        finally:
            if conn:
                conn.close()

    logger.error("Nie udało się wyczyścić bazy po %s próbach: %s", _LOCK_RETRY_ATTEMPTS, last_error)
    raise last_error


def backup_database(db_path: str, backup_dir: str, keep_count: int = 7) -> pathlib.Path | None:
    backup_path = pathlib.Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    db_name = pathlib.Path(db_path).stem
    dest = backup_path / f"{db_name}_{timestamp}.db"

    last_error = None
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        source_conn = None
        dest_conn = None
        try:
            source_conn = _connect_with_retry(db_path)
            dest_conn = sqlite3.connect(str(dest))
            source_conn.backup(dest_conn)
            logger.info("Backup zapisany: %s", dest)
            break
        except sqlite3.OperationalError as exc:
            last_error = exc
            logger.warning(
                "Baza zajęta podczas backupu (próba %s/%s): %s",
                attempt + 1, _LOCK_RETRY_ATTEMPTS, exc,
            )
            time.sleep(_LOCK_RETRY_DELAY_SECONDS)
        finally:
            if source_conn:
                source_conn.close()
            if dest_conn:
                dest_conn.close()
    else:
        logger.error("Nie udało się wykonać backupu po %s próbach: %s", _LOCK_RETRY_ATTEMPTS, last_error)
        if dest.exists():
            dest.unlink()
        return None

    existing = sorted(backup_path.glob(f"{db_name}_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old_backup in existing[keep_count:]:
        old_backup.unlink()
        logger.info("Usunięto stary backup: %s", old_backup)

    return dest


def run_maintenance(config) -> dict:
    maint_cfg = config.raw.get("maintenance", {})
    db_path = config.raw.get("dashboard", {}).get("db_path", "bridge_history.db")
    summary = {}

    if maint_cfg.get("purge_enabled", True):
        summary["purge"] = purge_old_data(db_path, maint_cfg.get("retention_days", 180))

    if maint_cfg.get("backup_enabled", False):
        backup_path = backup_database(
            db_path,
            maint_cfg.get("backup_dir", "backups"),
            maint_cfg.get("backup_keep_count", 7),
        )
        summary["backup"] = str(backup_path) if backup_path else None

    return summary
