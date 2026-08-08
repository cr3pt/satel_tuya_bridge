"""Alarmy progowe na wartościach liczbowych (np. temperatura kotła Wolf).

Reguły definiowane w config.yaml (sekcja `alarm_rules`), np.:

  alarm_rules:
    - name: "Zbyt wysoka temp. zasilania"
      sensor: kociol_temp_zasilania
      condition: "above"       # above | below
      threshold: 85
      hysteresis: 2            # o ile musi spaść/wzrosnąć, żeby zamknąć alarm
    - name: "Brak danych z kotła"
      sensor: kociol_temp_zasilania
      condition: "stale"       # brak nowych odczytów przez X sekund
      max_age_seconds: 600

Przy przekroczeniu progu: zapis do tabeli `alerts` (widoczny w panelu jako
czerwony baner) + opcjonalne powiadomienie push przez ntfy.sh (lekkie,
jeden POST HTTP, bez zależności) lub dowolny webhook.
"""
from __future__ import annotations

import asyncio
import logging
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
        # stan w pamięci: rule_name -> (alert_id albo None, last_value, last_ts)
        self._state: dict[str, dict] = {r["name"]: {"alert_id": None} for r in rules}
        self._last_seen: dict[str, float] = {}

    def on_value(self, sensor: str, value) -> None:
        self._last_seen[sensor] = time.time()  # aktualizowane niezależnie od typu wartości - liczy się sam fakt odczytu
        if not isinstance(value, (int, float)):
            return  # progi liczbowe nie mają sensu dla wartości tekstowych (np. status palnika)
        for rule in self.rules:
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
        """Wywoływać cyklicznie (np. co 60s) z rules_engine - wykrywa brak
        nowych odczytów (np. ebusd/moduł eBUS przestał odpowiadać)."""
        now = time.time()
        for rule in self.rules:
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
        if self.notifier.enabled:
            asyncio.create_task(self.notifier.send(f"⚠️ {title}", message, priority="high"))
