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
    """Buduje kontroler Tuya (lokalny + fallback chmurowy) z sekcji
    `tuya` configu - wspólne dla RulesEngine.__init__ i reload_config, żeby
    obie ścieżki budowały go identycznie. Chmurowy kontroler jest budowany
    ZAWSZE (nawet bez skonfigurowanych urządzeń/poświadczeń) - patrz
    TuyaController, który i tak nigdy do niego nie sięgnie, jeśli
    `cloud_devices` jest puste."""
    local = TuyaLocalController(tuya_cfg["devices"], tuya_cfg.get("zigbee_gateways"))
    cloud = TuyaCloudController(tuya_cfg.get("cloud_devices", {}), tuya_cfg.get("cloud", {}))
    return TuyaController(local, cloud)


class RulesEngine:
    def __init__(self, config: BridgeConfig):
        self.config = config

        # Historia + alarmy tworzone najpierw - inne komponenty (Satel, MQTT,
        # kamery) dostają referencję do HistoryStore, żeby raportować status
        # połączenia ('system_health') widoczny w panelu.
        self.history = HistoryStore(config.raw.get("dashboard", {}).get("db_path", "bridge_history.db"))
        notify_cfg = config.raw.get("notifications")
        # Wyłączenie z zakładki 🧩 Wtyczki (notifications.enabled=False) -
        # notify_cfg trafia jako None wszędzie indziej w tym pliku, co już
        # jest normalnie tolerowane (powiadomienia są od zawsze opcjonalne -
        # patrz każde `notify_cfg=notify_cfg` niżej, NotificationSender
        # samo nic nie robi bez ntfy_topic_url).
        if notify_cfg and not notify_cfg.get("enabled", True):
            notify_cfg = None
        self.alarms = AlarmEngine(
            config.raw.get("alarm_rules", []),
            self.history,
            notify_cfg=notify_cfg,
        )
        # Ile sekund nieprzerwanego rozłączenia, zanim wyślemy powiadomienie
        # push o utracie połączenia - chroni przed spamem przy chwilowych
        # zerwaniach WiFi. Konfigurowalne globalnie (notifications.grace_seconds).
        grace_seconds = (notify_cfg or {}).get("grace_seconds", 120)

        self.tuya = _build_tuya_controller(config.tuya)
        self.satel = SatelIntegraClient(
            config.satel["host"],
            config.satel["port"],
            config.satel["integration_key"],
            config.satel.get("partition", 1),
            monitored_zones=config.satel.get("monitored_zones"),
            monitored_outputs=config.satel.get("monitored_outputs"),
            virtual_inputs=config.satel.get("virtual_inputs", {}).get("inputs"),
            encryption_key=config.satel.get("encryption_key"),
            history=self.history,
            notify_cfg=notify_cfg,
            grace_seconds=grace_seconds,
            status_poll_interval_seconds=config.satel.get("status_poll_interval_seconds", 3),
        )
        # Tryb "http" wirtualnych wejść IP (satel.virtual_inputs.mode) - osobne
        # połączenie HTTP, niezależne od kolejki poleceń protokołu integracji
        # (patrz obszerny docstring bridge/satel_client.py). Konstruowany
        # BEZWARUNKOWO (jak wcześniej) - tani, nie otwiera żadnego połączenia
        # dopóki .trigger() nie zostanie wywołane - używany tylko gdy
        # _vi_mode == "http" (patrz _trigger_virtual_input/_set_target_state).
        self._vi_mode = config.satel.get("virtual_inputs", {}).get("mode", "library")
        self.satel_virtual_inputs = SatelVirtualInputTrigger(
            config.satel["host"], config.satel.get("virtual_inputs", {}),
        )
        self._active_timers: dict[str, asyncio.Task] = {}
        # Ostatnia znana wartość statusu per (source, nazwa urządzenia) -
        # potrzebne do wykrycia FAKTYCZNEJ zmiany w handle_device_state()
        # niżej (Tuya/HomeWhiz jako "wejście" reguły ruchu, obok Satela).
        # Bez tego pętla odpytywania Tuya (co kilkanaście sekund) wyzwalałaby
        # regułę PONOWNIE przy każdym obrocie, nawet gdy stan się nie zmienił.
        self._device_state_last_value: dict[tuple[str, str], str] = {}
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

        # Nazwa reguły ruchu -> jej indeks w config.motion_rules - potrzebne
        # do "then_trigger_rule" (patrz _chain_trigger niżej): reguła
        # odwołuje się do KOLEJNEJ reguły po nazwie (nie po indeksie, bo ten
        # zmienia się przy każdej edycji listy), więc trzymamy mapowanie
        # zbudowane raz i odświeżane przy reload_config().
        self._rule_name_index = self._build_rule_name_index()

        # Sceny - wspólny fundament dla harmonogramów, eskalacji i
        # automatyki kotła (patrz bridge/scenes.py)
        self.scenes = SceneRunner(
            config.raw.get("scenes", []), self.tuya, self.satel,
            satel_virtual_inputs=self.satel_virtual_inputs, virtual_input_mode=self._vi_mode,
            history=self.history,
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

    def _build_rule_name_index(self) -> dict[str, int]:
        return {
            rule["name"]: idx
            for idx, rule in enumerate(self.config.motion_rules)
            if rule.get("name")
        }

    def _rule_gate_open(self, rule: dict, now: datetime) -> bool:
        """Sprawdza time_window ORAZ warunek "condition" (and/or/not/state) -
        wspólne dla zwykłego uruchomienia reguły i dla then_trigger_rule
        (patrz _chain_trigger), żeby wyzwolona łańcuchowo reguła respektowała
        WŁASNE ograniczenia dokładnie tak samo, jak gdyby zadziałała wprost
        ze swojego triggera."""
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

    # ------------------------------------------------------------------
    # Ruch -> włączenie urządzenia Tuya na czas (LUB scena, LUB eskalacja
    # przy powtarzających się naruszeniach - patrz config.yaml)
    # ------------------------------------------------------------------
    async def handle_motion(self, source: str, zone_or_device_id, trigger: str = "violated") -> None:
        now = datetime.now()
        for idx, rule in enumerate(self.config.motion_rules):
            if rule["source"] != source:
                continue
            if source == "satel_zone" and rule.get("zone_number") != zone_or_device_id:
                continue
            # dla source == "tuya_pir" dopasowanie po device_id analogicznie

            if not rule.get("enabled", True):
                continue  # wyłączona w edytorze (przełącznik "Włączona")

            if not self._rule_matches_trigger(rule, trigger):
                continue

            if not self._rule_gate_open(rule, now):
                continue  # poza oknem czasowym albo warunek (condition) niespełniony

            await self._fire_motion_rule(rule, f"motion_rule_{idx}", source, zone_or_device_id, trigger)

    # ------------------------------------------------------------------
    # Stan urządzenia (Tuya/HomeWhiz) jako "wejście" reguły ruchu - obok
    # wejść Satela (handle_motion wyżej). Rule ma wtedy `source_device`
    # (nazwa urządzenia/urządzenia AGD) zamiast `zone_number` i
    # `trigger_value` (wartość statusu, przy której reguła ma zadziałać)
    # zamiast `trigger_on` (naruszenie/ustanie - to jest specyfika Satela,
    # binarna z definicji). Wołane z _tuya_status_poll_loop/
    # _on_homewhiz_state PO zapisaniu statusu (history.set_status) - ta
    # metoda TYLKO decyduje, czy któraś reguła ma na to zareagować.
    # ------------------------------------------------------------------
    async def handle_device_state(self, source: str, device_name: str, value: str) -> None:
        state_key = (source, device_name)
        old_value = self._device_state_last_value.get(state_key)
        self._device_state_last_value[state_key] = value
        if old_value == value:
            return  # bez faktycznej zmiany - nie wyzwalaj ponownie tej samej reguły co poprzedni odczyt

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
                # Wartość WŁAŚNIE zmieniła się Z wartości wyzwalającej na
                # inną - odpowiednik "ustania naruszenia" dla Satela, ale
                # TYLKO dla reguł w trybie "aż do ustania" (patrz
                # _is_until_restored) - zwykła reguła z czasem/scena/
                # "wyłącz urządzenie" reaguje WYŁĄCZNIE na osiągnięcie
                # trigger_value, nie na odejście od niego.
                trigger = "restored"
            else:
                continue

            if not self._rule_gate_open(rule, now):
                continue  # poza oknem czasowym albo warunek (condition) niespełniony

            await self._fire_motion_rule(rule, f"motion_rule_{idx}", source, device_name, trigger)

    @staticmethod
    def _is_until_restored(rule: dict) -> bool:
        """Czy ta reguła ma tryb "włącz aż do ustania naruszenia/wyłączenia
        wyzwalacza" - PUSTE ALBO ZEROWE `duration_seconds` ("Czas
        włączenia (s)" niewypełniony ALBO wpisane jawnie jako 0 w
        edytorze - obie wartości znaczą to samo, 0 jest tylko
        wygodniejszym sposobem wpisania tego samego w polu liczbowym), na
        akcji "włącz urządzenie" ALBO "aktywuj wejście IP" (nie scena, nie
        "wyłącz urządzenie" - te nie mają czasu trwania w ogóle)."""
        return (
            "scene" not in rule
            and rule.get("action") != "turn_off"
            and rule.get("duration_seconds") in (None, 0)
        )

    @staticmethod
    def _rule_matches_trigger(rule: dict, trigger: str) -> bool:
        """Czy reguła ma zareagować na TO konkretne wyzwolenie (naruszenie
        czy jego ustanie)?

        Domyślnie: `trigger_on` reguły (domyślnie "violated" - wsteczna
        kompatybilność ze starymi regułami bez tego pola) musi zgadzać się
        z aktualnym wyzwoleniem - pozwala np. wyłączyć światło dopiero PO
        ustaniu naruszenia czujki (akcja "turn_off"), zamiast tylko po
        stałym czasie.

        WYJĄTEK: reguła "Włącz urządzenie na czas" (nie scena, nie
        "turn_off") w trybie "aż do ustania naruszenia" (patrz
        _is_until_restored wyżej) reaguje na OBA wyzwolenia niezależnie od
        `trigger_on` - to jedna reguła obsługuje "włącz przy naruszeniu,
        wyłącz przy jego ustaniu" (patrz _fire_motion_rule niżej), więc musi
        zobaczyć zarówno "violated", jak i "restored"."""
        if RulesEngine._is_until_restored(rule):
            return True
        return rule.get("trigger_on", "violated") == trigger

    async def _fire_motion_rule(
        self, rule: dict, rule_key: str, source: str, zone_or_device_id,
        trigger: str = "violated", _chain_depth: int = 0,
    ) -> None:
        """Wykonuje akcję JEDNEJ reguły ruchu (scena/eskalacja/urządzenie
        docelowe) - wydzielone z handle_motion(), żeby ten sam kod obsługiwał
        zarówno zwykłe wyzwolenie (z pętli w handle_motion), jak i wyzwolenie
        łańcuchowe przez then_trigger_rule innej reguły (patrz
        _chain_trigger)."""
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
            # Aktywacja "wirtualnego wejścia IP" Satela jako akcja reguły
            # ruchu - to zwykłe WYJŚCIE Satela pod inną nazwą w UI (patrz
            # docstring bridge/satel_client.py), więc od tego miejsca leci
            # DOKŁADNIE tą samą wspólną ścieżką co akcja "wyjście Satela"/
            # urządzenie Tuya niżej (ten sam _set_target_state/
            # _run_on_for_duration, z REALNYM bool wł/wył przez
            # `set_output`) - wystarczy znormalizować nazwy pól.
            device = rule["virtual_input"]
            target_system = "satel_virtual_input"
        else:
            device = rule["target_device"]
            # target_system: "tuya" (domyślne, wsteczna kompatybilność ze
            # starymi regułami bez tego pola) albo "satel_output" - `device`
            # jest wtedy NAZWĄ wyjścia (klucz w satel.monitored_outputs), nie
            # urządzeniem Tuya. Patrz _set_target_state/_run_on_for_duration
            # niżej - to one rozróżniają system, reszta tej metody jest
            # wspólna dla obu.
            target_system = rule.get("target_system", "tuya")

        # jeśli timer już trwa dla tego urządzenia - zawsze go anuluj
        # (kolejne wyzwolenie reguły "włącz na czas" przedłuża włączenie
        # zamiast nakładać zadania; wyzwolenie "wyłącz urządzenie" po
        # prostu porzuca odliczanie, bo urządzenie i tak zaraz zgaśnie).
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
            # "Czas włączenia" niewypełniony ALBO 0 w edytorze - urządzenie
            # zostaje włączone AŻ DO USTANIA NARUSZENIA czujki, zamiast na
            # stały czas: włącz na "violated" (i wyzwolenia łańcuchowe/
            # eskalacyjne, które nie niosą prawdziwego "restored" - patrz
            # _chain_trigger), wyłącz na "restored". _rule_matches_trigger()
            # wyżej gwarantuje, że ta sama reguła zobaczy OBA wyzwolenia.
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
        """Włącza/wyłącza CEL akcji reguły ruchu - Tuya (domyślnie), wyjście
        Satela (target_system: "satel_output", `device` to wtedy NAZWA
        wyjścia, klucz w satel.monitored_outputs) albo "wirtualne wejście
        IP" Satela (target_system: "satel_virtual_input", `device` to NAZWA
        - patrz _trigger_virtual_input niżej, który obsługuje OBA tryby,
        "library" i "http"). Jeden wspólny punkt dla wszystkich trzech, z
        którego korzystają akcja "wyłącz urządzenie", tryb "aż do ustania
        naruszenia" i _run_on_for_duration niżej - żadne z nich nie musi
        osobno rozróżniać systemu.

        UWAGA tryb "http": funkcja "Wejścia IP" ETHM-1 Plus jest z definicji
        JEDNORAZOWYM impulsem (brak odpowiednika "przywróć" w tym
        protokole) - `on`/`off` są więc w tym trybie NIEROZRÓŻNIALNE, oba
        po prostu wywołują ten sam trigger() (jak dwukrotne naciśnięcie
        tego samego przycisku) - patrz _trigger_virtual_input."""
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
        """Włącza cel na czas `duration`, potem sam wyłącza - dla Tuya
        deleguje do tuya.turn_on_for_duration() (własna logika), dla
        wyjścia/wirtualnego wejścia Satela robi to samodzielnie (AsyncSatel
        nie ma odpowiednika: włącz -> śpij -> wyłącz). Owija to zapisem
        statusu "automation:<nazwa reguły>" (kategoria "automation") na czas
        trwania włączenia - dzięki temu inna reguła/scena może w warunku
        (state) sprawdzić "czy TA reguła jest właśnie aktywna" (patrz
        analogiczny mechanizm w SceneRunner.run(), bridge/scenes.py).
        Tylko dla NAZWANYCH reguł - bez nazwy nie ma pod jakim kluczem tego
        zapisać, a to i tak jedyny sposób, żeby ktokolwiek mógł się do niej
        odwołać."""
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

    # ------------------------------------------------------------------
    # then_trigger_rule: reguła po zadziałaniu może wyzwolić KOLEJNĄ regułę
    # ruchu po nazwie (np. "wejście do salonu" -> "wieczorna scena" ->
    # (jeśli po 22:00) "eskalacja - dodatkowe światło na korytarzu"),
    # zamiast dublować tę samą logikę pod dwoma osobnymi triggerami.
    # Limit głębokości chroni przed nieskończoną pętlą przy A -> B -> A.
    # ------------------------------------------------------------------
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
        """Wyzwala "wirtualne wejście IP" Satela po nazwie - JEDYNY punkt w
        RulesEngine, który zna oba tryby (satel.virtual_inputs.mode, patrz
        obszerny docstring bridge/satel_client.py):
        - "library" (domyślny): zwykłe wyjście Satela, set_output() przez
          już istniejące połączenie integracji.
        - "http": funkcja "Wejścia IP" ETHM-1 Plus, osobne połączenie HTTP
          (SatelVirtualInputTrigger) - JEDNORAZOWY impuls, `state` jest w
          tym trybie ignorowane (protokół nie ma odpowiednika "przywróć").

        Wspólny punkt zarówno dla _set_target_state (pełna ścieżka reguły
        ruchu z realnym wł/wył w trybie "library"), jak i dla miejsc, które
        NIE przez nią przechodzą (zmiana stanu światła Tuya, reguła kamery,
        ręczna aktywacja z panelu - patrz _execute_command)."""
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

    # ------------------------------------------------------------------
    # Zmiana stanu światła Tuya -> wirtualne wejście ETHM
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Ruch z kamery -> akcja w Satelu (LUB scena), z opcjonalnym oknem
    # czasowym i cooldownem (żeby migoczący ruch nie strzelał w kółko)
    # ------------------------------------------------------------------
    async def _on_homewhiz_state(self, appliance_name: str, state_name: str) -> None:
        """Callback HomeWhizClient (patrz start() wyżej) - publikuje
        zdekodowany stan zmywarki jako zwykły status (kategoria "homewhiz"),
        DOKŁADNIE jak stan kotła (kategoria "boiler") - żadnego nowego typu
        warunku nie trzeba dodawać, wystarczy istniejący warunek "state"
        reguły ruchu wskazujący klucz "homewhiz:<nazwa>" (patrz README).
        Dodatkowo - stan urządzenia może być bezpośrednio "wejściem" reguły
        ruchu (source: "homewhiz_state"), patrz handle_device_state()."""
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
            elif cmd["command"] == "satel_virtual_input":
                # Ręczna aktywacja z panelu głównego (osobna od automatyzacji -
                # patrz RulesEngine._fire_motion_rule, gałąź "virtual_input" in
                # rule) - ta sama _trigger_virtual_input, ale wywołana na
                # żądanie użytkownika zamiast przez regułę ruchu.
                name = cmd["params"]["name"]
                await self._trigger_virtual_input(name)
                self.history.mark_command_done(cmd["id"], {"message": f"Aktywowano wirtualne wejście '{name}'"})
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
        self._rule_name_index = self._build_rule_name_index()
        # Współrzędne do liczenia wschodu/zachodu słońca (bridge/scheduling.py,
        # okna czasowe "sunset"/"sunrise") - CZYSTE DANE bez żadnego żywego
        # połączenia do zerwania, więc (w odróżnieniu od hosta/portu/klucza
        # Satela) działa na żywo od razu, bez restartu procesu.
        self._location_cfg = new_config.raw.get("location")

        # Tuya (lokalne + chmurowe) - bezpieczne do podmiany w całości: ani
        # TuyaLocalController, ani TuyaCloudController nie trzymają żywego
        # połączenia (tylko krótkie żądania per komenda).
        self.tuya = _build_tuya_controller(new_config.tuya)

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
        # "Wirtualne wejścia IP" - oba tryby (mode: library/http, patrz
        # docstring bridge/satel_client.py) AKTUALIZOWANE na żywo, bez
        # restartu - tryb "http" idzie przez OSOBNY klient
        # (SatelVirtualInputTrigger, bez trwałego połączenia do zerwania,
        # więc bezpiecznie podmieniamy go w całości, w odróżnieniu od
        # self.satel wyżej).
        self.satel.virtual_inputs = new_config.satel.get("virtual_inputs", {}).get("inputs", {})
        self._vi_mode = new_config.satel.get("virtual_inputs", {}).get("mode", "library")
        self.satel_virtual_inputs = SatelVirtualInputTrigger(
            new_config.satel["host"], new_config.satel.get("virtual_inputs", {}),
        )
        # Interwał cyklicznego dopytywania o stan (patrz satel_client.py:
        # _periodic_status_sync) - W ODRÓŻNIENIU od host/portu/klucza,
        # DZIAŁA NA ŻYWO od razu, bez restartu procesu - pętla czyta ten
        # atrybut na bieżąco przy każdym obrocie, nie tylko raz na starcie.
        self.satel.status_poll_interval_seconds = new_config.satel.get("status_poll_interval_seconds", 3)

        # Sceny/automatyka kotła/alarmy progowe - budowane na bieżąco z
        # configu przy każdym wywołaniu, bezpieczne do pełnej podmiany.
        self.scenes = SceneRunner(
            new_config.raw.get("scenes", []), self.tuya, self.satel,
            satel_virtual_inputs=self.satel_virtual_inputs, virtual_input_mode=self._vi_mode,
            history=self.history,
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
        self.presence_simulation.location_cfg = self._location_cfg

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
        self._seed_homewhiz_status_labels()
        self._seed_automation_status_labels()

        logger.info("Konfiguracja przeładowana bez restartu procesu")
        self.history.write_event("system", detail={"message": "Konfiguracja przeładowana (bez restartu procesu)"})
        return {
            "message": "Konfiguracja przeładowana bez restartu procesu (czujniki eBUS, pola Satela, "
                       "urządzenia Tuya, automatyzacje, alarmy progowe). Zmiany połączeń (host/port/"
                       "klucze/brokery MQTT, zakładka 🔌 Połączenia) wciąż wymagają pełnego restartu.",
        }

    # ------------------------------------------------------------------
    # Cykliczne odpytywanie statusów Tuya (nie mają callbacków lokalnie -
    # trzeba je samemu pollingować; projekt nie jest już ograniczony do
    # RPi Zero W, patrz README "Status tego kodu")
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
                        await self.handle_device_state("tuya_state", device_name, "niedostępne")
                        continue
                    # DP '1' to konwencjonalnie stan on/off większości urządzeń Tuya
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
                    # .get(), NIE [] - device_name może być urządzeniem
                    # sterowanym przez Tuya Cloud (patrz
                    # bridge/tuya_dispatch.py: TuyaController.devices_cfg
                    # łączy lokalne i chmurowe), które nie ma wpisu w
                    # config.tuya["devices"] (tylko lokalne) - bez .get()
                    # KeyError tutaj przerywał resztę pętli for dla
                    # WSZYSTKICH pozostałych urządzeń w tym cyklu. Sterowanie
                    # chmurowe i tak nie obsługuje ściemniania (patrz
                    # TuyaCloudController), więc brak wpisu = po prostu
                    # pomiń blok jasności.
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
                # Ta pętla działa w tle przez cały czas życia procesu -
                # pojedynczy nieoczekiwany błąd nie może jej zabić, bo
                # straciłoby się cykliczne statusy Tuya na zawsze.
                logger.exception("Błąd w pętli odpytywania statusów Tuya")
            # Odczytywane NA ŚWIEŻO przy każdym obrocie (nie raz, na
            # starcie zadania) - żeby RulesEngine.reload_config() mogło
            # podmienić interwał na żywo, bez restartu procesu (patrz
            # analogiczne rozwiązanie dla Satela: SatelIntegraClient.
            # status_poll_interval_seconds).
            await asyncio.sleep(self.config.tuya.get("status_poll_interval_seconds", 15))

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
        input_keys = {f"satel_input:{number}" for number in self.satel.monitored_zones.values()}
        output_keys = {f"satel_output:{number}" for number in self.satel.monitored_outputs.values()}
        partition_keys = {
            f"satel_partition:{number}" for number in self.config.satel.get("partitions", {}).values()
        }
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

        # Sprzątanie wejść/wyjść/stref USUNIĘTYCH albo PRZEMIANOWANYCH
        # (zmiana numeru) w ⚙ Ustawienia od poprzedniego seedowania - patrz
        # HistoryStore.prune_status(). Inaczej strona główna pokazywałaby na
        # zawsze stare, martwe wpisy niezależne od aktualnej konfiguracji.
        self.history.prune_status("satel_input", input_keys)
        self.history.prune_status("satel_output", output_keys)
        self.history.prune_status("satel_partition", partition_keys)

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

        # Sprzątanie urządzeń USUNIĘTYCH/PRZEMIANOWANYCH w ⚙ Ustawienia od
        # poprzedniego seedowania - patrz analogiczny komentarz w
        # _seed_satel_status_labels()/HistoryStore.prune_status(). ":brightness"
        # to DODATKOWY klucz per ściemniacz zapisywany przez
        # _tuya_status_poll_loop() niżej (ta sama kategoria "tuya") - musi
        # zostać w zestawie "do zachowania", inaczej sprzątanie skasowałoby
        # żywy odczyt jasności zaraz po jego zapisaniu.
        tuya_keys = {f"tuya:{name}" for name in self.tuya.devices_cfg}
        tuya_keys |= {f"tuya:{name}:brightness" for name in self.tuya.devices_cfg}
        self.history.prune_status("tuya", tuya_keys)

    def _seed_homewhiz_status_labels(self) -> None:
        """Zapisuje 'nieznany' dla KAŻDEGO skonfigurowanego urządzenia
        HomeWhiz (zakładka 🧩 Wtyczki -> Zmywarki HomeWhiz), żeby klucz
        'homewhiz:<nazwa>' był od razu widoczny na stronie głównej i na
        liście warunków (state) w edytorze /automations, zanim urządzenie
        w ogóle się odezwie po MQTT - patrz analogiczny komentarz w
        _seed_satel_status_labels()/_seed_tuya_status_labels()."""
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
        """Zapisuje "bezczynna" dla każdej nazwanej sceny i (nazwanej)
        reguły ruchu z akcją "włącz urządzenie na czas", żeby klucz
        "automation:<nazwa>" był od razu widoczny na liście warunków (state)
        w edytorze /automations - patrz warunek "inna automatyzacja jest
        uruchomiona" i komentarz w SceneRunner.run()/_run_turn_on_for_duration
        wyżej. Sceny wywoływane wyłącznie jako `scene:` w regule/eskalacji
        (bez nazwy własnej) i akcje "wyłącz urządzenie"/"ustanie naruszenia"
        (natychmiastowe, bez okna trwania) celowo pominięte - nie mają
        sensownego stanu "uruchomiona" do zaznaczenia. Pomija już znane
        klucze - patrz analogiczny komentarz w _seed_satel_status_labels()."""
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

        # Sprzątanie scen/reguł USUNIĘTYCH/PRZEMIANOWANYCH od poprzedniego
        # seedowania - patrz analogiczny komentarz w
        # _seed_satel_status_labels()/HistoryStore.prune_status().
        self.history.prune_status("automation", valid_keys)

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
        self._seed_homewhiz_status_labels()
        self._seed_automation_status_labels()

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
        async def _on_zone_change(status: dict) -> None:
            # UWAGA: satel_integra >=1.0.0 woła ten callback BEZPOŚREDNIO
            # z `{numer: 0/1, ...}` (patrz AsyncSatel._zones_violated() w
            # satel_integra/satel_integra.py oraz obszerny docstring
            # bridge/satel_client.py) - w odróżnieniu od poprzedniej wersji
            # 0.3.x, która owijała to w `{"zones": {...}}`.
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
            # Analogicznie do _on_zone_change wyżej - `{numer: 0/1, ...}`
            # bezpośrednio.
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

        # Wszystkie długożyjące połączenia jako nadzorowane zadania w tle -
        # każde samo się podnosi po awarii (patrz bridge/resilience.py).
        # Awaria jednego komponentu (np. Satel chwilowo nieosiągalny) nie
        # blokuje ani nie zatrzymuje pozostałych.
        #
        # Satel/Tuya lokalnie/eBUS mają teraz też flagę "enabled" (zakładka
        # 🧩 Wtyczki, tak samo jak Tuya Cloud/Home Assistant/publikacja MQTT
        # od dawna) - obiekty (self.satel/self.tuya/self.ebus_listener)
        # ZAWSZE istnieją (konstrukcja w __init__ nie robi żadnego I/O,
        # więc reszta kodu, która się do nich odwołuje - sceny, reguły,
        # komendy z panelu - nie musi sprawdzać None), ale ich ŻYWA pętla
        # połączenia startuje TYLKO gdy włączone. Wyłączenie/włączenie
        # wymaga pełnego restartu procesu (restart_required: True) - patrz
        # PLUGIN_DEFS w dashboard/setup.py po uzasadnienie (bezpieczne
        # zamknięcie/odtworzenie żywych połączeń W TRAKCIE działania byłoby
        # dużo ryzykowniejszą przebudową niż warte tego dla rzadko
        # przełączanej flagi).
        if self.config.satel.get("enabled", True):
            asyncio.create_task(self.satel.run_forever())
        else:
            logger.info("Satel wyłączony w ustawieniach (🧩 Wtyczki) - pomijam nawiązanie połączenia")
        if self.config.tuya.get("enabled", True):
            asyncio.create_task(self._tuya_status_poll_loop())
        else:
            logger.info("Tuya lokalnie wyłączone w ustawieniach (🧩 Wtyczki) - pomijam odpytywanie")
        asyncio.create_task(self.presence_simulation.run_forever())
        asyncio.create_task(self._command_poll_loop())
        asyncio.create_task(self._maintenance_loop())

        # eBUS / kocioł (paho-mqtt ma wbudowany reconnect - patrz ebus_client.py)
        # Trzymane na self (nie jako lokalna zmienna) - reload_config()
        # potrzebuje do niej dostępu, żeby zaktualizować listę czujników
        # bez zrywania połączenia MQTT. self.ebus_listener zostaje None,
        # jeśli wyłączone - reszta kodu już to toleruje (patrz
        # reload_config: "if self.ebus_listener is not None").
        if self.config.ebus.get("enabled", True):
            self.ebus_listener = EbusMqttListener(
                self.config.ebus, self.handle_boiler_value, history=self.history,
                notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
            )
            self.ebus_listener.start()
        else:
            logger.info("eBUS wyłączony w ustawieniach (🧩 Wtyczki) - pomijam połączenie MQTT")

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

        # Kamera UniFi Protect (opcjonalnie, wyłączalna z 🧩 Wtyczki)
        if self.config.unifi_protect and self.config.unifi_protect.get("enabled", True):
            unifi_listener = UnifiProtectMotionListener(
                self.config.unifi_protect,
                lambda cam_id: self.handle_camera_motion("unifi_protect", cam_id),
                history=self.history,
                notify_cfg=self._notify_cfg,
                grace_seconds=self._grace_seconds,
            )
            asyncio.create_task(unifi_listener.run_forever())

        # Zmywarki/AGD HomeWhiz (opcjonalnie, wyłączalna z 🧩 Wtyczki) -
        # stan publikowany jako zwykły status (kategoria "homewhiz",
        # dokładnie jak "boiler" dla kotła) - włączenie/wyłączenie danej
        # automatyzacji od stanu zmywarki nie potrzebuje ŻADNEGO nowego
        # typu warunku, tylko istniejącego warunku "state" (patrz README:
        # "Warunek 'stan zmywarki HomeWhiz'").
        if self.config.homewhiz and self.config.homewhiz.get("enabled", True):
            homewhiz_client = HomeWhizClient(
                self.config.homewhiz, self._on_homewhiz_state,
                history=self.history, notify_cfg=self._notify_cfg, grace_seconds=self._grace_seconds,
            )
            asyncio.create_task(homewhiz_client.run_forever())

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
