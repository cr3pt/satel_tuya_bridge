"""Wczytywanie i walidacja konfiguracji.

Konfiguracja żyje w bazie SQLite (bridge/config_store.py: ConfigStore) jako
kilka nazwanych "dokumentów" (config/satel_fields/tuya_devices/automations/
sensors) - dokładnie ten sam podział, co wcześniej osobne pliki .yaml, tylko
inny nośnik (patrz docstring config_store.py po uzasadnienie: sekrety typu
Tuya `local_key` to dowolne bajty, JSON w SQLite nie ma niejawnego rzutowania
typów, na które podatny jest ręcznie edytowany YAML).

Stare instalacje (config.yaml + siostrzane pliki) są migrowane AUTOMATYCZNIE
i JEDNORAZOWO przy pierwszym `BridgeConfig.load()` wskazującym na jeszcze
nieistniejącą bazę - patrz `migrate_legacy_yaml_dir`.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

from .config_store import ConfigStore, migrate_legacy_yaml_dir, resolve_db_and_legacy_paths


_AUTOMATIONS_DEFAULTS = {
    "scenes": [],
    "reguły_ruchu": [],
    "presence_simulation": {"enabled": False},
}


@dataclass
class BridgeConfig:
    raw: dict[str, Any]
    # Żywy uchwyt do bazy konfiguracji - dashboard/app.py zapisuje przez
    # niego zmiany z panelu (store.set("satel_fields", ...) itd.), zamiast
    # bezpośrednio pisać pliki. RulesEngine.reload_config() (bridge/
    # rules_engine.py) używa `db_path` do ponownego wczytania.
    store: ConfigStore
    db_path: pathlib.Path

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "BridgeConfig":
        db_path, legacy_yaml_path = resolve_db_and_legacy_paths(path)

        if not db_path.exists() and not legacy_yaml_path.exists():
            raise FileNotFoundError(
                f"Nie znaleziono konfiguracji: {db_path} (ani starego {legacy_yaml_path}). "
                f"Uruchom kreator pierwszej konfiguracji (/setup) albo skopiuj config.example.yaml "
                f"jako {legacy_yaml_path.name}, uzupełnij i uruchom ponownie (patrz README)."
            )

        store = ConfigStore(db_path)
        if not store.has("config"):
            migrate_legacy_yaml_dir(store, legacy_yaml_path)
        # Jednorazowa migracja pojedynczego konta (dashboard.auth.*) do
        # tabeli users - no-op, jeśli jakiekolwiek konto już istnieje.
        store.migrate_single_admin_to_users()

        data = store.get("config")

        # "reguły_ruchu" jest wymagane jako klucz najwyższego poziomu - w
        # nowo generowanych configach (kreator) mieszka w dokumencie
        # "automations" razem ze scenami i symulacją obecności.
        automations = store.get("automations")
        for key, default in _AUTOMATIONS_DEFAULTS.items():
            if key in automations:
                data[key] = automations[key]
            else:
                data.setdefault(key, default)

        cls._validate(data)

        # Pola Satela (strefy/wyjścia/wejścia IP/partycje) - dokument
        # "satel_fields" NADPISUJE wartości wpisane wprost w "config", jeśli
        # faktycznie coś zawiera (tak samo jak automations wyżej). "satel"
        # jest już zwalidowane wyżej jako wymagany słownik.
        satel_fields = store.get("satel_fields")
        for key in ("monitored_zones", "monitored_outputs", "partitions"):
            if key in satel_fields:
                data["satel"][key] = satel_fields[key]
            else:
                data["satel"].setdefault(key, {})
        # Rodzaje wejść/wyjść (nazwa -> np. "drzwi"/"ruch"/"syrena") - CZYSTO
        # kosmetyczne, do ikon/etykiet w panelu (patrz dashboard/templates/
        # index.html i statusCardClass()). Osobne mapy (nie pole wewnątrz
        # monitored_zones/monitored_outputs), żeby NIE zmieniać kształtu
        # "nazwa -> numer" (int), na którym polega satel_client.py
        # (AsyncSatel(monitored_zones=list(...values()))) i rules_engine.py
        # (odwrotne mapowanie numer -> nazwa).
        for key in ("zone_types", "output_types"):
            data["satel"][key] = satel_fields.get(key, {})
        # "Wirtualne wejścia IP" - dwa tryby wyzwalania (satel.virtual_inputs.mode,
        # patrz obszerny docstring bridge/satel_client.py):
        # "library" (domyślny) - zwykłe wyjście Satela, set_output(), nazwa ->
        #   numer (int). "http" - funkcja "Wejścia IP" ETHM-1 Plus, osobne
        #   połączenie HTTP - nazwa -> numer (int, TEN SAM kształt co w
        #   trybie "library"). "key" to WSPÓLNE hasło CAŁEGO modułu ETHM do
        #   tej funkcji (ustawiane RAZ w DLOADX, NIE per wejście - wcześniejsza
        #   wersja błędnie wymagała osobnego klucza per wejście, zgłoszony
        #   błąd). "port"/"url_template"/"key" mają wartości domyślne
        #   (potrzebne tylko w trybie "http") - inaczej config bez jawnie
        #   zapisanej sekcji zostawiłby je nieobecne, co wywala silnik reguł
        #   (RulesEngine.__init__ zawsze tworzy SatelVirtualInputTrigger)
        #   KeyErrorem przy starcie/przeładowaniu.
        data["satel"].setdefault("virtual_inputs", {})
        data["satel"]["virtual_inputs"].setdefault("mode", "library")
        data["satel"]["virtual_inputs"].setdefault("port", 8888)
        data["satel"]["virtual_inputs"].setdefault("key", "")
        data["satel"]["virtual_inputs"].setdefault(
            "url_template", "http://{host}:{port}/input.cgi?key={key}&input={input_no}"
        )
        if "virtual_inputs" in satel_fields:
            data["satel"]["virtual_inputs"]["inputs"] = satel_fields["virtual_inputs"]
        else:
            data["satel"]["virtual_inputs"].setdefault("inputs", {})

        # Urządzenia Tuya - analogicznie, dokument "tuya_devices" nadpisuje
        # tuya.devices wpisane wprost w "config", jeśli istnieje.
        tuya_devices_data = store.get("tuya_devices")
        if "devices" in tuya_devices_data:
            data["tuya"]["devices"] = tuya_devices_data["devices"]
        else:
            data["tuya"].setdefault("devices", {})
        # Bramki Zigbee (Tuya) - urządzenia w tuya.devices mogą wskazywać na
        # nie przez pole "gateway" (patrz TuyaLocalController), zamiast
        # własnego ip/local_key. Osobna mapa (nie zagnieżdżona w devices),
        # analogicznie do zone_types/output_types wyżej.
        if "zigbee_gateways" in tuya_devices_data:
            data["tuya"]["zigbee_gateways"] = tuya_devices_data["zigbee_gateways"]
        else:
            data["tuya"].setdefault("zigbee_gateways", {})
        # Urządzenia sterowane WYŁĄCZNIE przez Tuya Cloud (REST) - typowo
        # zaimportowane z Tuya Cloud bez local_key/IP (np. urządzenia bez
        # dostępu do tej samej sieci LAN) - patrz TuyaCloudController i
        # RulesEngine._build_tuya_controller. Osobna mapa od "devices"
        # (lokalnych) - to samo urządzenie (po nazwie) w obu mapach naraz
        # oznacza "sterowane lokalnie, chmura jako fallback niedostępny,
        # bo lokalne ma pierwszeństwo" (patrz komentarz w rules_engine.py).
        if "cloud_devices" in tuya_devices_data:
            data["tuya"]["cloud_devices"] = tuya_devices_data["cloud_devices"]
        else:
            data["tuya"].setdefault("cloud_devices", {})

        sensors_content = store.get("sensors")
        data.setdefault("ebus", {})["sensors"] = sensors_content.get("sensors", [])

        return cls(raw=data, store=store, db_path=db_path)

    @staticmethod
    def _validate(data: dict[str, Any]) -> None:
        required_top = ["satel", "tuya", "reguły_ruchu", "ebus"]
        missing = [k for k in required_top if k not in data]
        if missing:
            raise ValueError(f"Brak wymaganych sekcji w konfiguracji: {missing}")

    # Wygodne skróty
    @property
    def satel(self) -> dict:
        return self.raw["satel"]

    @property
    def tuya(self) -> dict:
        return self.raw["tuya"]

    @property
    def motion_rules(self) -> list[dict]:
        return self.raw.get("reguły_ruchu", [])

    @property
    def light_to_satel_rules(self) -> list[dict]:
        return self.raw.get("reguły_swiatlo_do_satela", [])

    @property
    def camera_alarm_rules(self) -> list[dict]:
        return self.raw.get("reguły_kamera_alarm", [])

    @property
    def ebus(self) -> dict:
        return self.raw["ebus"]

    @property
    def boiler_output(self) -> dict:
        return self.raw.get("boiler_output", {"mode": "mqtt"})

    @property
    def unifi_protect(self) -> dict | None:
        return self.raw.get("unifi_protect")

    @property
    def homewhiz(self) -> dict | None:
        return self.raw.get("homewhiz")
