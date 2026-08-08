"""Wczytywanie i walidacja pliku konfiguracyjnego."""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class BridgeConfig:
    raw: dict[str, Any]
    # Ścieżka do pliku z listą czujników eBUS (patrz sensors.example.yaml) -
    # trzymana OSOBNO od config.yaml celowo: to jedyny plik, który
    # nadpisuje edytor w panelu webowym (dashboard/app.py, /config).
    # config.yaml zostaje w pełni ręcznie edytowalny, z komentarzami,
    # bez ryzyka, że zapis z UI je wykasuje.
    sensors_file: pathlib.Path = field(default=None)

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
        cls._validate(data)

        # Ścieżka do sensors.yaml - względna wobec katalogu config.yaml,
        # jeśli podana jako względna. Domyślnie "sensors.yaml" obok config.yaml.
        sensors_filename = data.get("ebus", {}).get("sensors_file", "sensors.yaml")
        sensors_path = pathlib.Path(sensors_filename)
        if not sensors_path.is_absolute():
            sensors_path = path.parent / sensors_path

        sensors = cls._load_sensors_file(sensors_path)
        data.setdefault("ebus", {})["sensors"] = sensors

        return cls(raw=data, sensors_file=sensors_path)

    @staticmethod
    def _load_sensors_file(sensors_path: pathlib.Path) -> list[dict]:
        if not sensors_path.exists():
            return []
        with open(sensors_path, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f) or {}
        return content.get("sensors", [])

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
