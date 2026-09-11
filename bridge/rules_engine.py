from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

from .config import BridgeConfig
from .logging_config import apply_log_levels
from .satel_client import SatelIntegraClient, SatelVirtualInputTrigger
from .tuya_local import TuyaLocalController, format_tuya_dps, extract_tuya_sensor_readings
from .tuya_cloud import TuyaCloudController
from .tuya_dispatch import TuyaController
from .tuya_cloud_events import TuyaCameraMotionListener
from .unifi_protect_events import UnifiProtectMotionListener
from .homewhiz_client import HomeWhizClient
from .ebus_client import EbusMqttListener
from .weather_client import fetch_forecast
from .boiler_output import build_boiler_publishers, MqttBoilerPublisher, TuyaVirtualDevicePublisher
from .history_store import HistoryStore
from .alarms import AlarmEngine
from .resilience import HealthReporter
from .scheduling import is_within_window
from .conditions import evaluate_condition
from .scenes import SceneRunner
from .presence_simulation import PresenceSimulation
from .boiler_automation import BoilerAutomation
from .maintenance import run_maintenance
from .ha_discovery import build_all_entities
from .ha_mqtt import HomeAssistantMqttBridge
from .homekit_bridge import HomeKitBridge, resolve_homekit_config
from .flow_engine import FlowEngine

logger = logging.getLogger("bridge.rules")


def _build_tuya_controller(tuya_cfg: dict) -> TuyaController:
    local = TuyaLocalController(tuya_cfg["devices"], tuya_cfg.get("zigbee_gateways"))
    cloud = TuyaCloudController(tuya_cfg.get("cloud_devices", {}), tuya_cfg.get("cloud", {}))
    return TuyaController(local, cloud)


class RulesEngine:
    def __init__(self, config: BridgeConfig):
        self.config = config

        self.history = HistoryStore(config.raw.get("dashboard", {}).get("db_path", "bridge_history.db"))
        notify_cfg = config.raw.get("notifications")
        if notify_cfg and not notify_cfg.get("enabled", True):
            notify_cfg = None
        self.alarms = AlarmEngine(
            config.raw.get("alarm_rules", []),
            self.history,
            notify_cfg=notify_cfg,
        )
        grace_seconds = (notify_cfg or {}).get("grace_seconds", 120)

        self.tuya = _build_tuya_controller(config.tuya)
        self.satel = SatelIntegraClient(
            config.satel["host"],
            config.satel["port"],
            config.satel.get("integration_key") or "",
            config.satel.get("partition", 1),
            monitored_zones=config.satel.get("monitored_zones"),
            monitored_outputs=config.satel.get("monitored_outputs"),
            virtual_inputs=config.satel.get("virtual_inputs", {}).get("inputs"),
            encryption_key=(
                config.satel.get("encryption_key") if config.satel.get("encryption_enabled") else None
            ),
            history=self.history,
            notify_cfg=notify_cfg,
            grace_seconds=grace_seconds,
            status_poll_interval_seconds=config.satel.get("status_poll_interval_seconds", 3),
        )
        self._vi_mode = config.satel.get("virtual_inputs", {}).get("mode", "library")
        self.satel_virtual_inputs = SatelVirtualInputTrigger(
            config.satel["host"], config.satel.get("virtual_inputs", {}),
        )
        self._active_timers: dict[str, asyncio.Task] = {}
        self._device_state_last_value: dict[tuple[str, str], str] = {}
        self._zone_violated_state: dict[int, bool] = {}
        self._rule_and_state: dict[str, bool] = {}
        self._alarm_status_debounce_task: asyncio.Task | None = None
        self.boiler_publishers = build_boiler_publishers(config, history=self.history)
        self.ebus_listener: EbusMqttListener | None = None
        self._notify_cfg = notify_cfg
        self._grace_seconds = grace_seconds
        self._tuya_health: HealthReporter | None = None

        self._location_cfg = config.raw.get("location")

        self._rule_name_index = self._build_rule_name_index()

        self.scenes = SceneRunner(
            config.raw.get("scenes", []), self.tuya, self.satel,
            satel_virtual_inputs=self.satel_virtual_inputs, virtual_input_mode=self._vi_mode,
            history=self.history,
        )
        self.boiler_automation = BoilerAutomation(config.raw.get("boiler_rules", []), self.scenes)
        self.presence_simulation = PresenceSimulation(
            config.raw.get("presence_simulation", {}), self.tuya, location_cfg=self._location_cfg, history=self.history,
        )
        self.flow_engine = FlowEngine(
            config.raw.get("flows", []), set_target_state=self._set_target_state,
            run_scene=lambda name: self.scenes.run(name), notify_cfg=notify_cfg,
            mqtt_cfg=config.raw.get("mqtt_publish", {}), location_cfg=self._location_cfg,
            get_status_value=self.history.get_status_value, set_tuya_dp=self._set_tuya_dp,
        )

        self._escalation_history: dict[str, list[float]] = {}
        self._camera_rule_last_triggered: dict[str, float] = {}

        self._loop = asyncio.get_event_loop()

        self.ha_mqtt: HomeAssistantMqttBridge | None = None
        self._ha_topic_prefix = config.raw.get("home_assistant", {}).get("state_topic_prefix", "home/bridge")
        if config.raw.get("home_assistant", {}).get("enabled"):
            boiler_topic_prefix = config.boiler_output.get("mqtt", {}).get("topic_prefix", "home/boiler")
            entities = build_all_entities(config, boiler_topic_prefix)
            self.ha_mqtt = HomeAssistantMqttBridge(
                config.raw["home_assistant"], entities, history=self.history,
                notify_cfg=notify_cfg, grace_seconds=grace_seconds,
            )

        self.state_mqtt: HomeAssistantMqttBridge | None = None
        mqtt_publish_cfg = config.raw.get("mqtt_publish", {})
        self._satel_mqtt_topic = mqtt_publish_cfg.get("topics", {}).get("satel", "satel")
        self._tuya_mqtt_topic = mqtt_publish_cfg.get("topics", {}).get("tuya", "tuya")
        self._homewhiz_mqtt_topic = mqtt_publish_cfg.get("topics", {}).get("homewhiz", "homewhiz")
        if mqtt_publish_cfg.get("enabled", True) and config.ebus.get("mqtt_host"):
            self.state_mqtt = HomeAssistantMqttBridge(
                {"mqtt_host": config.ebus["mqtt_host"], "mqtt_port": config.ebus.get("mqtt_port", 1883)},
                entities=[], history=self.history, notify_cfg=notify_cfg, grace_seconds=grace_seconds,
            )

        self.homekit: HomeKitBridge | None = self._build_homekit(config)

    def _build_homekit(self, config: BridgeConfig) -> HomeKitBridge | None:
        """Buduje świeżą instancję HomeKitBridge (i jej akcesoria) z aktualnej
        konfiguracji. Akcesoria HAP-python są budowane raz w konstruktorze i nie
        da się ich potem dodawać/usuwać na żywo - jedyny sposób, żeby dodanie albo
        usunięcie urządzenia Tuya (albo strefy/sceny) faktycznie pojawiło się w
        Home.app bez pełnego restartu procesu, to zbudowanie nowej instancji i
        podmiana starej (patrz reload_config: stop starej, potem start nowej)."""
        homekit_cfg = config.raw.get("homekit", {})
        if not homekit_cfg.get("enabled"):
            return None
        resolved = resolve_homekit_config(config.raw)
        enabled_partitions = {
            name: info["number"] for name, info in resolved["partitions"].items() if info["enabled"]
        }
        exposed_devices = {name: info for name, info in resolved["devices"].items() if info["type"]}
        exposed_scene_names = {name for name, info in resolved["scenes"].items() if info["enabled"]}
        return HomeKitBridge(
            homekit_cfg, self.satel, self.tuya, self.scenes,
            enabled_partitions, exposed_devices, exposed_scene_names,
            persist_path=config.db_path.parent / "homekit_state.json",
            loop=self._loop, history=self.history,
        )

    def _build_rule_name_index(self) -> dict[str, int]:
        return {
            rule["name"]: idx
            for idx, rule in enumerate(self.config.motion_rules)
            if rule.get("name")
        }

    def _rule_gate_open(self, rule: dict, now: datetime) -> bool:
        if not is_within_window(rule.get("time_window"), self._location_cfg, now):
            return False
        condition = rule.get("condition")
        if not condition:
            return True
        try:
            return evaluate_condition(
                condition, get_status_value=self.history.get_status_value,
                location_cfg=self._location_cfg, now=now,
            )
        except Exception:
            logger.exception(
                "Błąd ewaluacji warunku reguły ruchu '%s' - traktuję jako niespełniony",
                rule.get("name", "?"),
            )
            return False

    @staticmethod
    def _rule_sources(rule: dict) -> list[dict]:
        """Normalizuje źródła reguły do listy - reguły bez pola "sources" (stary,
        pojedynczy format) zwracają listę z jednym elementem zbudowanym z
        legacy pól "source"/"zone_number"/"source_device"/"trigger_value",
        więc cała reszta silnika może zawsze operować na liście."""
        sources = rule.get("sources")
        if sources:
            return sources
        entry = {"source": rule.get("source")}
        if rule.get("source") == "satel_zone":
            entry["zone_number"] = rule.get("zone_number")
        else:
            entry["source_device"] = rule.get("source_device")
            entry["trigger_value"] = rule.get("trigger_value")
        return [entry]

    def _source_is_active(self, entry: dict) -> bool:
        if entry.get("source") == "satel_zone":
            return self._zone_violated_state.get(entry.get("zone_number"), False)
        key = (entry.get("source"), entry.get("source_device"))
        return self._device_state_last_value.get(key) == entry.get("trigger_value")

    def _resolve_and_trigger(self, rule: dict, rule_key: str, sources: list[dict]) -> str | None:
        """Dla combinator="and": przelicza, czy WSZYSTKIE źródła są aktualnie
        aktywne, i zwraca "violated"/"restored" tylko na zboczu (przejściu
        stanu), albo None gdy nic się nie zmieniło - unika wielokrotnego
        odpalania reguły przy każdej aktualizacji jednego z wielu źródeł."""
        all_active = all(self._source_is_active(s) for s in sources)
        was_active = self._rule_and_state.get(rule_key, False)
        self._rule_and_state[rule_key] = all_active
        if all_active == was_active:
            return None
        return "violated" if all_active else "restored"

    async def handle_motion(self, source: str, zone_or_device_id, trigger: str = "violated") -> None:
        if source == "satel_zone":
            self._zone_violated_state[zone_or_device_id] = (trigger == "violated")

        await self.flow_engine.handle_motion(source, zone_or_device_id, trigger)

        now = datetime.now()
        for idx, rule in enumerate(self.config.motion_rules):
            if not rule.get("enabled", True):
                continue

            sources = self._rule_sources(rule)
            if not any(
                s.get("source") == source and (
                    source != "satel_zone" or s.get("zone_number") == zone_or_device_id
                )
                for s in sources
            ):
                continue

            rule_key = f"motion_rule_{idx}"
            if rule.get("source_combinator") == "and" and len(sources) > 1:
                effective_trigger = self._resolve_and_trigger(rule, rule_key, sources)
                if effective_trigger is None:
                    continue
            else:
                effective_trigger = trigger

            if not self._rule_matches_trigger(rule, effective_trigger):
                continue

            if not self._rule_gate_open(rule, now):
                continue

            await self._fire_motion_rule(rule, rule_key, source, zone_or_device_id, effective_trigger)

    async def handle_device_state(self, source: str, device_name: str, value: str) -> None:
        state_key = (source, device_name)
        old_value = self._device_state_last_value.get(state_key)
        self._device_state_last_value[state_key] = value
        if old_value == value:
            return

        await self.flow_engine.handle_device_state(source, device_name, value)

        now = datetime.now()
        for idx, rule in enumerate(self.config.motion_rules):
            if not rule.get("enabled", True):
                continue

            sources = self._rule_sources(rule)
            matching = [s for s in sources if s.get("source") == source and s.get("source_device") == device_name]
            if not matching:
                continue

            rule_key = f"motion_rule_{idx}"
            if rule.get("source_combinator") == "and" and len(sources) > 1:
                trigger = self._resolve_and_trigger(rule, rule_key, sources)
                if trigger is None:
                    continue
            else:
                trigger_value = matching[0].get("trigger_value")
                if value == trigger_value:
                    trigger = "violated"
                elif self._is_until_restored(rule) and old_value == trigger_value:
                    trigger = "restored"
                else:
                    continue

            if not self._rule_gate_open(rule, now):
                continue

            await self._fire_motion_rule(rule, rule_key, source, device_name, trigger)

    @staticmethod
    def _is_until_restored(rule: dict) -> bool:
        return (
            "scene" not in rule
            and rule.get("action") != "turn_off"
            and rule.get("duration_seconds") in (None, 0)
        )

    @staticmethod
    def _rule_matches_trigger(rule: dict, trigger: str) -> bool:
        if RulesEngine._is_until_restored(rule):
            return True
        return rule.get("trigger_on", "violated") == trigger

    async def _fire_motion_rule(
        self, rule: dict, rule_key: str, source: str, zone_or_device_id,
        trigger: str = "violated", _chain_depth: int = 0,
    ) -> None:
        rule_name = rule.get("name")
        reason = f"reguła ruchu '{rule_name}'" if rule_name else None
        escalation_cfg = rule.get("escalation")
        if escalation_cfg:
            history = self._escalation_history.setdefault(rule_key, [])
            history.append(time.time())
            cutoff = time.time() - escalation_cfg["window_seconds"]
            history[:] = [t for t in history if t >= cutoff]
            if len(history) >= escalation_cfg["threshold_count"]:
                logger.warning(
                    "Eskalacja reguły ruchu '%s': %d naruszeń w %ds -> scena '%s'",
                    rule_key, len(history), escalation_cfg["window_seconds"], escalation_cfg["escalated_scene"],
                )
                self.history.write_event(
                    "escalation", source=f"{source}:{zone_or_device_id}",
                    detail={"count": len(history), "scene": escalation_cfg["escalated_scene"]},
                )
                await self.scenes.run(escalation_cfg["escalated_scene"])
                history.clear()
                await self._chain_trigger(rule, trigger, _chain_depth)
                return

        if "scene" in rule:
            await self.scenes.run(rule["scene"])
            self.history.write_event(
                "motion", source=f"{source}:{zone_or_device_id}", detail={"scene": rule["scene"]},
            )
            await self._chain_trigger(rule, trigger, _chain_depth)
            return

        targets = self._rule_targets(rule)

        for target in targets:
            device, _ = self._resolve_target(target, rule)
            if device in self._active_timers and not self._active_timers[device].done():
                self._active_timers[device].cancel()

        if rule.get("action") == "turn_off":
            for target in targets:
                device, target_system = self._resolve_target(target, rule)
                await self._set_target_state(target_system, device, False, reason=reason)
                logger.info(
                    "Reguła ruchu: %s(%s) -> wyłączenie %s (%s)",
                    source, zone_or_device_id, device, target_system,
                )
                self.history.write_event(
                    "motion", source=f"{source}:{zone_or_device_id}",
                    detail={"target_device": device, "target_system": target_system, "action": "turn_off"},
                )
            await self._chain_trigger(rule, trigger, _chain_depth)
            return

        if self._is_until_restored(rule):
            for target in targets:
                device, target_system = self._resolve_target(target, rule)
                dimmer = target.get("dimmer_level_percent", rule.get("dimmer_level_percent"))
                await self._set_target_state(target_system, device, trigger != "restored", dimmer, reason=reason)
                logger.info(
                    "Reguła ruchu: %s(%s) -> %s %s (%s, do ustania naruszenia)",
                    source, zone_or_device_id, "wyłączenie" if trigger == "restored" else "włączenie",
                    device, target_system,
                )
                self.history.write_event(
                    "motion", source=f"{source}:{zone_or_device_id}",
                    detail={
                        "target_device": device, "target_system": target_system,
                        "until_restored": True, "trigger": trigger,
                    },
                )
            await self._chain_trigger(rule, trigger, _chain_depth)
            return

        duration = rule.get("duration_seconds")
        if len(targets) == 1:
            # Ścieżka identyczna z tą sprzed wsparcia wielu akcji - bez dodatkowej
            # warstwy asyncio.gather, żeby nie zmieniać liczby "przełączeń" event loopa
            # (od tego zależy część istniejących testów asyncio.sleep(0)).
            device, target_system = self._resolve_target(targets[0], rule)
            dimmer = targets[0].get("dimmer_level_percent", rule.get("dimmer_level_percent"))
            self._active_timers[device] = asyncio.create_task(
                self._run_on_for_duration(target_system, device, duration, dimmer, rule_name)
            )
        else:
            # Wiele celów jednej reguły biegnie jako JEDNA grupa (asyncio.gather) zamiast
            # osobnych zadań na urządzenie - dzięki temu status "automation:<nazwa>" odpowiada
            # całej grupie (od startu pierwszego do końca ostatniego celu), a nie temu, który
            # cel akurat skończy jako pierwszy.
            group_task = asyncio.create_task(self._run_targets_for_duration(targets, rule, duration, rule_name))
            for target in targets:
                device, _ = self._resolve_target(target, rule)
                self._active_timers[device] = group_task
        logger.info(
            "Reguła ruchu: %s(%s) -> %d cel(e) na %ss",
            source, zone_or_device_id, len(targets), duration,
        )
        self.history.write_event(
            "motion", source=f"{source}:{zone_or_device_id}",
            detail={
                "targets": [self._resolve_target(t, rule)[0] for t in targets],
                "duration_seconds": duration,
            },
        )
        await self._chain_trigger(rule, trigger, _chain_depth)

    @staticmethod
    def _rule_targets(rule: dict) -> list[dict]:
        """Normalizuje cele akcji reguły do listy - reguły bez pola "actions"
        (stary, pojedynczy format) zwracają listę z jednym elementem zbudowanym
        z legacy pól "virtual_input"/"target_device"/"target_system"."""
        if rule.get("actions"):
            return rule["actions"]
        if "virtual_input" in rule:
            return [{"virtual_input": rule["virtual_input"]}]
        return [{"target_device": rule["target_device"], "target_system": rule.get("target_system", "tuya")}]

    @staticmethod
    def _resolve_target(target: dict, rule: dict) -> tuple[str, str]:
        if "virtual_input" in target:
            return target["virtual_input"], "satel_virtual_input"
        return target["target_device"], target.get("target_system", rule.get("target_system", "tuya"))

    async def _set_target_state(
        self, target_system: str, device: str, on: bool | None, dimmer: int | None = None,
        reason: str | None = None,
    ) -> None:
        """`on=None` oznacza "zmiana stanu" (toggle) - odwrócenie AKTUALNEGO stanu
        celu zamiast wymuszenia konkretnego. Dla `satel_output` i `satel_virtual_input`
        (tryb library) stan aktualny czytamy z `SatelIntegraClient.violated_outputs`
        (na żywo, bez zapytania) - dla Tuya przez `TuyaController.toggle`. `reason`
        trafia do logu Tuya (np. nazwa reguły ruchu/flow/sceny), żeby dało się
        ustalić, co wyzwoliło daną komendę.

        Po udanym turn_on/turn_off aktualizujemy status "tuya:<device>" w historii
        OD RAZU (zanim przyjdzie potwierdzenie z kolejnego pollingu/Pulsara) - bez
        tego węzeł "Warunek stanu" reguły/flow odpalonej wielokrotnie pod rząd (np.
        strefa ruchu zgłaszająca "naruszona" co kilka sekund, dopóki trwa ruch)
        czytałby wciąż nieaktualny, stary stan i wysyłał tę samą komendę do Tuya
        wielokrotnie zamiast raz."""
        if target_system == "satel_output":
            output_number = self.satel.monitored_outputs.get(device)
            if output_number is None:
                logger.warning(
                    "Reguła ruchu wskazuje na nieznane wyjście Satela '%s' (sprawdź zakładkę "
                    "⚙ Ustawienia -> Satel) - pomijam", device,
                )
                return
            if on is None:
                on = output_number not in self.satel.violated_outputs
            await self.satel.set_output(output_number, on)
        elif target_system == "satel_virtual_input":
            await self._trigger_virtual_input(device, on)
        elif on is None:
            await self.tuya.toggle(device, reason=reason)
        elif on:
            await self.tuya.turn_on(device, dimmer, reason=reason)
            self.history.set_status(f"tuya:{device}", "tuya", "włączone", label=device)
        else:
            await self.tuya.turn_off(device, reason=reason)
            self.history.set_status(f"tuya:{device}", "tuya", "wyłączone", label=device)

    async def _set_tuya_dp(self, device: str, dp_code: str, value: object, reason: str | None = None) -> None:
        """Wrapper wołany przez FlowEngine (węzeł "Ustaw pole Tuya") - jak
        _set_target_state, dereferencjonuje self.tuya na bieżąco (a nie w
        momencie budowy FlowEngine), żeby przetrwać podmianę self.tuya przy
        reload_config()."""
        await self.tuya.set_dp(device, dp_code, value, reason=reason)

    async def _run_on_for_duration(
        self, target_system: str, device: str, duration: float, dimmer: int | None, rule_name: str | None,
        reason: str | None = None,
    ) -> None:
        if reason is None:
            reason = f"reguła ruchu '{rule_name}'" if rule_name else None
        if rule_name:
            self.history.set_status(f"automation:{rule_name}", "automation", "uruchomiona", label=rule_name)
        try:
            if target_system in ("satel_output", "satel_virtual_input"):
                await self._set_target_state(target_system, device, True, reason=reason)
                await asyncio.sleep(duration)
                await self._set_target_state(target_system, device, False, reason=reason)
            else:
                await self.tuya.turn_on_for_duration(device, duration, dimmer, reason=reason)
        finally:
            if rule_name:
                self.history.set_status(f"automation:{rule_name}", "automation", "bezczynna", label=rule_name)

    async def _run_targets_for_duration(
        self, targets: list[dict], rule: dict, duration: float, rule_name: str | None,
    ) -> None:
        reason = f"reguła ruchu '{rule_name}'" if rule_name else None
        if rule_name:
            self.history.set_status(f"automation:{rule_name}", "automation", "uruchomiona", label=rule_name)
        try:
            async def _run_one(target: dict):
                device, target_system = self._resolve_target(target, rule)
                dimmer = target.get("dimmer_level_percent", rule.get("dimmer_level_percent"))
                await self._run_on_for_duration(target_system, device, duration, dimmer, None, reason=reason)

            await asyncio.gather(*(_run_one(target) for target in targets))
        finally:
            if rule_name:
                self.history.set_status(f"automation:{rule_name}", "automation", "bezczynna", label=rule_name)

    async def _chain_trigger(self, rule: dict, trigger: str, _chain_depth: int) -> None:
        chain_name = (rule.get("then_trigger_rule") or "").strip()
        if not chain_name:
            return
        if _chain_depth >= 5:
            logger.warning(
                "Łańcuch reguł ruchu przerwany (then_trigger_rule) - zbyt duża głębokość "
                "(możliwa pętla) przed wyzwoleniem '%s'", chain_name,
            )
            return

        idx = self._rule_name_index.get(chain_name)
        if idx is None or idx >= len(self.config.motion_rules):
            logger.warning(
                "Reguła '%s' ma then_trigger_rule wskazujące na nieznaną/usuniętą regułę '%s'",
                rule.get("name", "?"), chain_name,
            )
            return

        target = self.config.motion_rules[idx]
        if not target.get("enabled", True):
            logger.debug(
                "Łańcuchowe wyzwolenie '%s' pominięte - reguła docelowa wyłączona w edytorze", chain_name,
            )
            return
        now = datetime.now()
        if not self._rule_gate_open(target, now):
            logger.debug(
                "Łańcuchowe wyzwolenie '%s' pominięte - okno czasowe/warunek docelowej reguły niespełniony",
                chain_name,
            )
            return

        logger.info(
            "Reguła ruchu '%s' wyzwala kolejną regułę '%s' (then_trigger_rule)",
            rule.get("name", "?"), chain_name,
        )
        await self._fire_motion_rule(
            target, f"motion_rule_{idx}", "chain", rule.get("name", "?"), trigger, _chain_depth + 1,
        )

    async def _trigger_virtual_input(self, name: str, state: bool | None = True) -> None:
        if self._vi_mode == "http":
            if state is False:
                logger.info(
                    "Wirtualne wejście IP '%s': akcja WYŁĄCZ pominięta - tryb HTTP (ETHM-1 Plus) "
                    "obsługuje TYLKO wyzwolenie (impuls naruszenia) wejścia, nie ma odpowiednika "
                    "'wyłącz' na poziomie protokołu. Wcześniej WYŁĄCZ błędnie wysyłało dokładnie "
                    "ten sam impuls co WŁĄCZ, co powodowało samopodtrzymujące się przełączanie "
                    "(flapowanie) w regułach/flow typu włącz->opóźnienie->wyłącz.",
                    name,
                )
                return
            # state is True lub None (zmiana stanu) - w trybie http oba oznaczają identyczny
            # impuls, bo protokół nie zna pojęcia poziomu/stanu, tylko moment naruszenia.
            await self.satel_virtual_inputs.trigger(name)
            return
        output_number = self.satel.virtual_inputs.get(name)
        if output_number is None:
            logger.warning(
                "Nieznane wirtualne wejście IP Satela '%s' (sprawdź zakładkę "
                "⚙ Ustawienia -> Satel) - pomijam", name,
            )
            return
        if state is None:
            state = output_number not in self.satel.violated_outputs
        await self.satel.set_output(output_number, state)

    async def handle_light_state_change(self, device_name: str, is_on: bool) -> None:
        if not is_on:
            return
        for rule in self.config.light_to_satel_rules:
            if rule["source_device"] == device_name:
                await self._trigger_virtual_input(rule["virtual_input"])
                self.history.write_event(
                    "light_to_satel", source=f"tuya:{device_name}",
                    detail={"virtual_input": rule["virtual_input"]},
                )

    async def _on_homewhiz_state(self, appliance_name: str, state_name: str) -> None:
        self.history.set_status(f"homewhiz:{appliance_name}", "homewhiz", state_name, label=appliance_name)
        await self.handle_device_state("homewhiz_state", appliance_name, state_name)
        if self.ha_mqtt:
            self.ha_mqtt.publish_state(f"{self._ha_topic_prefix}/homewhiz/{appliance_name}/state", state_name)
        if self.state_mqtt:
            self.state_mqtt.publish_state(f"{self._homewhiz_mqtt_topic}/{appliance_name}/state", state_name)

    async def handle_camera_motion(self, source: str, camera_id: str) -> None:
        now = datetime.now()
        for idx, rule in enumerate(self.config.camera_alarm_rules):
            if rule["source"] != source or rule["camera_id"] != camera_id:
                continue

            if not is_within_window(rule.get("time_window"), self._location_cfg, now):
                continue

            rule_key = f"camera_rule_{idx}"
            cooldown = rule.get("cooldown_seconds", 0)
            if cooldown:
                last = self._camera_rule_last_triggered.get(rule_key, 0)
                if time.time() - last < cooldown:
                    logger.debug(
                        "Reguła kamery '%s' pominięta - w okresie karencji (%ds)", rule_key, cooldown,
                    )
                    continue
                self._camera_rule_last_triggered[rule_key] = time.time()

            if "scene" in rule:
                await self.scenes.run(rule["scene"])
                self.history.write_event(
                    "camera_alarm", source=f"{source}:{camera_id}", detail={"scene": rule["scene"]},
                )
                continue

            action = rule["action"]
            if action == "virtual_input":
                await self._trigger_virtual_input(rule["virtual_input"])
            elif action == "output":
                await self.satel.set_output(rule["output_number"], True)
            elif action == "arm":
                await self.satel.arm(rule.get("arm_mode", 0), rule.get("partitions"))
            logger.info("Reguła kamery: %s(%s) -> %s", source, camera_id, action)
            self.history.write_event(
                "camera_alarm", source=f"{source}:{camera_id}", detail={"action": action},
            )

    def handle_boiler_value(self, sensor_cfg: dict, value) -> None:
        name = sensor_cfg["name"]
        label = sensor_cfg.get("label", name)
        targets = sensor_cfg.get("targets", {})
        is_numeric = isinstance(value, (int, float))

        if targets.get("history", True):
            if is_numeric:
                self.history.write_reading(name, value)
            self.history.set_status(f"boiler:{name}", "boiler", value, label=label)

        self.alarms.on_value(name, value)

        asyncio.run_coroutine_threadsafe(self.boiler_automation.on_value(name, value), self._loop)

        if targets.get("mqtt_publish") and "mqtt" in self.boiler_publishers:
            self.boiler_publishers["mqtt"].publish(name, value)

        if targets.get("tuya_virtual_device") and "tuya" in self.boiler_publishers:
            dp_code = targets.get("tuya_dp")
            if not dp_code:
                logger.warning(
                    "Czujnik '%s' ma tuya_virtual_device=true, ale brak targets.tuya_dp w config.yaml - pomijam", name,
                )
            elif not is_numeric:
                logger.warning(
                    "Czujnik '%s' ma wartość tekstową ('%s') - wirtualne urządzenia Tuya (DP typu 'value') "
                    "oczekują zwykle liczby; sprawdź typ DP na platformie Tuya, jeśli chcesz wysyłać tekst/status.",
                    name, value,
                )
                self.boiler_publishers["tuya"].publish_to_dp(dp_code, value)
            else:
                self.boiler_publishers["tuya"].publish_to_dp(dp_code, value)

    async def _stale_check_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.alarms.check_stale_sensors()
            except Exception:
                logger.exception("Błąd w pętli sprawdzania nieaktywnych czujników")

    async def _maintenance_loop(self) -> None:
        maint_cfg = self.config.raw.get("maintenance", {})
        if not maint_cfg.get("run_in_process", True):
            logger.info("Automatyczna rotacja/backup wyłączona w procesie (maintenance.run_in_process: false)")
            return

        interval_seconds = maint_cfg.get("interval_hours", 24) * 3600
        await asyncio.sleep(60)
        loop = asyncio.get_event_loop()
        while True:
            try:
                summary = await loop.run_in_executor(None, run_maintenance, self.config)
                logger.info("Automatyczna rotacja/backup bazy zakończona: %s", summary)
                self.history.write_event("system", detail={"message": "Rotacja/backup bazy zakończone", **summary})
            except Exception:
                logger.exception("Błąd podczas automatycznej rotacji/backupu bazy")
            await asyncio.sleep(interval_seconds)

    async def _command_poll_loop(self, interval_seconds: float = 1.5) -> None:
        while True:
            try:
                for cmd in self.history.get_pending_commands():
                    await self._execute_command(cmd)
            except Exception:
                logger.exception("Błąd w pętli wykonywania komend z panelu")
            await asyncio.sleep(interval_seconds)

    async def _execute_command(self, cmd: dict) -> None:
        try:
            if cmd["command"] == "satel_arm":
                partitions = cmd["params"]["partitions"]
                mode = cmd["params"].get("mode", 0)
                await self.satel.arm(mode=mode, partitions=partitions)
                self.history.mark_command_done(cmd["id"], {"message": f"Uzbrojono strefy {partitions}"})
            elif cmd["command"] == "satel_disarm":
                partitions = cmd["params"]["partitions"]
                await self.satel.disarm(partitions=partitions)
                self.history.mark_command_done(cmd["id"], {"message": f"Rozbrojono strefy {partitions}"})
            elif cmd["command"] == "satel_virtual_input":
                name = cmd["params"]["name"]
                await self._trigger_virtual_input(name)
                self.history.mark_command_done(cmd["id"], {"message": f"Aktywowano wirtualne wejście '{name}'"})
            elif cmd["command"] == "tuya_on":
                name = cmd["params"]["name"]
                await self.tuya.turn_on(name, reason="panel (ręcznie)")
                self.history.mark_command_done(cmd["id"], {"message": f"Włączono '{name}'"})
                self._schedule_tuya_status_refresh(name)
            elif cmd["command"] == "tuya_off":
                name = cmd["params"]["name"]
                await self.tuya.turn_off(name, reason="panel (ręcznie)")
                self.history.mark_command_done(cmd["id"], {"message": f"Wyłączono '{name}'"})
                self._schedule_tuya_status_refresh(name)
            elif cmd["command"] == "run_scene":
                name = cmd["params"]["name"]
                if not self.scenes.exists(name):
                    self.history.mark_command_error(cmd["id"], f"Nieznana scena: '{name}'")
                else:
                    await self.scenes.run(name)
                    self.history.mark_command_done(cmd["id"], {"message": f"Uruchomiono scenę '{name}'"})
            elif cmd["command"] == "run_flow":
                flow_id = cmd["params"]["flow_id"]
                try:
                    await self.flow_engine.run_flow(flow_id)
                    self.history.mark_command_done(cmd["id"], {"message": f"Uruchomiono flow '{flow_id}'"})
                except ValueError as exc:
                    self.history.mark_command_error(cmd["id"], str(exc))
            elif cmd["command"] == "mqtt_publish":
                topic = cmd["params"]["topic"]
                payload = cmd["params"].get("payload", "")
                retain = cmd["params"].get("retain", False)
                publisher = self.state_mqtt or self.ha_mqtt or self.boiler_publishers.get("mqtt")
                if publisher is None:
                    self.history.mark_command_error(
                        cmd["id"],
                        "Brak aktywnego połączenia MQTT (włącz 'Publikacja stanu do MQTT', "
                        "Home Assistant, albo powiąż czujnik kotła z MQTT)",
                    )
                else:
                    publisher.publish_raw(topic, payload, retain=retain)
                    self.history.mark_command_done(cmd["id"], {"message": f"Opublikowano na '{topic}'"})
            elif cmd["command"] == "reload_config":
                result = await self.reload_config()
                self.history.mark_command_done(cmd["id"], result)
            elif cmd["command"] == "restart_process":
                self.history.mark_command_done(cmd["id"], {"message": "Restartuję proces bridge'a"})
                self.history.write_event("system", detail={"message": "Restart procesu bridge'a zażądany z panelu"})
                asyncio.get_event_loop().call_later(0.5, os._exit, 0)
            else:
                self.history.mark_command_error(cmd["id"], f"Nieznana komenda: {cmd['command']}")
        except Exception as exc:
            logger.exception("Błąd wykonania komendy #%s (%s)", cmd["id"], cmd["command"])
            self.history.mark_command_error(cmd["id"], str(exc))

    async def reload_config(self) -> dict:
        new_config = BridgeConfig.load(self.config.db_path)
        self.config = new_config
        apply_log_levels(new_config.raw)
        self._rule_name_index = self._build_rule_name_index()
        self._location_cfg = new_config.raw.get("location")

        self.tuya = _build_tuya_controller(new_config.tuya)

        self.satel.monitored_zones = new_config.satel.get("monitored_zones", {})
        self.satel.monitored_outputs = new_config.satel.get("monitored_outputs", {})
        self.satel.virtual_inputs = new_config.satel.get("virtual_inputs", {}).get("inputs", {})
        self._vi_mode = new_config.satel.get("virtual_inputs", {}).get("mode", "library")
        self.satel_virtual_inputs = SatelVirtualInputTrigger(
            new_config.satel["host"], new_config.satel.get("virtual_inputs", {}),
        )
        self.satel.status_poll_interval_seconds = new_config.satel.get("status_poll_interval_seconds", 3)

        self.scenes = SceneRunner(
            new_config.raw.get("scenes", []), self.tuya, self.satel,
            satel_virtual_inputs=self.satel_virtual_inputs, virtual_input_mode=self._vi_mode,
            history=self.history,
        )
        self.flow_engine.update_flows(new_config.raw.get("flows", []))
        self.flow_engine.location_cfg = self._location_cfg
        self.boiler_automation = BoilerAutomation(new_config.raw.get("boiler_rules", []), self.scenes)
        self.alarms = AlarmEngine(new_config.raw.get("alarm_rules", []), self.history, notify_cfg=self._notify_cfg)

        self.presence_simulation.cfg = new_config.raw.get("presence_simulation", {})
        self.presence_simulation.location_cfg = self._location_cfg

        # Akcesoria HAP-python (urządzenia/strefy/sceny wystawione do Home.app) są
        # budowane raz przy tworzeniu HomeKitBridge - jedyny sposób, żeby dodanie
        # albo usunięcie urządzenia Tuya faktycznie zniknęło/pojawiło się w
        # Home.app bez pełnego restartu procesu, to zbudowanie nowej instancji
        # (z już przeładowanym self.tuya/self.scenes) i podmiana starej.
        old_homekit = self.homekit
        if old_homekit is not None:
            await old_homekit.async_stop()
        self.homekit = self._build_homekit(new_config)
        if self.homekit is not None:
            await self.homekit.async_start()

        if self.ebus_listener is not None:
            self.ebus_listener.update_sensors(new_config.ebus)

        sensors = new_config.ebus.get("sensors", [])
        if any(s.get("targets", {}).get("mqtt_publish") for s in sensors) and "mqtt" not in self.boiler_publishers:
            self.boiler_publishers["mqtt"] = MqttBoilerPublisher(
                new_config.boiler_output["mqtt"], history=self.history, notify_cfg=self._notify_cfg,
            )
        if any(s.get("targets", {}).get("tuya_virtual_device") for s in sensors) and "tuya" not in self.boiler_publishers:
            try:
                self.boiler_publishers["tuya"] = TuyaVirtualDevicePublisher(
                    new_config.tuya["cloud"], new_config.boiler_output["tuya_virtual_device"],
                )
            except Exception:
                logger.exception(
                    "Nie udało się zainicjować wirtualnego urządzenia Tuya przy przeładowaniu "
                    "konfiguracji - ten kanał zostanie wyłączony, reszta reloadu się powiodła."
                )

        self._seed_satel_status_labels()
        self._seed_tuya_status_labels()
        self._seed_homewhiz_status_labels()
        self._seed_automation_status_labels()

        logger.info("Konfiguracja przeładowana bez restartu procesu")
        self.history.write_event("system", detail={"message": "Konfiguracja przeładowana (bez restartu procesu)"})
        return {
            "message": "Konfiguracja przeładowana bez restartu procesu (czujniki eBUS, pola Satela, "
                       "urządzenia Tuya, automatyzacje, alarmy progowe). Zmiany połączeń (host/port/"
                       "klucze/brokery MQTT, zakładka 🔌 Połączenia) wciąż wymagają pełnego restartu.",
        }

    async def _apply_tuya_device_status(self, device_name: str, dps: dict | None) -> None:
        if dps is None:
            is_local = device_name in self.config.tuya.get("devices", {})
            tuya_value = "poza siecią" if is_local else "niedostępne"
            self.history.set_status(f"tuya:{device_name}", "tuya", tuya_value, label=device_name)
            await self.handle_device_state("tuya_state", device_name, tuya_value)
            return
        local_cfg = self.config.tuya.get("devices", {}).get(device_name)
        cloud_cfg = self.config.tuya.get("cloud_devices", {}).get(device_name)
        schema = (local_cfg or {}).get("dp_schema") or {}
        preferred_switch_key = (cloud_cfg or {}).get("switch_code")
        is_on, extra_status = format_tuya_dps(dps, schema=schema, preferred_switch_key=preferred_switch_key)
        if is_on:
            tuya_value = "włączone"
        elif is_on is not None:
            tuya_value = "wyłączone"
        else:
            tuya_value = extra_status or "?"
        self.history.set_status(f"tuya:{device_name}", "tuya", tuya_value, label=device_name)
        await self.handle_device_state("tuya_state", device_name, tuya_value)
        if self.ha_mqtt and is_on is not None:
            self.ha_mqtt.publish_state(
                f"{self._ha_topic_prefix}/tuya/{device_name}/state",
                "ON" if is_on else "OFF",
            )
        if self.state_mqtt and is_on is not None:
            self.state_mqtt.publish_state(
                f"{self._tuya_mqtt_topic}/{device_name}/state",
                "ON" if is_on else "OFF",
            )
        percent = None
        if local_cfg and local_cfg.get("type") == "dimmer" and local_cfg.get("dimmer_dp") in dps:
            lo, hi = local_cfg.get("dimmer_range", [10, 1000])
            raw = dps[local_cfg["dimmer_dp"]]
            percent = round((raw - lo) / (hi - lo) * 100) if hi > lo else 0
            self.history.set_status(
                f"tuya:{device_name}:brightness", "tuya", f"{percent}%",
                label=f"{device_name} (jasność)",
            )
        if self.homekit and is_on is not None:
            self.homekit.update_tuya_state(device_name, is_on, percent)
        sensor_readings = extract_tuya_sensor_readings(dps, schema)
        if "soil_humidity_percent" in sensor_readings:
            self.history.set_status(
                f"tuya:{device_name}:soil_humidity", "tuya", f"{sensor_readings['soil_humidity_percent']}%",
                label=f"{device_name} (wilgotność gleby)",
            )
        if "humidity_percent" in sensor_readings:
            self.history.set_status(
                f"tuya:{device_name}:humidity", "tuya", f"{sensor_readings['humidity_percent']}%",
                label=f"{device_name} (wilgotność)",
            )
        if "temperature_celsius" in sensor_readings:
            self.history.set_status(
                f"tuya:{device_name}:temperature", "tuya", f"{sensor_readings['temperature_celsius']}°C",
                label=f"{device_name} (temperatura)",
            )
        if "contact_open" in sensor_readings:
            self.history.set_status(
                f"tuya:{device_name}:contact", "tuya", "tak" if sensor_readings["contact_open"] else "nie",
                label=f"{device_name} (kontaktron)",
            )
        if "leak_detected" in sensor_readings:
            self.history.set_status(
                f"tuya:{device_name}:leak", "tuya", "tak" if sensor_readings["leak_detected"] else "nie",
                label=f"{device_name} (czujnik zalania)",
            )
        if "battery_percent" in sensor_readings:
            self.history.set_status(
                f"tuya:{device_name}:battery", "tuya", f"{sensor_readings['battery_percent']}%",
                label=f"{device_name} (bateria)",
            )
        if self.homekit and sensor_readings:
            self.homekit.update_tuya_sensor_state(device_name, sensor_readings)

    def _schedule_tuya_status_refresh(self, device_name: str, delay_seconds: float = 1.5) -> None:
        async def _refresh() -> None:
            await asyncio.sleep(delay_seconds)
            try:
                dps = await self.tuya.get_status(device_name)
            except Exception:
                logger.exception("Nie udało się odświeżyć statusu Tuya '%s' po komendzie", device_name)
                return
            await self._apply_tuya_device_status(device_name, dps)

        asyncio.ensure_future(_refresh())

    async def _tuya_status_poll_loop(self) -> None:
        self._tuya_health = HealthReporter(
            "tuya_local", history=self.history, notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
        )
        while True:
            try:
                statuses = await self.tuya.get_all_statuses()
                unreachable = [name for name, dps in statuses.items() if dps is None]
                all_unreachable = statuses and len(unreachable) == len(statuses)
                self._tuya_health.report(
                    not all_unreachable,
                    f"niedostępne: {', '.join(unreachable)}" if unreachable else "",
                )
                for device_name, dps in statuses.items():
                    await self._apply_tuya_device_status(device_name, dps)
            except Exception:
                logger.exception("Błąd w pętli odpytywania statusów Tuya")
            await asyncio.sleep(self.config.tuya.get("status_poll_interval_seconds", 15))

    async def _weather_poll_loop(self, interval_seconds: float = 3 * 3600) -> None:
        """Cyklicznie pobiera prognozę opadów dla lokalizacji ustawionej w
        sekcji "🌍 Lokalizacja" (ta sama, co dla wschodu/zachodu słońca) i
        publikuje ją jako statusy widoczne na panelu głównym oraz do użycia
        w Warunku stanu (Flow) / dodatkowym warunku reguły ruchu - np.
        "weather:precipitation_probability_percent >= 60" jako warunek
        blokujący podlewanie. Bez skonfigurowanej lokalizacji pętla w ogóle
        się nie uruchamia (patrz start())."""
        while True:
            try:
                forecast = await fetch_forecast(
                    self._location_cfg["latitude"], self._location_cfg["longitude"],
                )
                if forecast is not None:
                    self.history.set_status(
                        "weather:precipitation_probability_percent", "weather",
                        str(forecast["precipitation_probability_percent"]), label="Prawdopodobieństwo opadów (%)",
                    )
                    self.history.set_status(
                        "weather:precipitation_sum_mm", "weather",
                        str(forecast["precipitation_sum_mm"]), label="Suma opadów dziś (mm)",
                    )
            except Exception:
                logger.exception("Błąd w pętli odpytywania prognozy pogody")
            await asyncio.sleep(interval_seconds)

    def _seed_satel_status_labels(self) -> None:
        satel_enabled = self.config.satel.get("enabled", True)
        seen = set() if not satel_enabled else (
            {s["key"] for s in self.history.get_status("satel_input")}
            | {s["key"] for s in self.history.get_status("satel_output")}
            | {s["key"] for s in self.history.get_status("satel_partition")}
        )
        input_keys = {f"satel_input:{number}" for number in self.satel.monitored_zones.values()}
        output_keys = {f"satel_output:{number}" for number in self.satel.monitored_outputs.values()}
        partition_keys = {
            f"satel_partition:{number}" for number in self.config.satel.get("partitions", {}).values()
        }
        unavailable = "brak danych (wtyczka wyłączona)"
        for name, number in self.satel.monitored_zones.items():
            key = f"satel_input:{number}"
            if key not in seen:
                self.history.set_status(key, "satel_input", unavailable if not satel_enabled else "OK", label=name)
        for name, number in self.satel.monitored_outputs.items():
            key = f"satel_output:{number}"
            if key not in seen:
                self.history.set_status(
                    key, "satel_output", unavailable if not satel_enabled else "wyłączone", label=name,
                )
        for name, number in self.config.satel.get("partitions", {}).items():
            key = f"satel_partition:{number}"
            if key not in seen:
                self.history.set_status(
                    key, "satel_partition", unavailable if not satel_enabled else "nieznany", label=name,
                )

        self.history.prune_status("satel_input", input_keys)
        self.history.prune_status("satel_output", output_keys)
        self.history.prune_status("satel_partition", partition_keys)

    def _seed_tuya_status_labels(self) -> None:
        seen = {s["key"] for s in self.history.get_status("tuya")}
        for name in self.tuya.devices_cfg:
            key = f"tuya:{name}"
            if key not in seen:
                self.history.set_status(key, "tuya", "?", label=name)

        tuya_status_suffixes = (
            "brightness", "soil_humidity", "contact", "leak", "battery", "humidity", "temperature",
        )
        tuya_keys = {f"tuya:{name}" for name in self.tuya.devices_cfg}
        for suffix in tuya_status_suffixes:
            tuya_keys |= {f"tuya:{name}:{suffix}" for name in self.tuya.devices_cfg}
        self.history.prune_status("tuya", tuya_keys)

    def _seed_homewhiz_status_labels(self) -> None:
        homewhiz_cfg = self.config.homewhiz or {}
        appliance_names = set((homewhiz_cfg.get("appliances") or {}).keys())
        seen = {s["key"] for s in self.history.get_status("homewhiz")}
        for name in appliance_names:
            key = f"homewhiz:{name}"
            if key not in seen:
                self.history.set_status(key, "homewhiz", "nieznany", label=name)

        homewhiz_keys = {f"homewhiz:{name}" for name in appliance_names}
        self.history.prune_status("homewhiz", homewhiz_keys)

    def _seed_automation_status_labels(self) -> None:
        seen = {s["key"] for s in self.history.get_status("automation")}
        valid_keys = {f"automation:{scene_name}" for scene_name in self.scenes.scenes}
        for scene_name in self.scenes.scenes:
            key = f"automation:{scene_name}"
            if key not in seen:
                self.history.set_status(key, "automation", "bezczynna", label=scene_name)
        for rule in self.config.motion_rules:
            name = rule.get("name")
            if name and "scene" not in rule and rule.get("action") != "turn_off":
                key = f"automation:{name}"
                valid_keys.add(key)
                if key not in seen:
                    self.history.set_status(key, "automation", "bezczynna", label=name)

        self.history.prune_status("automation", valid_keys)

    async def _on_alarm_status_change(self) -> None:
        if self._alarm_status_debounce_task is not None and not self._alarm_status_debounce_task.done():
            self._alarm_status_debounce_task.cancel()
        self._alarm_status_debounce_task = asyncio.create_task(self._debounced_alarm_status_change())

    async def _debounced_alarm_status_change(self) -> None:
        try:
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            return
        await self._handle_alarm_status_change()

    async def _handle_alarm_status_change(self) -> None:
        armed_numbers = self.satel.armed_partition_numbers
        triggered_numbers = self.satel.triggered_partition_numbers
        partition_name_by_number = {v: k for k, v in self.config.satel.get("partitions", {}).items()}
        for partition_number in self.config.satel.get("partitions", {}).values():
            armed = partition_number in armed_numbers
            self.history.set_status(
                f"satel_partition:{partition_number}", "satel_partition",
                "uzbrojona" if armed else "rozbrojona",
                label=partition_name_by_number.get(partition_number, f"strefa {partition_number}"),
            )
            if self.ha_mqtt:
                self.ha_mqtt.publish_state(
                    f"{self._ha_topic_prefix}/satel/partition/{partition_number}/state",
                    "ON" if armed else "OFF",
                )
            if self.state_mqtt:
                self.state_mqtt.publish_state(
                    f"{self._satel_mqtt_topic}/partition/{partition_number}/state",
                    "ON" if armed else "OFF",
                )
            if self.homekit:
                self.homekit.update_partition_state(
                    partition_number, armed, triggered=partition_number in triggered_numbers,
                )

    async def start(self) -> None:
        self.history.write_event("system", detail={"message": "Bridge uruchomiony"})
        asyncio.create_task(self._stale_check_loop())
        self.flow_engine.start()

        self._seed_satel_status_labels()
        self._seed_tuya_status_labels()
        self._seed_homewhiz_status_labels()
        self._seed_automation_status_labels()

        async def _on_zone_change(status: dict) -> None:
            zone_name_by_number = {v: k for k, v in self.satel.monitored_zones.items()}
            for zone_number, violated in status.items():
                self.history.set_status(
                    f"satel_input:{zone_number}", "satel_input",
                    "naruszone" if violated else "OK",
                    label=zone_name_by_number.get(zone_number, f"wejście {zone_number}"),
                )
                if self.ha_mqtt:
                    self.ha_mqtt.publish_state(
                        f"{self._ha_topic_prefix}/satel/input/{zone_number}/state",
                        "ON" if violated else "OFF",
                    )
                if self.state_mqtt:
                    self.state_mqtt.publish_state(
                        f"{self._satel_mqtt_topic}/input/{zone_number}/state",
                        "ON" if violated else "OFF",
                    )
                await self.handle_motion(
                    "satel_zone", zone_number,
                    trigger="violated" if violated else "restored",
                )

        async def _on_output_change(status: dict) -> None:
            output_name_by_number = {v: k for k, v in self.satel.monitored_outputs.items()}
            for output_number, active in status.items():
                self.history.set_status(
                    f"satel_output:{output_number}", "satel_output",
                    "włączone" if active else "wyłączone",
                    label=output_name_by_number.get(output_number, f"wyjście {output_number}"),
                )
                if self.ha_mqtt:
                    self.ha_mqtt.publish_state(
                        f"{self._ha_topic_prefix}/satel/output/{output_number}/state",
                        "ON" if active else "OFF",
                    )
                if self.state_mqtt:
                    self.state_mqtt.publish_state(
                        f"{self._satel_mqtt_topic}/output/{output_number}/state",
                        "ON" if active else "OFF",
                    )

        self.satel.register_zone_callback(_on_zone_change)
        self.satel.register_output_callback(_on_output_change)
        self.satel.register_alarm_status_callback(self._on_alarm_status_change)

        if self.config.satel.get("enabled", True):
            asyncio.create_task(self.satel.run_forever())
        else:
            logger.info("Satel wyłączony w ustawieniach (🧩 Wtyczki) - pomijam nawiązanie połączenia")
            self.history.set_status("health:satel", "system_health", "wyłączono (wtyczka)", label="satel")
        if self.config.tuya.get("enabled", True):
            asyncio.create_task(self._tuya_status_poll_loop())
        else:
            logger.info("Tuya lokalnie wyłączone w ustawieniach (🧩 Wtyczki) - pomijam odpytywanie")
            self.history.set_status("health:tuya_local", "system_health", "wyłączono (wtyczka)", label="tuya_local")
        asyncio.create_task(self.presence_simulation.run_forever())
        asyncio.create_task(self._command_poll_loop())
        asyncio.create_task(self._maintenance_loop())
        if self._location_cfg:
            asyncio.create_task(self._weather_poll_loop())

        if self.config.ebus.get("enabled", True):
            self.ebus_listener = EbusMqttListener(
                self.config.ebus, self.handle_boiler_value, history=self.history,
                notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
            )
            self.ebus_listener.start()
        else:
            logger.info("eBUS wyłączony w ustawieniach (🧩 Wtyczki) - pomijam połączenie MQTT")
            self.history.set_status("health:ebus_mqtt", "system_health", "wyłączono (wtyczka)", label="ebus_mqtt")

        if "cloud" in self.config.tuya:
            camera_listener = TuyaCameraMotionListener(
                self.config.tuya["cloud"],
                lambda cam_id: self.handle_camera_motion("tuya_cloud", cam_id),
                history=self.history,
                notify_cfg=self._notify_cfg,
                grace_seconds=self._grace_seconds,
            )
            asyncio.create_task(camera_listener.run_forever())

        if self.config.unifi_protect and self.config.unifi_protect.get("enabled", True):
            unifi_listener = UnifiProtectMotionListener(
                self.config.unifi_protect,
                lambda cam_id: self.handle_camera_motion("unifi_protect", cam_id),
                history=self.history,
                notify_cfg=self._notify_cfg,
                grace_seconds=self._grace_seconds,
            )
            asyncio.create_task(unifi_listener.run_forever())

        if self.config.homewhiz and self.config.homewhiz.get("enabled", True):
            homewhiz_client = HomeWhizClient(
                self.config.homewhiz, self._on_homewhiz_state,
                history=self.history, notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
            )
            asyncio.create_task(homewhiz_client.run_forever())

        if self.ha_mqtt:
            def _make_tuya_handler(name):
                async def _handler(payload: str) -> None:
                    if payload.upper() == "ON":
                        await self.tuya.turn_on(name, reason="Home Assistant (MQTT)")
                    else:
                        await self.tuya.turn_off(name, reason="Home Assistant (MQTT)")
                    self.ha_mqtt.publish_state(f"{self._ha_topic_prefix}/tuya/{name}/state", payload.upper())
                return _handler

            for device_name in self.config.tuya["devices"]:
                topic = f"{self._ha_topic_prefix}/tuya/{device_name}/set"
                self.ha_mqtt.register_command_handler(topic, _make_tuya_handler(device_name))

            def _make_output_handler(number):
                async def _handler(payload: str) -> None:
                    await self.satel.set_output(number, payload.upper() == "ON")
                return _handler

            for output_name, output_number in self.satel.monitored_outputs.items():
                output_topic = f"{self._ha_topic_prefix}/satel/output/{output_number}/set"
                self.ha_mqtt.register_command_handler(output_topic, _make_output_handler(output_number))

            self.ha_mqtt.start()

        if self.state_mqtt:
            self.state_mqtt.start()

        if self.homekit:
            await self.homekit.async_start()

        logger.info("Silnik reguł uruchomiony i gotowy")
