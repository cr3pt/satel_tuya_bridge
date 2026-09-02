from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

from .config import BridgeConfig
from .logging_config import apply_log_levels
from .satel_client import SatelIntegraClient, SatelVirtualInputTrigger
from .tuya_local import TuyaLocalController
from .tuya_cloud import TuyaCloudController
from .tuya_dispatch import TuyaController
from .tuya_cloud_events import TuyaCameraMotionListener
from .unifi_protect_events import UnifiProtectMotionListener
from .homewhiz_client import HomeWhizClient
from .ebus_client import EbusMqttListener
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
        if mqtt_publish_cfg.get("enabled", True) and config.ebus.get("mqtt_host"):
            self.state_mqtt = HomeAssistantMqttBridge(
                {"mqtt_host": config.ebus["mqtt_host"], "mqtt_port": config.ebus.get("mqtt_port", 1883)},
                entities=[], history=self.history, notify_cfg=notify_cfg, grace_seconds=grace_seconds,
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

    async def handle_motion(self, source: str, zone_or_device_id, trigger: str = "violated") -> None:
        now = datetime.now()
        for idx, rule in enumerate(self.config.motion_rules):
            if rule["source"] != source:
                continue
            if source == "satel_zone" and rule.get("zone_number") != zone_or_device_id:
                continue

            if not rule.get("enabled", True):
                continue

            if not self._rule_matches_trigger(rule, trigger):
                continue

            if not self._rule_gate_open(rule, now):
                continue

            await self._fire_motion_rule(rule, f"motion_rule_{idx}", source, zone_or_device_id, trigger)

    async def handle_device_state(self, source: str, device_name: str, value: str) -> None:
        state_key = (source, device_name)
        old_value = self._device_state_last_value.get(state_key)
        self._device_state_last_value[state_key] = value
        if old_value == value:
            return

        now = datetime.now()
        for idx, rule in enumerate(self.config.motion_rules):
            if rule.get("source") != source or rule.get("source_device") != device_name:
                continue
            if not rule.get("enabled", True):
                continue

            trigger_value = rule.get("trigger_value")
            if value == trigger_value:
                trigger = "violated"
            elif self._is_until_restored(rule) and old_value == trigger_value:
                trigger = "restored"
            else:
                continue

            if not self._rule_gate_open(rule, now):
                continue

            await self._fire_motion_rule(rule, f"motion_rule_{idx}", source, device_name, trigger)

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

        if "virtual_input" in rule:
            device = rule["virtual_input"]
            target_system = "satel_virtual_input"
        else:
            device = rule["target_device"]
            target_system = rule.get("target_system", "tuya")

        if device in self._active_timers and not self._active_timers[device].done():
            self._active_timers[device].cancel()

        if rule.get("action") == "turn_off":
            await self._set_target_state(target_system, device, False)
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

        duration = rule.get("duration_seconds")
        dimmer = rule.get("dimmer_level_percent")

        if self._is_until_restored(rule):
            await self._set_target_state(target_system, device, trigger != "restored", dimmer)
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

        rule_name = rule.get("name")
        self._active_timers[device] = asyncio.create_task(
            self._run_on_for_duration(target_system, device, duration, dimmer, rule_name)
        )
        logger.info(
            "Reguła ruchu: %s(%s) -> %s (%s) na %ss",
            source, zone_or_device_id, device, target_system, duration,
        )
        self.history.write_event(
            "motion", source=f"{source}:{zone_or_device_id}",
            detail={
                "target_device": device, "target_system": target_system,
                "duration_seconds": duration, "dimmer_level_percent": dimmer,
            },
        )
        await self._chain_trigger(rule, trigger, _chain_depth)

    async def _set_target_state(
        self, target_system: str, device: str, on: bool, dimmer: int | None = None,
    ) -> None:
        if target_system == "satel_output":
            output_number = self.satel.monitored_outputs.get(device)
            if output_number is None:
                logger.warning(
                    "Reguła ruchu wskazuje na nieznane wyjście Satela '%s' (sprawdź zakładkę "
                    "⚙ Ustawienia -> Satel) - pomijam", device,
                )
                return
            await self.satel.set_output(output_number, on)
        elif target_system == "satel_virtual_input":
            await self._trigger_virtual_input(device, on)
        elif on:
            await self.tuya.turn_on(device, dimmer)
        else:
            await self.tuya.turn_off(device)

    async def _run_on_for_duration(
        self, target_system: str, device: str, duration: float, dimmer: int | None, rule_name: str | None,
    ) -> None:
        if rule_name:
            self.history.set_status(f"automation:{rule_name}", "automation", "uruchomiona", label=rule_name)
        try:
            if target_system in ("satel_output", "satel_virtual_input"):
                await self._set_target_state(target_system, device, True)
                await asyncio.sleep(duration)
                await self._set_target_state(target_system, device, False)
            else:
                await self.tuya.turn_on_for_duration(device, duration, dimmer)
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

    async def _trigger_virtual_input(self, name: str, state: bool = True) -> None:
        if self._vi_mode == "http":
            await self.satel_virtual_inputs.trigger(name)
            return
        output_number = self.satel.virtual_inputs.get(name)
        if output_number is None:
            logger.warning(
                "Nieznane wirtualne wejście IP Satela '%s' (sprawdź zakładkę "
                "⚙ Ustawienia -> Satel) - pomijam", name,
            )
            return
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
        self.boiler_automation = BoilerAutomation(new_config.raw.get("boiler_rules", []), self.scenes)
        self.alarms = AlarmEngine(new_config.raw.get("alarm_rules", []), self.history, notify_cfg=self._notify_cfg)

        self.presence_simulation.cfg = new_config.raw.get("presence_simulation", {})
        self.presence_simulation.location_cfg = self._location_cfg

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
                    if dps is None:
                        self.history.set_status(
                            f"tuya:{device_name}", "tuya", "niedostępne", label=device_name,
                        )
                        await self.handle_device_state("tuya_state", device_name, "niedostępne")
                        continue
                    is_on = dps.get("1")
                    tuya_value = "włączone" if is_on else "wyłączone" if is_on is not None else "?"
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
                    cfg = self.config.tuya["devices"].get(device_name)
                    if cfg and cfg.get("type") == "dimmer" and cfg.get("dimmer_dp") in dps:
                        lo, hi = cfg.get("dimmer_range", [10, 1000])
                        raw = dps[cfg["dimmer_dp"]]
                        percent = round((raw - lo) / (hi - lo) * 100) if hi > lo else 0
                        self.history.set_status(
                            f"tuya:{device_name}:brightness", "tuya", f"{percent}%",
                            label=f"{device_name} (jasność)",
                        )
            except Exception:
                logger.exception("Błąd w pętli odpytywania statusów Tuya")
            await asyncio.sleep(self.config.tuya.get("status_poll_interval_seconds", 15))

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

        tuya_keys = {f"tuya:{name}" for name in self.tuya.devices_cfg}
        tuya_keys |= {f"tuya:{name}:brightness" for name in self.tuya.devices_cfg}
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

    async def start(self) -> None:
        self.history.write_event("system", detail={"message": "Bridge uruchomiony"})
        asyncio.create_task(self._stale_check_loop())

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
                        await self.tuya.turn_on(name)
                    else:
                        await self.tuya.turn_off(name)
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

        logger.info("Silnik reguł uruchomiony i gotowy")
