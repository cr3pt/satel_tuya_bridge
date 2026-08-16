"""Wczytywanie i walidacja pliku konfiguracyjnego."""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


_AUTOMATIONS_DEFAULTS = {
    "scenes": [],
    "reguły_ruchu": [],
    "presence_simulation": {"enabled": False},
}


def _resolve_path(config_dir: pathlib.Path, filename: str) -> pathlib.Path:
    p = pathlib.Path(filename)
    return p if p.is_absolute() else config_dir / p


def _load_yaml_file(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class BridgeConfig:
    raw: dict[str, Any]
    # Ścieżka do pliku z listą czujników eBUS (patrz sensors.example.yaml) -
    # trzymana OSOBNO od config.yaml celowo: to jedyny plik, który
    # nadpisuje edytor w panelu webowym (dashboard/app.py, /config).
    # config.yaml zostaje w pełni ręcznie edytowalny, z komentarzami,
    # bez ryzyka, że zapis z UI je wykasuje.
    sensors_file: pathlib.Path = field(default=None)
    # Analogicznie - sceny/reguły ruchu (harmonogram+eskalacja)/symulacja
    # obecności trzymane OSOBNO, żeby panel /automations mógł je edytować
    # bezpiecznie (patrz dashboard/app.py). Plik opcjonalny: jeśli nie
    # istnieje, wartości brane są wprost z config.yaml (wstecznie zgodne
    # ze starszymi configami, gdzie te sekcje były wpisane bezpośrednio).
    automations_file: pathlib.Path = field(default=None)
    # Tak samo - "pola" Satela (strefy/wyjścia/wejścia IP do monitorowania,
    # partycje) i urządzenia Tuya, edytowalne z panelu /config bez ryzyka
    # nadpisania reszty config.yaml. Połączenie (host/port/klucz/cloud)
    # zostaje inline w config.yaml - to tylko NAZWANE POLA.
    satel_fields_file: pathlib.Path = field(default=None)
    tuya_devices_file: pathlib.Path = field(default=None)

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "BridgeConfig":
        path = pathlib.Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Nie znaleziono pliku konfiguracyjnego: {path}. "
                f"Skopiuj config.example.yaml jako config.yaml i uzupełnij dane."
            )
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Wczytanie automations.yaml MUSI nastąpić PRZED _validate() -
        # "reguły_ruchu" jest wymagane jako klucz najwyższego poziomu, a w
        # nowo generowanych configach (kreator) już go tam nie ma wprost,
        # tylko w automations.yaml (albo dostaje domyślną pustą wartość
        # poniżej, jeśli nie ma go NIGDZIE).
        automations_path = _resolve_path(path.parent, data.get("automations_file", "automations.yaml"))
        automations = _load_yaml_file(automations_path)
        for key, default in _AUTOMATIONS_DEFAULTS.items():
            if key in automations:
                data[key] = automations[key]
            else:
                data.setdefault(key, default)

        cls._validate(data)

        # Pola Satela (strefy/wyjścia/wejścia IP/partycje) - jeśli
        # satel_fields.yaml istnieje, NADPISUJE wartości wpisane wprost w
        # config.yaml (tak samo jak automations.yaml). "satel" jest już
        # zwalidowane wyżej jako wymagany słownik.
        satel_fields_path = _resolve_path(path.parent, data["satel"].get("fields_file", "satel_fields.yaml"))
        satel_fields = _load_yaml_file(satel_fields_path)
        for key in ("monitored_zones", "monitored_outputs", "partitions"):
            if key in satel_fields:
                data["satel"][key] = satel_fields[key]
            else:
                data["satel"].setdefault(key, {})
        data["satel"].setdefault("virtual_inputs", {})
        if "virtual_inputs" in satel_fields:
            data["satel"]["virtual_inputs"]["inputs"] = satel_fields["virtual_inputs"]
        else:
            data["satel"]["virtual_inputs"].setdefault("inputs", {})

        # Urządzenia Tuya - analogicznie, tuya_devices.yaml nadpisuje
        # tuya.devices wpisane wprost w config.yaml, jeśli istnieje.
        tuya_devices_path = _resolve_path(path.parent, data["tuya"].get("devices_file", "tuya_devices.yaml"))
        tuya_devices_data = _load_yaml_file(tuya_devices_path)
        if "devices" in tuya_devices_data:
            data["tuya"]["devices"] = tuya_devices_data["devices"]
        else:
            data["tuya"].setdefault("devices", {})

        # Ścieżka do sensors.yaml - względna wobec katalogu config.yaml,
        # jeśli podana jako względna. Domyślnie "sensors.yaml" obok config.yaml.
        sensors_path = _resolve_path(path.parent, data.get("ebus", {}).get("sensors_file", "sensors.yaml"))
        sensors_content = _load_yaml_file(sensors_path)
        data.setdefault("ebus", {})["sensors"] = sensors_content.get("sensors", [])

        return cls(
            raw=data, sensors_file=sensors_path, automations_file=automations_path,
            satel_fields_file=satel_fields_path, tuya_devices_file=tuya_devices_path,
        )

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
