"""Sceny - nazwane grupy akcji wykonywane razem, jednym wywołaniem.

To wspólny fundament dla kilku funkcji naraz: reguła ruchu może wywołać
scenę zamiast pojedynczego "włącz urządzenie", eskalacja przy powtarzających
się naruszeniach wywołuje inną (mocniejszą) scenę, automatyka kotła
(np. ochrona przed mrozem) też wywołuje scenę, tak samo harmonogram.
Zamiast pięciu osobnych mechanizmów "zrób coś na wielu urządzeniach" -
jeden, reużywalny.

Przykład definicji w config.yaml:
  scenes:
    - name: "wieczorne_powitanie"
      actions:
        - {type: tuya_on, device: swiatlo_korytarz, dimmer_level_percent: 30}
        - {type: satel_virtual_input, virtual_input: light_on_living_room}

    - name: "wykryto_intruza"
      actions:
        - {type: tuya_on, device: gniazdko_salon}
        - {type: tuya_on, device: swiatlo_korytarz}
        - {type: satel_output, output_number: 1, state: true}
        - {type: delay, seconds: 2}
        - {type: satel_virtual_input, virtual_input: motion_hall_camera}
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("bridge.scenes")


class SceneRunner:
    def __init__(self, scenes_cfg: list[dict], tuya, satel, satel_virtual_inputs, history=None):
        self.scenes = {s["name"]: s for s in (scenes_cfg or [])}
        self.tuya = tuya
        self.satel = satel
        self.satel_virtual_inputs = satel_virtual_inputs
        self.history = history

    def exists(self, scene_name: str) -> bool:
        return scene_name in self.scenes

    async def run(self, scene_name: str) -> None:
        scene = self.scenes.get(scene_name)
        if scene is None:
            logger.error("Nieznana scena: '%s' (sprawdź sekcję 'scenes' w config.yaml)", scene_name)
            return
        logger.info("Uruchamiam scenę '%s' (%d akcji)", scene_name, len(scene.get("actions", [])))
        if self.history:
            self.history.write_event("scene", source=scene_name, detail={"actions": len(scene.get("actions", []))})
        for action in scene.get("actions", []):
            try:
                await self._run_action(action)
            except Exception:
                # Jedna nieudana akcja (np. martwe urządzenie Tuya) nie może
                # przerwać reszty sceny - reszta akcji i tak powinna się wykonać.
                logger.exception("Błąd akcji w scenie '%s': %s", scene_name, action)

    async def _run_action(self, action: dict) -> None:
        kind = action["type"]
        if kind == "tuya_on":
            await self.tuya.turn_on(action["device"], action.get("dimmer_level_percent"))
        elif kind == "tuya_off":
            await self.tuya.turn_off(action["device"])
        elif kind == "tuya_on_for_duration":
            # nieblokujące - scena nie czeka, aż upłynie czas, żeby wykonać
            # kolejne akcje
            asyncio.create_task(self.tuya.turn_on_for_duration(
                action["device"], action["duration_seconds"], action.get("dimmer_level_percent"),
            ))
        elif kind == "satel_output":
            await self.satel.set_output(action["output_number"], action.get("state", True))
        elif kind == "satel_virtual_input":
            await self.satel_virtual_inputs.trigger(action["virtual_input"])
        elif kind == "satel_arm":
            # `partitions` opcjonalne - gdy akcja go nie poda (stare sceny
            # sprzed tego pola, albo instalacja z jedną strefą), arm()
            # sam spada na domyślną (SatelIntegraClient.partition) - patrz
            # tam. Z wieloma strefami warto podać je JAWNIE per scena
            # (jedna scena może uzbrajać tylko "Garaż", inna wszystko).
            await self.satel.arm(action.get("mode", 0), action.get("partitions"))
        elif kind == "satel_disarm":
            await self.satel.disarm(action.get("partitions"))
        elif kind == "delay":
            await asyncio.sleep(action["seconds"])
        else:
            logger.warning("Nieznany typ akcji w scenie: '%s' - pomijam", kind)
