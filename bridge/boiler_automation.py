"""Automatyka wyzwalana wartościami z kotła (np. ochrona przed mrozem:
bardzo niska temperatura na zewnątrz -> włącz grzałkę w garażu).

Podobne w duchu do alarmów progowych (bridge/alarms.py), ale zamiast
tworzyć alert w panelu, WYWOŁUJE SCENĘ (bridge/scenes.py). Ma własny
cooldown na regułę, żeby wahania wartości blisko progu nie odpalały
sceny w kółko co odczyt.

Przykład w config.yaml:
  boiler_rules:
    - name: "Ochrona przed mrozem"
      sensor: kociol_temp_zewnetrzna
      condition: below       # above | below
      threshold: -5
      scene: "ochrona_przed_mrozem"
      cooldown_seconds: 1800   # nie częściej niż co 30 minut
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("bridge.boiler_automation")


class BoilerAutomation:
    def __init__(self, rules: list[dict], scene_runner):
        self.rules = rules or []
        self.scene_runner = scene_runner
        self._last_triggered: dict[str, float] = {}

    async def on_value(self, sensor: str, value) -> None:
        if not isinstance(value, (int, float)):
            return  # progi liczbowe nie mają sensu dla wartości tekstowych
        for rule in self.rules:
            if rule["sensor"] != sensor:
                continue
            breached = value > rule["threshold"] if rule["condition"] == "above" else value < rule["threshold"]
            if not breached:
                continue

            cooldown = rule.get("cooldown_seconds", 900)
            last = self._last_triggered.get(rule["name"], 0)
            if time.time() - last < cooldown:
                continue  # wciąż w okresie karencji od ostatniego wywołania

            self._last_triggered[rule["name"]] = time.time()
            logger.info(
                "Automatyka kotła: '%s' (%s=%s %s %s) -> scena '%s'",
                rule["name"], sensor, value, rule["condition"], rule["threshold"], rule["scene"],
            )
            await self.scene_runner.run(rule["scene"])
