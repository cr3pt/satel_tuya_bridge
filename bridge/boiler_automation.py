# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

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
            return
        for rule in self.rules:
            if rule["sensor"] != sensor:
                continue
            breached = value > rule["threshold"] if rule["condition"] == "above" else value < rule["threshold"]
            if not breached:
                continue

            cooldown = rule.get("cooldown_seconds", 900)
            last = self._last_triggered.get(rule["name"], 0)
            if time.time() - last < cooldown:
                continue

            self._last_triggered[rule["name"]] = time.time()
            logger.info(
                "Automatyka kotła: '%s' (%s=%s %s %s) -> scena '%s'",
                rule["name"], sensor, value, rule["condition"], rule["threshold"], rule["scene"],
            )
            await self.scene_runner.run(rule["scene"])
