# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("bridge.scenes")


class _SceneSuperseded(Exception):
    pass


class SceneRunner:
    def __init__(self, scenes_cfg: list[dict], tuya, satel, satel_virtual_inputs=None,
                 virtual_input_mode: str = "library", history=None,
                 on_tuya_state: Callable[[str, str], Awaitable[None]] | None = None,
                 on_satel_output: Callable[[int], object] | None = None):
        self.scenes = {s["name"]: s for s in (scenes_cfg or [])}
        self.tuya = tuya
        self.satel = satel
        self.satel_virtual_inputs = satel_virtual_inputs
        self.virtual_input_mode = virtual_input_mode
        self.history = history
        self._on_tuya_state = on_tuya_state
        self._on_satel_output = on_satel_output
        self._gen: dict[str, int] = {}
        self._active: dict[str, int] = {}

    def exists(self, scene_name: str) -> bool:
        return scene_name in self.scenes

    @staticmethod
    def _action_target(action: dict, satel) -> tuple[str, object] | None:
        kind = action.get("type")
        if kind in ("tuya_on", "tuya_off", "tuya_on_for_duration"):
            return "tuya", action.get("device")
        if kind == "satel_output":
            return "satel_output", action.get("output_number")
        if kind == "satel_virtual_input":
            number = (getattr(satel, "virtual_inputs", None) or {}).get(action.get("virtual_input"))
            return ("satel_output", number) if number is not None else None
        return None

    def invalidate_device(self, system: str, ident: object) -> None:
        for name, scene in self.scenes.items():
            if any(self._action_target(a, self.satel) == (system, ident) for a in scene.get("actions", [])):
                self._gen[name] = self._gen.get(name, 0) + 1
                logger.info("Scena '%s': cel zmieniono z zewnątrz - trwające opóźnienia zostają anulowane", name)

    async def run(self, scene_name: str) -> None:
        scene = self.scenes.get(scene_name)
        if scene is None:
            logger.error("Nieznana scena: '%s' (sprawdź sekcję 'scenes' w config.yaml)", scene_name)
            return
        if not scene.get("enabled", True):
            logger.debug("Scena '%s' pominięta - wyłączona w edytorze", scene_name)
            return
        logger.info("Uruchamiam scenę '%s' (%d akcji)", scene_name, len(scene.get("actions", [])))
        if self.history:
            self.history.write_event("scene", source=scene_name, detail={"actions": len(scene.get("actions", []))})
            self.history.set_status(f"automation:{scene_name}", "automation", "uruchomiona", label=scene_name)
        self._gen[scene_name] = self._gen.get(scene_name, 0) + 1
        token = self._gen[scene_name]
        self._active[scene_name] = self._active.get(scene_name, 0) + 1
        try:
            for action in scene.get("actions", []):
                try:
                    await self._run_action(action, scene_name)
                except Exception:
                    logger.exception("Błąd akcji w scenie '%s': %s", scene_name, action)
                if action.get("type") == "delay" and self._gen.get(scene_name) != token:
                    raise _SceneSuperseded
        except _SceneSuperseded:
            logger.info("Scena '%s': dalsze akcje pominięte - uruchomiono ją ponownie lub anulowano", scene_name)
        finally:
            self._active[scene_name] -= 1
            if self.history and self._active[scene_name] == 0:
                self.history.set_status(f"automation:{scene_name}", "automation", "bezczynna", label=scene_name)

    async def _run_action(self, action: dict, scene_name: str) -> None:
        kind = action["type"]
        reason = f"scena '{scene_name}'"
        if kind == "tuya_on":
            await self.tuya.turn_on(action["device"], action.get("dimmer_level_percent"), reason=reason)
            if self._on_tuya_state:
                await self._on_tuya_state(action["device"], "włączone")
        elif kind == "tuya_off":
            await self.tuya.turn_off(action["device"], reason=reason)
            if self._on_tuya_state:
                await self._on_tuya_state(action["device"], "wyłączone")
        elif kind == "tuya_on_for_duration":
            device = action["device"]

            async def _on_state_change(is_on: bool, device=device) -> None:
                if self._on_tuya_state:
                    await self._on_tuya_state(device, "włączone" if is_on else "wyłączone")

            asyncio.create_task(self.tuya.turn_on_for_duration(
                device, action["duration_seconds"], action.get("dimmer_level_percent"), reason=reason,
                on_state_change=_on_state_change,
            ))
        elif kind == "satel_output":
            if self._on_satel_output:
                self._on_satel_output(action["output_number"])
            await self.satel.set_output(action["output_number"], action.get("state", True))
        elif kind == "satel_virtual_input":
            name = action["virtual_input"]
            if self.virtual_input_mode == "http":
                if self.satel_virtual_inputs is None:
                    logger.warning(
                        "Scena używa wirtualnego wejścia IP '%s' w trybie 'http', ale brak "
                        "skonfigurowanego klienta HTTP - pomijam", name,
                    )
                    return
                await self.satel_virtual_inputs.trigger(name)
                return
            output_number = self.satel.virtual_inputs.get(name)
            if output_number is None:
                logger.warning(
                    "Nieznane wirtualne wejście IP Satela '%s' w scenie - pomijam", name,
                )
                return
            if self._on_satel_output:
                self._on_satel_output(output_number)
            await self.satel.set_output(output_number, action.get("state", True))
        elif kind == "satel_arm":
            await self.satel.arm(action.get("mode", 0), action.get("partitions"))
        elif kind == "satel_disarm":
            await self.satel.disarm(action.get("partitions"))
        elif kind == "delay":
            await asyncio.sleep(action["seconds"])
        else:
            logger.warning("Nieznany typ akcji w scenie: '%s' - pomijam", kind)
