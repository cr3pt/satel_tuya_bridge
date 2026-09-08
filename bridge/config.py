from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

from .config_store import ConfigStore, migrate_legacy_yaml_dir, resolve_db_and_legacy_paths


_AUTOMATIONS_DEFAULTS = {
    "scenes": [],
    "reguły_ruchu": [],
    "presence_simulation": {"enabled": False},
    "flows": [],
}


@dataclass
class BridgeConfig:
    raw: dict[str, Any]
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
        store.migrate_single_admin_to_users()

        data = store.get("config")

        automations = store.get("automations")
        for key, default in _AUTOMATIONS_DEFAULTS.items():
            if key in automations:
                data[key] = automations[key]
            else:
                data.setdefault(key, default)

        cls._validate(data)

        satel_fields = store.get("satel_fields")
        for key in ("monitored_zones", "monitored_outputs", "partitions"):
            if key in satel_fields:
                data["satel"][key] = satel_fields[key]
            else:
                data["satel"].setdefault(key, {})
        for key in ("zone_types", "output_types"):
            data["satel"][key] = satel_fields.get(key, {})
        data["satel"].setdefault("virtual_inputs", {})
        data["satel"]["virtual_inputs"].setdefault("mode", "library")
        data["satel"]["virtual_inputs"].setdefault("port", 8888)
        if "virtual_inputs" in satel_fields:
            data["satel"]["virtual_inputs"]["inputs"] = satel_fields["virtual_inputs"]
        else:
            data["satel"]["virtual_inputs"].setdefault("inputs", {})

        tuya_devices_data = store.get("tuya_devices")
        if "devices" in tuya_devices_data:
            data["tuya"]["devices"] = tuya_devices_data["devices"]
        else:
            data["tuya"].setdefault("devices", {})
        if "zigbee_gateways" in tuya_devices_data:
            data["tuya"]["zigbee_gateways"] = tuya_devices_data["zigbee_gateways"]
        else:
            data["tuya"].setdefault("zigbee_gateways", {})
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
    def flows(self) -> list[dict]:
        return self.raw.get("flows", [])

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
