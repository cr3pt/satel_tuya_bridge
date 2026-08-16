"""Silnik reguł - centralny punkt, w którym zdarzenia (ruch, zmiana stanu
światła, ruch z kamery, dane z kotła) trafiają na akcje zdefiniowane
w config.yaml.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from .config import BridgeConfig
from .satel_client import SatelIntegraClient, SatelVirtualInputTrigger
from .tuya_local import TuyaLocalController
from .tuya_cloud_events import TuyaCameraMotionListener
from .unifi_protect_events import UnifiProtectMotionListener
from .ebus_client import EbusMqttListener
from .boiler_output import build_boiler_publishers
from .history_store import HistoryStore
from .alarms import AlarmEngine
from .resilience import HealthReporter
from .scheduling import is_within_window
from .scenes import SceneRunner
from .presence_simulation import PresenceSimulation
from .boiler_automation import BoilerAutomation
from .maintenance import run_maintenance
from .ha_discovery import build_all_entities
from .ha_mqtt import HomeAssistantMqttBridge

logger = logging.getLogger("bridge.rules")


class RulesEngine:
    def __init__(self, config: BridgeConfig):
        self.config = config

        # Historia + alarmy tworzone najpierw - inne komponenty (Satel, MQTT,
        # kamery) dostają referencję do HistoryStore, żeby raportować status
        # połączenia ('system_health') widoczny w panelu.
        self.history = HistoryStore(config.raw.get("dashboard", {}).get("db_path", "bridge_history.db"))
        notify_cfg = config.raw.get("notifications")
        self.alarms = AlarmEngine(
            config.raw.get("alarm_rules", []),
            self.history,
            notify_cfg=notify_cfg,
        )
        # Ile sekund nieprzerwanego rozłączenia, zanim wyślemy powiadomienie
        # push o utracie połączenia - chroni przed spamem przy chwilowych
        # zerwaniach WiFi. Konfigurowalne globalnie (notifications.grace_seconds).
        grace_seconds = (notify_cfg or {}).get("grace_seconds", 120)

        self.tuya = TuyaLocalController(config.tuya["devices"])
        self.satel_virtual_inputs = SatelVirtualInputTrigger(
            config.satel["host"], config.satel["virtual_inputs"]
        )
        self.satel = SatelIntegraClient(
            config.satel["host"],
            config.satel["port"],
            config.satel["integration_key"],
            config.satel.get("partition", 1),
            monitored_zones=config.satel.get("monitored_zones"),
            monitored_outputs=config.satel.get("monitored_outputs"),
            history=self.history,
            notify_cfg=notify_cfg,
            grace_seconds=grace_seconds,
        )
        self._active_timers: dict[str, asyncio.Task] = {}
        self.boiler_publishers = build_boiler_publishers(config, history=self.history)
        self._notify_cfg = notify_cfg
        self._grace_seconds = grace_seconds
        self._tuya_health: HealthReporter | None = None  # tworzony w start(), potrzebuje działającej pętli asyncio

        # Lokalizacja (do okien czasowych sunset/sunrise) - opcjonalna,
        # potrzebna tylko jeśli jakaś reguła używa time_window z "sunset"/"sunrise"
        self._location_cfg = config.raw.get("location")

        # Sceny - wspólny fundament dla harmonogramów, eskalacji i
        # automatyki kotła (patrz bridge/scenes.py)
        self.scenes = SceneRunner(
            config.raw.get("scenes", []), self.tuya, self.satel, self.satel_virtual_inputs, history=self.history,
        )
        self.boiler_automation = BoilerAutomation(config.raw.get("boiler_rules", []), self.scenes)
        self.presence_simulation = PresenceSimulation(
            config.raw.get("presence_simulation", {}), self.tuya, location_cfg=self._location_cfg, history=self.history,
        )

        # Stan w pamięci dla eskalacji (znaczniki czasu naruszeń per reguła)
        # i cooldownu reguł kamer (ostatnie wywołanie per reguła)
        self._escalation_history: dict[str, list[float]] = {}
        self._camera_rule_last_triggered: dict[str, float] = {}

        # Pętla asyncio - potrzebna, żeby bezpiecznie wywołać korutyny
        # (boiler_automation) z callbacku MQTT działającego w INNYM wątku
        # (patrz handle_boiler_value niżej)
        self._loop = asyncio.get_event_loop()

        # Integracja z Home Assistant (MQTT Discovery) - opcjonalna
        self.ha_mqtt: HomeAssistantMqttBridge | None = None
        self._ha_topic_prefix = config.raw.get("home_assistant", {}).get("state_topic_prefix", "home/bridge")
        if config.raw.get("home_assistant", {}).get("enabled"):
            boiler_topic_prefix = config.boiler_output.get("mqtt", {}).get("topic_prefix", "home/boiler")
            entities = build_all_entities(config, boiler_topic_prefix)
            self.ha_mqtt = HomeAssistantMqttBridge(
                config.raw["home_assistant"], entities, history=self.history,
                notify_cfg=notify_cfg, grace_seconds=grace_seconds,
            )

        # Publikacja stanu Satel/Tuya do MQTT pod skonfigurowalnym tematem
        # PER INTEGRACJA - NIEZALEŻNIE od Home Assistant (ha_mqtt wyżej
        # publikuje TYLKO gdy home_assistant.enabled: true, pod jednym
        # wspólnym `state_topic_prefix`). eBUS ma już swój odpowiednik od
        # dawna (boiler_output.mqtt.topic_prefix, patrz handle_boiler_value)
        # - to jest analogiczny mechanizm dla Satela i Tuyi. Osobny klient
        # paho-mqtt (nie dzielimy z ha_mqtt/ebus_client/boiler_output) -
        # zgodnie z resztą kodu, patrz docstring ha_mqtt.py.
        self.state_mqtt: HomeAssistantMqttBridge | None = None
        mqtt_publish_cfg = config.raw.get("mqtt_publish", {})
        self._satel_mqtt_topic = mqtt_publish_cfg.get("topics", {}).get("satel", "satel")
        self._tuya_mqtt_topic = mqtt_publish_cfg.get("topics", {}).get("tuya", "tuya")
        if mqtt_publish_cfg.get("enabled", True) and config.ebus.get("mqtt_host"):
            self.state_mqtt = HomeAssistantMqttBridge(
                {"mqtt_host": config.ebus["mqtt_host"], "mqtt_port": config.ebus.get("mqtt_port", 1883)},
                entities=[], history=self.history, notify_cfg=notify_cfg, grace_seconds=grace_seconds,
            )

    # ------------------------------------------------------------------
    # Ruch -> włączenie urządzenia Tuya na czas (LUB scena, LUB eskalacja
    # przy powtarzających się naruszeniach - patrz config.yaml)
    # ------------------------------------------------------------------
    async def handle_motion(self, source: str, zone_or_device_id) -> None:
        now = datetime.now()
        for idx, rule in enumerate(self.config.motion_rules):
            if rule["source"] != source:
                continue
            if source == "satel_zone" and rule.get("zone_number") != zone_or_device_id:
                continue
            # dla source == "tuya_pir" dopasowanie po device_id analogicznie

            if not is_within_window(rule.get("time_window"), self._location_cfg, now):
                continue  # poza skonfigurowanym oknem czasowym - reguła teraz nieaktywna

            rule_key = f"motion_rule_{idx}"

            # Eskalacja: licz naruszenia w ruchomym oknie czasowym,
            # niezależnie od zwykłej akcji. Po przekroczeniu progu -
            # mocniejsza scena ZAMIAST zwykłej akcji (nie oba naraz).
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
                    history.clear()  # reset po eskalacji, żeby nie spamować tej samej sceny co naruszenie
                    continue

            if "scene" in rule:
                await self.scenes.run(rule["scene"])
                self.history.write_event(
                    "motion", source=f"{source}:{zone_or_device_id}", detail={"scene": rule["scene"]},
                )
                continue

            device = rule["target_device"]
            duration = rule["duration_seconds"]
            dimmer = rule.get("dimmer_level_percent")

            # jeśli timer już trwa dla tego urządzenia - zresetuj go
            # (kolejny ruch przedłuża włączenie, zamiast nakładać zadania)
            if device in self._active_timers and not self._active_timers[device].done():
                self._active_timers[device].cancel()

            self._active_timers[device] = asyncio.create_task(
                self.tuya.turn_on_for_duration(device, duration, dimmer)
            )
            logger.info(
                "Reguła ruchu: %s(%s) -> %s na %ss",
                source, zone_or_device_id, device, duration,
            )
            self.history.write_event(
                "motion", source=f"{source}:{zone_or_device_id}",
                detail={"target_device": device, "duration_seconds": duration, "dimmer_level_percent": dimmer},
            )

    # ------------------------------------------------------------------
    # Zmiana stanu światła Tuya -> wirtualne wejście ETHM
    # ------------------------------------------------------------------
    async def handle_light_state_change(self, device_name: str, is_on: bool) -> None:
        if not is_on:
            return
        for rule in self.config.light_to_satel_rules:
            if rule["source_device"] == device_name:
                await self.satel_virtual_inputs.trigger(rule["virtual_input"])
                self.history.write_event(
                    "light_to_satel", source=f"tuya:{device_name}",
                    detail={"virtual_input": rule["virtual_input"]},
                )

    # ------------------------------------------------------------------
    # Ruch z kamery -> akcja w Satelu (LUB scena), z opcjonalnym oknem
    # czasowym i cooldownem (żeby migoczący ruch nie strzelał w kółko)
    # ------------------------------------------------------------------
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
                await self.satel_virtual_inputs.trigger(rule["virtual_input"])
            elif action == "output":
                await self.satel.set_output(rule["output_number"], True)
            elif action == "arm":
                await self.satel.arm(rule.get("arm_mode", 0))
            logger.info("Reguła kamery: %s(%s) -> %s", source, camera_id, action)
            self.history.write_event(
                "camera_alarm", source=f"{source}:{camera_id}", detail={"action": action},
            )

    # ------------------------------------------------------------------
    # Dane z kotła -> publikacja WEDŁUG WYBORU per czujnik (targets w
    # config.yaml: ebus.sensors[].targets) - historia/panel, MQTT,
    # wirtualne urządzenie Tuya to trzy NIEZALEŻNE przełączniki.
    # ------------------------------------------------------------------
    def handle_boiler_value(self, sensor_cfg: dict, value) -> None:
        name = sensor_cfg["name"]
        label = sensor_cfg.get("label", name)
        targets = sensor_cfg.get("targets", {})
        is_numeric = isinstance(value, (int, float))

        if targets.get("history", True):
            if is_numeric:
                self.history.write_reading(name, value)  # tabela readings wymaga liczby (wykres historyczny)
            self.history.set_status(f"boiler:{name}", "boiler", value, label=label)

        self.alarms.on_value(name, value)  # sam decyduje, czy wartość nadaje się do progów liczbowych

        # boiler_automation.on_value jest korutyną, a handle_boiler_value bywa
        # wywoływane z wątku MQTT (paho), nie z głównej pętli asyncio - stąd
        # run_coroutine_threadsafe zamiast zwykłego await/create_task.
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
            await self.alarms.check_stale_sensors()

    # ------------------------------------------------------------------
    # Rotacja starych danych + opcjonalny backup - raz na dobę, o ile
    # config.yaml (maintenance.run_in_process) tego nie wyłączy (np. gdy
    # wolisz to robić przez cron zamiast wbudowanego mechanizmu - patrz
    # maintenance.py i README).
    #
    # VACUUM (część czyszczenia) to blokująca operacja I/O na dysku - żeby
    # nie zamrozić na ten czas reszty pętli asyncio (Satel, MQTT, panel...),
    # uruchamiamy ją w osobnym wątku wykonawczym.
    # ------------------------------------------------------------------
    async def _maintenance_loop(self) -> None:
        maint_cfg = self.config.raw.get("maintenance", {})
        if not maint_cfg.get("run_in_process", True):
            logger.info("Automatyczna rotacja/backup wyłączona w procesie (maintenance.run_in_process: false)")
            return

        interval_seconds = maint_cfg.get("interval_hours", 24) * 3600
        # Pierwsze uruchomienie z opóźnieniem, żeby nie robić ciężkiego I/O
        # tuż po starcie procesu, kiedy i tak dzieje się dużo innych rzeczy
        # (łączenie z Satelem, MQTT, itd.)
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

    # ------------------------------------------------------------------
    # Kolejka komend z panelu webowego (uzbrojenie/rozbrojenie stref) -
    # panel to OSOBNY proces bez dostępu do żywego połączenia z Satelem,
    # więc komunikują się przez tę samą bazę SQLite (patrz auth.py w
    # dashboard oraz history_store.py: enqueue_command/get_pending_commands).
    # ------------------------------------------------------------------
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
            else:
                self.history.mark_command_error(cmd["id"], f"Nieznana komenda: {cmd['command']}")
        except Exception as exc:
            logger.exception("Błąd wykonania komendy #%s (%s)", cmd["id"], cmd["command"])
            self.history.mark_command_error(cmd["id"], str(exc))

    # ------------------------------------------------------------------
    # Cykliczne odpytywanie statusów Tuya (nie mają callbacków lokalnie -
    # trzeba je samemu pollingować; interwał dobrany tak, żeby nie
    # obciążać RPi Zero W ani sieci)
    # ------------------------------------------------------------------
    async def _tuya_status_poll_loop(self, interval_seconds: int = 30) -> None:
        # HealthReporter tworzony tutaj (nie w __init__) - potrzebuje
        # działającej pętli asyncio, którą mamy dopiero po starcie.
        self._tuya_health = HealthReporter(
            "tuya_local", history=self.history, notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
        )
        while True:
            try:
                statuses = await self.tuya.get_all_statuses()
                unreachable = [name for name, dps in statuses.items() if dps is None]
                # Zdrowie = połączono, jeśli PRZYNAJMNIEJ JEDNO urządzenie
                # odpowiada - pojedyncze niedostępne urządzenie to zwykle
                # jego własny problem (np. wyjęty z gniazdka), a nie awaria
                # całego kanału komunikacji z Tuya. Pełne "rozłączono" (i
                # ewentualne powiadomienie) tylko gdy WSZYSTKIE nie odpowiadają.
                all_unreachable = statuses and len(unreachable) == len(statuses)
                self._tuya_health.report(
                    not all_unreachable,
                    f"niedostępne: {', '.join(unreachable)}" if unreachable else "",
                )
                for device_name, dps in statuses.items():
                    if dps is None:
                        continue
                    # DP '1' to konwencjonalnie stan on/off większości urządzeń Tuya
                    is_on = dps.get("1")
                    self.history.set_status(
                        f"tuya:{device_name}", "tuya",
                        "włączone" if is_on else "wyłączone" if is_on is not None else "?",
                        label=device_name,
                    )
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
                    cfg = self.config.tuya["devices"][device_name]
                    if cfg.get("type") == "dimmer" and cfg.get("dimmer_dp") in dps:
                        lo, hi = cfg.get("dimmer_range", [10, 1000])
                        raw = dps[cfg["dimmer_dp"]]
                        percent = round((raw - lo) / (hi - lo) * 100) if hi > lo else 0
                        self.history.set_status(
                            f"tuya:{device_name}:brightness", "tuya", f"{percent}%",
                            label=f"{device_name} (jasność)",
                        )
            except Exception:
                # Ta pętla działa w tle przez cały czas życia procesu -
                # pojedynczy nieoczekiwany błąd nie może jej zabić, bo
                # straciłoby się cykliczne statusy Tuya na zawsze.
                logger.exception("Błąd w pętli odpytywania statusów Tuya")
            await asyncio.sleep(interval_seconds)

    def _seed_satel_status_labels(self) -> None:
        """Zapisuje początkowy stan (OK/nieaktywne) dla wszystkich
        monitorowanych wejść/wyjść/stref, żeby panel pokazywał je od razu,
        zanim cokolwiek się zmieni."""
        for name, number in self.satel.monitored_zones.items():
            self.history.set_status(f"satel_input:{number}", "satel_input", "OK", label=name)
        for name, number in self.satel.monitored_outputs.items():
            self.history.set_status(f"satel_output:{number}", "satel_output", "wyłączone", label=name)
        for name, number in self.config.satel.get("partitions", {}).items():
            self.history.set_status(f"satel_partition:{number}", "satel_partition", "nieznany", label=name)

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self.history.write_event("system", detail={"message": "Bridge uruchomiony"})
        asyncio.create_task(self._stale_check_loop())

        self._seed_satel_status_labels()
        zone_name_by_number = {v: k for k, v in self.satel.monitored_zones.items()}
        output_name_by_number = {v: k for k, v in self.satel.monitored_outputs.items()}

        # Callback z centrali: zmiana wejść -> reguły ruchu (source=satel_zone)
        # + zapis statusu (widoczny w panelu jako "wejścia Satela").
        # Rejestrujemy PRZED uruchomieniem run_forever() - callbacki są
        # zapamiętane na obiekcie klienta i automatycznie podpinane na nowo
        # przy każdym (re)połączeniu, patrz satel_client.py.
        async def _on_zone_change(zones: dict) -> None:
            for zone_number, violated in zones.items():
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
                if violated:
                    await self.handle_motion("satel_zone", zone_number)

        async def _on_output_change(outputs: dict) -> None:
            for output_number, active in outputs.items():
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

        partition_name_by_number = {v: k for k, v in self.config.satel.get("partitions", {}).items()}

        async def _on_alarm_status_change(partition_states: dict) -> None:
            # Kształt `partition_states` zależy od wersji biblioteki
            # satel_integra - zakładamy {numer_strefy: bool_uzbrojona},
            # zweryfikuj przy pierwszym użyciu z realną centralą.
            for partition_number, armed in partition_states.items():
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

        self.satel.register_zone_callback(_on_zone_change)
        self.satel.register_output_callback(_on_output_change)
        self.satel.register_alarm_status_callback(_on_alarm_status_change)

        # Wszystkie długożyjące połączenia jako nadzorowane zadania w tle -
        # każde samo się podnosi po awarii (patrz bridge/resilience.py).
        # Awaria jednego komponentu (np. Satel chwilowo nieosiągalny) nie
        # blokuje ani nie zatrzymuje pozostałych.
        asyncio.create_task(self.satel.run_forever())
        asyncio.create_task(self._tuya_status_poll_loop(
            self.config.tuya.get("status_poll_interval_seconds", 30)
        ))
        asyncio.create_task(self.presence_simulation.run_forever())
        asyncio.create_task(self._command_poll_loop())
        asyncio.create_task(self._maintenance_loop())

        # eBUS / kocioł (paho-mqtt ma wbudowany reconnect - patrz ebus_client.py)
        ebus_listener = EbusMqttListener(
            self.config.ebus, self.handle_boiler_value, history=self.history,
            notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
        )
        ebus_listener.start()

        # Kamera Tuya (opcjonalnie, jeśli skonfigurowana)
        if "cloud" in self.config.tuya:
            camera_listener = TuyaCameraMotionListener(
                self.config.tuya["cloud"],
                lambda cam_id: self.handle_camera_motion("tuya_cloud", cam_id),
                history=self.history,
                notify_cfg=self._notify_cfg,
                grace_seconds=self._grace_seconds,
            )
            asyncio.create_task(camera_listener.run_forever())

        # Kamera UniFi Protect (opcjonalnie)
        if self.config.unifi_protect:
            unifi_listener = UnifiProtectMotionListener(
                self.config.unifi_protect,
                lambda cam_id: self.handle_camera_motion("unifi_protect", cam_id),
                history=self.history,
                notify_cfg=self._notify_cfg,
                grace_seconds=self._grace_seconds,
            )
            asyncio.create_task(unifi_listener.run_forever())

        # Integracja z Home Assistant (opcjonalna) - rejestrujemy handlery
        # komend PRZED startem, żeby subskrypcja MQTT objęła je od razu
        # po połączeniu (patrz HomeAssistantMqttBridge._on_connect).
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
