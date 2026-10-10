from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from .history_store import HistoryStore
from .notifier import Notifier

logger = logging.getLogger("bridge.alarms")


class AlarmEngine:
    def __init__(self, rules: list[dict], history: HistoryStore, notify_cfg: Optional[dict] = None):
        self.rules = rules
        self.history = history
        self.notifier = Notifier(notify_cfg)
        self._state: dict[str, dict] = {r["name"]: {"alert_id": None} for r in rules}
        self._last_seen: dict[str, float] = {}
        # on_value bywa wołane z wątków paho-mqtt (np. wM-Bus), check_stale_sensors
        # z pętli asyncio - lista reguł może się zmieniać (add_rule) w trakcie.
        self._rules_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_notifications: set = set()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Pętla asyncio do powiadomień wysyłanych Z OBCYCH WĄTKÓW (callbacki MQTT)."""
        self._loop = loop

    def add_rule(self, rule: dict) -> bool:
        """Dodaje regułę w trakcie działania (np. progi napięcia faz wM-Bus,
        znane dopiero po pierwszym telegramie). False, jeśli nazwa już istnieje."""
        with self._rules_lock:
            if rule["name"] in self._state:
                return False
            self._state[rule["name"]] = {"alert_id": None}
            self.rules = [*self.rules, rule]
            return True

    def on_value(self, sensor: str, value) -> None:
        self._last_seen[sensor] = time.time()
        if not isinstance(value, (int, float)):
            return
        for rule in list(self.rules):
            if rule["sensor"] != sensor or rule["condition"] == "stale":
                continue
            self._check_threshold_rule(rule, value)

    def _check_threshold_rule(self, rule: dict, value: float) -> None:
        state = self._state[rule["name"]]
        threshold = rule["threshold"]
        hysteresis = rule.get("hysteresis", 0)
        condition = rule["condition"]

        is_breached = value > threshold if condition == "above" else value < threshold
        currently_open = state["alert_id"] is not None

        if is_breached and not currently_open:
            alert_id = self.history.open_alert(
                rule["name"], rule["sensor"], {"value": value, "threshold": threshold}
            )
            state["alert_id"] = alert_id
            logger.warning("ALARM: %s (wartość=%s, próg=%s)", rule["name"], value, threshold)
            self._notify(rule["name"], f"{rule['sensor']} = {value} (próg {condition} {threshold})")

        elif currently_open:
            recovered = (
                value <= threshold - hysteresis if condition == "above"
                else value >= threshold + hysteresis
            )
            if recovered:
                self.history.resolve_alert(state["alert_id"])
                logger.info("Alarm ustąpił: %s (wartość=%s)", rule["name"], value)
                state["alert_id"] = None

    async def check_stale_sensors(self) -> None:
        now = time.time()
        for rule in list(self.rules):
            if rule["condition"] != "stale":
                continue
            sensor = rule["sensor"]
            last_ts = self._last_seen.get(sensor)
            state = self._state[rule["name"]]
            is_stale = last_ts is None or (now - last_ts) > rule["max_age_seconds"]
            currently_open = state["alert_id"] is not None

            if is_stale and not currently_open:
                alert_id = self.history.open_alert(rule["name"], sensor, {"last_seen": last_ts})
                state["alert_id"] = alert_id
                logger.warning("ALARM: %s (brak danych)", rule["name"])
                self._notify(rule["name"], f"Brak nowych danych z '{sensor}'")
            elif not is_stale and currently_open:
                self.history.resolve_alert(state["alert_id"])
                state["alert_id"] = None

    def _notify(self, title: str, message: str) -> None:
        if not self.notifier.enabled:
            return
        coro = self.notifier.send(f"⚠️ {title}", message, priority="high")
        try:
            task = asyncio.get_running_loop().create_task(coro)
            self._pending_notifications.add(task)
            task.add_done_callback(self._pending_notifications.discard)
            return
        except RuntimeError:
            pass
        # wywołanie z obcego wątku (callback MQTT) - tu nie ma pętli asyncio
        if self._loop is None:
            coro.close()
            logger.warning("Alarm '%s': brak pętli asyncio - pomijam powiadomienie push", title)
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)
