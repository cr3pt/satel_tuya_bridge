"""Silnik reguł - centralny punkt, w którym zdarzenia (ruch, zmiana stanu
światła, ruch z kamery, dane z kotła) trafiają na akcje zdefiniowane
w config.yaml.
"""
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
from .tuya_cloud_events import TuyaCameraMotionListener
from .unifi_protect_events import UnifiProtectMotionListener
from .ebus_client import EbusMqttListener
from .boiler_output import build_boiler_publishers, MqttBoilerPublisher, TuyaVirtualDevicePublisher
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

        self.tuya = TuyaLocalController(config.tuya["devices"], config.tuya.get("zigbee_gateways"))
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
            status_poll_interval_seconds=config.satel.get("status_poll_interval_seconds", 15),
        )
        self._active_timers: dict[str, asyncio.Task] = {}
        # Zadanie odliczające debounce dla _on_alarm_status_change() niżej -
        # patrz tam po wyjaśnienie.
        self._alarm_status_debounce_task: asyncio.Task | None = None
        self.boiler_publishers = build_boiler_publishers(config, history=self.history)
        self.ebus_listener: EbusMqttListener | None = None  # utworzony w start(), potrzebny reload_config()
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
                await self.satel.arm(rule.get("arm_mode", 0), rule.get("partitions"))
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
            elif cmd["command"] == "reload_config":
                result = await self.reload_config()
                self.history.mark_command_done(cmd["id"], result)
            elif cmd["command"] == "restart_process":
                self.history.mark_command_done(cmd["id"], {"message": "Restartuję proces bridge'a"})
                self.history.write_event("system", detail={"message": "Restart procesu bridge'a zażądany z panelu"})
                # Twardy exit (nie sys.exit/SystemExit - to dałoby się złapać
                # gdzieś w kodzie i zignorować) - Docker Compose ma dla obu
                # kontenerów `restart: unless-stopped` (patrz docker-compose.yml),
                # więc po zakończeniu procesu kontener sam wstaje ponownie,
                # z ŚWIEŻYM stanem procesu (nowe połączenia TCP, ponowne
                # wczytanie CAŁEGO config.yaml - w odróżnieniu od
                # reload_config() wyżej, które celowo NIE dotyka połączeń
                # sieciowych). Krótkie opóźnienie na osobnym zadaniu, żeby
                # mark_command_done()/write_event() wyżej zdążyły się zapisać
                # (SQLite - synchroniczne, ale dajemy margines na wypadek
                # równoległych zapisów) zanim proces zniknie.
                asyncio.get_event_loop().call_later(0.5, os._exit, 0)
            else:
                self.history.mark_command_error(cmd["id"], f"Nieznana komenda: {cmd['command']}")
        except Exception as exc:
            logger.exception("Błąd wykonania komendy #%s (%s)", cmd["id"], cmd["command"])
            self.history.mark_command_error(cmd["id"], str(exc))

    # ------------------------------------------------------------------
    # Przeładowanie konfiguracji BEZ restartu procesu - patrz też
    # _execute_command (komenda 'reload_config') i dashboard/app.py, gdzie
    # panel dokłada tę komendę automatycznie po każdym zapisie czujników
    # eBUS/pól Satela/urządzeń Tuya/automatyzacji.
    # ------------------------------------------------------------------
    async def reload_config(self) -> dict:
        """Przeładowuje 'lekką' część konfiguracji - dokładnie to, co
        zmienia się CZĘSTO podczas donastrajania instalacji: czujniki eBUS
        (sensors.yaml), pola Satela (satel_fields.yaml - etykiety/numery
        stref/wyjść/wejść IP), urządzenia Tuya (tuya_devices.yaml), sceny/
        reguły ruchu/symulacja obecności (automations.yaml), alarmy progowe.

        CELOWO NIE dotyka żywych połączeń sieciowych - gniazdo TCP do
        centrali Satel, klienci MQTT (eBUS/Home Assistant/publikacja
        stanu), Tuya Cloud Pulsar. Host/port/klucz integracji Satela,
        adresy brokerów MQTT i poświadczenia Tuya Cloud (zakładka
        🔌 Połączenia) WCIĄŻ wymagają pełnego restartu procesu - bezpieczne
        zamknięcie i odtworzenie żywych połączeń w trakcie działania byłoby
        dużo bardziej ryzykowne niż warte tego dla ustawień, które i tak
        zmienia się rzadko (w odróżnieniu od list czujników/urządzeń/reguł,
        edytowanych często)."""
        new_config = BridgeConfig.load(self.config.db_path)
        self.config = new_config
        apply_log_levels(new_config.raw)

        # Tuya lokalne - bezpieczne do podmiany w całości: TuyaLocalController
        # nie trzyma żywego połączenia (tylko krótkie żądania per komenda).
        self.tuya = TuyaLocalController(new_config.tuya["devices"], new_config.tuya.get("zigbee_gateways"))

        # Satel - etykiety/numery monitorowanych stref/wyjść/wejść IP,
        # AKTUALIZOWANE na już istniejącym obiekcie klienta (NIE tworzymy
        # nowego SatelIntegraClient - to zerwałoby żywe połączenie TCP).
        # satel_integra przyjmuje monitored_zones/monitored_outputs tylko
        # w konstruktorze AsyncSatel, więc faktyczne MONITOROWANIE nowych/
        # usuniętych numerów zacznie działać dopiero po najbliższym
        # (re)połączeniu - etykiety w panelu działają jednak natychmiast
        # (patrz odświeżone na bieżąco zamknięcia _on_zone_change itp. w
        # start()).
        self.satel.monitored_zones = new_config.satel.get("monitored_zones", {})
        self.satel.monitored_outputs = new_config.satel.get("monitored_outputs", {})
        # Interwał cyklicznego dopytywania o stan (patrz satel_client.py:
        # _periodic_status_sync) - W ODRÓŻNIENIU od host/portu/klucza,
        # DZIAŁA NA ŻYWO od razu, bez restartu procesu - pętla czyta ten
        # atrybut na bieżąco przy każdym obrocie, nie tylko raz na starcie.
        self.satel.status_poll_interval_seconds = new_config.satel.get("status_poll_interval_seconds", 15)
        self.satel_virtual_inputs = SatelVirtualInputTrigger(
            new_config.satel["host"], new_config.satel["virtual_inputs"],
        )

        # Sceny/automatyka kotła/alarmy progowe - budowane na bieżąco z
        # configu przy każdym wywołaniu, bezpieczne do pełnej podmiany.
        self.scenes = SceneRunner(
            new_config.raw.get("scenes", []), self.tuya, self.satel, self.satel_virtual_inputs, history=self.history,
        )
        self.boiler_automation = BoilerAutomation(new_config.raw.get("boiler_rules", []), self.scenes)
        self.alarms = AlarmEngine(new_config.raw.get("alarm_rules", []), self.history, notify_cfg=self._notify_cfg)

        # Symulacja obecności - AKTUALIZOWANA na istniejącym obiekcie (NIE
        # podmieniana) - jej run_forever() to już działające zadanie w tle,
        # przywiązane do KONKRETNEGO obiektu; podmiana obiektu zgubiłaby to
        # zadanie na zawsze. Bezpieczne, bo pętla w presence_simulation.py
        # czyta self.cfg NA ŚWIEŻO przy każdym obrocie. WYJĄTEK: jeśli przy
        # starcie procesu było enabled=false, ta pętla już się zakończyła -
        # dopiero TERAZ włączenie tej opcji wciąż wymaga restartu procesu.
        self.presence_simulation.cfg = new_config.raw.get("presence_simulation", {})

        # eBUS - AKTUALIZOWANY na istniejącym listenerze (subskrybuje nowe
        # tematy bez zrywania połączenia MQTT, patrz ebus_client.py).
        if self.ebus_listener is not None:
            self.ebus_listener.update_sensors(new_config.ebus)

        # Publishery danych z kotła (MQTT/Tuya virtual device) - budowane
        # LENIWIE: jeśli nowa lista czujników potrzebuje kanału, którego
        # jeszcze nie ma (np. dopiero co zaznaczono "MQTT" na jakimś
        # czujniku, a wcześniej żaden tego nie potrzebował), tworzymy go
        # teraz. Już istniejące zostają NIETKNIĘTE - nie zrywamy działających
        # połączeń tylko po to, żeby je zaraz odtworzyć identycznie.
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

        logger.info("Konfiguracja przeładowana bez restartu procesu")
        self.history.write_event("system", detail={"message": "Konfiguracja przeładowana (bez restartu procesu)"})
        return {
            "message": "Konfiguracja przeładowana bez restartu procesu (czujniki eBUS, pola Satela, "
                       "urządzenia Tuya, automatyzacje, alarmy progowe). Zmiany połączeń (host/port/"
                       "klucze/brokery MQTT, zakładka 🔌 Połączenia) wciąż wymagają pełnego restartu.",
        }

    # ------------------------------------------------------------------
    # Cykliczne odpytywanie statusów Tuya (nie mają callbacków lokalnie -
    # trzeba je samemu pollingować; interwał dobrany tak, żeby nie
    # obciążać RPi Zero W ani sieci)
    # ------------------------------------------------------------------
    async def _tuya_status_poll_loop(self) -> None:
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
                        # BEZ TEGO wpisu: urządzenie, które akurat nie
                        # odpowiada (zły local_key/IP/wersja protokołu,
                        # offline), w ogóle nie dostałoby żadnego statusu -
                        # zniknęłoby z panelu (a raczej NIGDY by się na nim
                        # nie pojawiło), bez żadnej wskazówki, że w ogóle
                        # jest skonfigurowane. Health kanału (system_health)
                        # jest już raportowany wyżej - to tu jest per-urządzenie.
                        self.history.set_status(
                            f"tuya:{device_name}", "tuya", "niedostępne", label=device_name,
                        )
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
            # Odczytywane NA ŚWIEŻO przy każdym obrocie (nie raz, na
            # starcie zadania) - żeby RulesEngine.reload_config() mogło
            # podmienić interwał na żywo, bez restartu procesu (patrz
            # analogiczne rozwiązanie dla Satela: SatelIntegraClient.
            # status_poll_interval_seconds).
            await asyncio.sleep(self.config.tuya.get("status_poll_interval_seconds", 30))

    def _seed_satel_status_labels(self) -> None:
        """Zapisuje początkowy stan (OK/nieaktywne) dla wszystkich
        monitorowanych wejść/wyjść/stref, żeby panel pokazywał je od razu,
        zanim cokolwiek się zmieni. Pomija numery, które JUŻ MAJĄ zapisany
        status (patrz seen niżej) - ta metoda jest wołana też z
        reload_config() (przy KAŻDYM zapisie dowolnego formularza w /config,
        nie tylko pól Satela), więc bez tego pomijania nadpisywałaby żywy,
        aktualny stan placeholderem przy każdym takim zapisie."""
        seen = {s["key"] for s in self.history.get_status("satel_input")}
        seen |= {s["key"] for s in self.history.get_status("satel_output")}
        seen |= {s["key"] for s in self.history.get_status("satel_partition")}
        for name, number in self.satel.monitored_zones.items():
            key = f"satel_input:{number}"
            if key not in seen:
                self.history.set_status(key, "satel_input", "OK", label=name)
        for name, number in self.satel.monitored_outputs.items():
            key = f"satel_output:{number}"
            if key not in seen:
                self.history.set_status(key, "satel_output", "wyłączone", label=name)
        for name, number in self.config.satel.get("partitions", {}).items():
            key = f"satel_partition:{number}"
            if key not in seen:
                self.history.set_status(key, "satel_partition", "nieznany", label=name)

    def _seed_tuya_status_labels(self) -> None:
        """Zapisuje wpis '?' dla KAŻDEGO skonfigurowanego urządzenia Tuya,
        żeby panel od razu je pokazywał. BEZ TEGO: _tuya_status_poll_loop()
        w ogóle NIE zapisuje statusu dla urządzenia, które akurat nie
        odpowiada (zły local_key/IP/wersja protokołu, urządzenie offline -
        patrz `if dps is None: continue` niżej) - dodane, ale niedostępne
        urządzenie NIGDY by się nie pojawiło na głównej stronie, bez żadnej
        wskazówki w panelu, że w ogóle istnieje w konfiguracji. Pomija
        urządzenia, które już mają zapisany status - patrz analogiczny
        komentarz w _seed_satel_status_labels()."""
        seen = {s["key"] for s in self.history.get_status("tuya")}
        for name in self.tuya.devices_cfg:
            key = f"tuya:{name}"
            if key not in seen:
                self.history.set_status(key, "tuya", "?", label=name)

    async def _on_alarm_status_change(self) -> None:
        """Rzeczywisty callback rejestrowany przez register_alarm_status_callback
        (patrz start() niżej) - odbija (debounce'uje) wybuchy powiadomień w
        _handle_alarm_status_change() poniżej, tak samo jak robi to
        integracja Home Assistant (PARTITION_UPDATE_DEBOUNCE_DELAY=0.15 w
        homeassistant/components/satel_integra/coordinator.py).

        DLACZEGO: centrala wysyła stan każdej strefy JAKO OSOBNĄ ramkę per
        tryb (0x0A/0x2A/0x0B/0x0C/0x0E/... - patrz AsyncSatel.partition_states),
        więc pojedyncza zmiana uzbrojenia/alarmu potrafi wywołać ten callback
        KILKA razy pod rząd, zanim partition_states w bibliotece zdąży się
        w pełni ustabilizować. Bez debounce _handle_alarm_status_change()
        publikowałoby do MQTT/historii kilka pośrednich (niepełnych) stanów
        zamiast jednego, ostatecznego."""
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
        """Odświeża status uzbrojenia WSZYSTKICH skonfigurowanych stref w
        panelu/MQTT. Wołane (z debounce, patrz _on_alarm_status_change
        wyżej) po każdym powiadomieniu "coś się zmieniło" z Satela -
        powiadomienie samo nie niesie stanu, więc trzeba go odczytać osobno
        z self.satel.armed_partition_numbers, dla WSZYSTKICH skonfigurowanych
        stref (nie tylko tej, która się akurat zmieniła - nie wiemy, która
        to była). Wydzielone jako osobna metoda (nie domknięcie wewnątrz
        start()) celowo - da się przetestować wprost, bez uruchamiania
        całego start() (żywe połączenia MQTT/zadania w tle)."""
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

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self.history.write_event("system", detail={"message": "Bridge uruchomiony"})
        asyncio.create_task(self._stale_check_loop())

        self._seed_satel_status_labels()
        self._seed_tuya_status_labels()

        # Callback z centrali: zmiana wejść -> reguły ruchu (source=satel_zone)
        # + zapis statusu (widoczny w panelu jako "wejścia Satela").
        # Rejestrujemy PRZED uruchomieniem run_forever() - callbacki są
        # zapamiętane na obiekcie klienta i automatycznie podpinane na nowo
        # przy każdym (re)połączeniu, patrz satel_client.py.
        #
        # Etykiety (zone_name_by_number itp.) są liczone NA ŚWIEŻO z
        # self.satel/self.config PRZY KAŻDYM zdarzeniu (nie raz, na starcie)
        # - celowo, żeby reload_config() (patrz niżej) mogło podmienić
        # numery/nazwy stref-wyjść-partycji w locie i te zamknięcia od razu
        # zobaczyły nowe wartości, bez ponownego rejestrowania callbacków.
        async def _on_zone_change(zones: dict) -> None:
            zone_name_by_number = {v: k for k, v in self.satel.monitored_zones.items()}
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
            output_name_by_number = {v: k for k, v in self.satel.monitored_outputs.items()}
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

        self.satel.register_zone_callback(_on_zone_change)
        self.satel.register_output_callback(_on_output_change)
        self.satel.register_alarm_status_callback(self._on_alarm_status_change)

        # Wszystkie długożyjące połączenia jako nadzorowane zadania w tle -
        # każde samo się podnosi po awarii (patrz bridge/resilience.py).
        # Awaria jednego komponentu (np. Satel chwilowo nieosiągalny) nie
        # blokuje ani nie zatrzymuje pozostałych.
        asyncio.create_task(self.satel.run_forever())
        asyncio.create_task(self._tuya_status_poll_loop())
        asyncio.create_task(self.presence_simulation.run_forever())
        asyncio.create_task(self._command_poll_loop())
        asyncio.create_task(self._maintenance_loop())

        # eBUS / kocioł (paho-mqtt ma wbudowany reconnect - patrz ebus_client.py)
        # Trzymane na self (nie jako lokalna zmienna) - reload_config()
        # potrzebuje do niej dostępu, żeby zaktualizować listę czujników
        # bez zrywania połączenia MQTT.
        self.ebus_listener = EbusMqttListener(
            self.config.ebus, self.handle_boiler_value, history=self.history,
            notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
        )
        self.ebus_listener.start()

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
