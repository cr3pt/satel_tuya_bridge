"""Kreator pierwszej konfiguracji - budowanie config.yaml/sensors.yaml
z danych wypełnionych w formularzu webowym (/setup).

Pokrywa PODSTAWOWĄ konfigurację potrzebną do uruchomienia bridge'a
(Satel, Tuya lokalnie, eBUS/MQTT, hasło panelu, powiadomienia,
opcjonalnie Home Assistant/UniFi Protect/Tuya cloud). Rzeczy bardziej
zaawansowane (sceny, harmonogramy, eskalacja, automatyka kotła, alarmy
progowe) dostają puste/domyślne wartości - dodaje się je później ręcznie
w config.yaml (plik zostaje w pełni edytowalny, to nie jest jednorazowy
generator) albo przez panel (/config - lista czujników eBUS).
"""
from __future__ import annotations

from werkzeug.security import generate_password_hash


def validate(form: dict) -> list[str]:
    """Zwraca listę komunikatów błędów (pusta lista = wszystko OK)."""
    errors = []

    satel = form.get("satel", {})
    if not satel.get("host"):
        errors.append("Satel: adres IP centrali/modułu ETHM jest wymagany")
    if not satel.get("integration_key"):
        errors.append("Satel: klucz integracji (ustawiony w DLOADX) jest wymagany")

    ebus = form.get("ebus", {})
    if not ebus.get("mqtt_host"):
        errors.append("eBUS: adres brokera MQTT jest wymagany")

    password = form.get("admin_password", "")
    if len(password) < 8:
        errors.append("Hasło panelu: minimum 8 znaków")

    for i, device in enumerate(form.get("tuya_devices", [])):
        if not device.get("name") or not device.get("id") or not device.get("ip") or not device.get("local_key"):
            errors.append(f"Urządzenie Tuya #{i + 1}: nazwa, ID, adres IP i local_key są wymagane")

    return errors


def build_config_dict(form: dict) -> dict:
    """Buduje pełny słownik config.yaml z danych formularza. Pola spoza
    formularza (sceny, alarmy progowe, itd.) dostają bezpieczne, puste
    wartości domyślne - użytkownik dopisze je później ręcznie, jeśli
    będzie chciał (patrz docstring modułu i README)."""
    satel_form = form.get("satel", {})
    tuya_devices = {
        d["name"]: {
            "id": d["id"], "ip": d["ip"], "local_key": d["local_key"],
            "version": float(d.get("version", 3.4)), "type": d.get("type", "socket"),
            **({"dimmer_dp": d["dimmer_dp"], "dimmer_range": [10, 1000]} if d.get("type") == "dimmer" else {}),
        }
        for d in form.get("tuya_devices", [])
        if d.get("name")
    }

    partitions = {p["name"]: int(p["number"]) for p in form.get("partitions", []) if p.get("name")}
    monitored_zones = {z["name"]: int(z["number"]) for z in form.get("monitored_zones", []) if z.get("name")}
    monitored_outputs = {o["name"]: int(o["number"]) for o in form.get("monitored_outputs", []) if o.get("name")}

    config = {
        "satel": {
            "host": satel_form["host"],
            "port": int(satel_form.get("port", 7094)),
            "integration_key": satel_form["integration_key"],
            "partition": int(satel_form.get("partition", 1)),
            "virtual_inputs": {
                "port": int(satel_form.get("virtual_inputs_port", 8888)),
                "url_template": "http://{host}:{port}/input.cgi?key={key}&input={input_no}",
                "inputs": {},
            },
            "outputs": {},
            "monitored_zones": monitored_zones,
            "monitored_outputs": monitored_outputs,
            "partitions": partitions or {"Dom": 1},
        },
        "tuya": {
            "status_poll_interval_seconds": 30,
            "devices": tuya_devices,
        },
        "reguły_ruchu": [],
        "reguły_swiatlo_do_satela": [],
        "reguły_kamera_alarm": [],
        "scenes": [],
        "alarm_rules": [],
        "boiler_rules": [],
        "presence_simulation": {"enabled": False},
        "ebus": {
            "mqtt_host": form["ebus"]["mqtt_host"],
            "mqtt_port": int(form["ebus"].get("mqtt_port", 1883)),
            "sensors_file": "sensors.yaml",
        },
        "boiler_output": {
            "mqtt": {"host": form["ebus"]["mqtt_host"], "port": int(form["ebus"].get("mqtt_port", 1883)),
                      "topic_prefix": "home/boiler"},
            "tuya_virtual_device": {"device_id": ""},
        },
        "dashboard": {
            "db_path": "bridge_history.db",
            "auth": {
                "password_hash": generate_password_hash(form["admin_password"]),
                "max_failed_attempts": 5,
                "lockout_seconds": 300,
            },
        },
        "maintenance": {
            "purge_enabled": True, "retention_days": 90,
            "backup_enabled": False, "backup_dir": "backups", "backup_keep_count": 7,
            "run_in_process": True, "interval_hours": 24,
        },
        "notifications": {
            "ntfy_topic_url": form.get("notifications", {}).get("ntfy_topic_url", ""),
            "grace_seconds": 120,
        },
    }

    if form.get("location", {}).get("latitude"):
        config["location"] = {
            "latitude": float(form["location"]["latitude"]),
            "longitude": float(form["location"]["longitude"]),
        }

    if form.get("home_assistant", {}).get("enabled"):
        config["home_assistant"] = {
            "enabled": True,
            "mqtt_host": form["home_assistant"].get("mqtt_host") or form["ebus"]["mqtt_host"],
            "mqtt_port": int(form["home_assistant"].get("mqtt_port", 1883)),
            "discovery_prefix": "homeassistant",
            "state_topic_prefix": "home/bridge",
            "device_name": "Satel-Tuya-eBUS Bridge",
        }

    if form.get("unifi_protect", {}).get("host"):
        config["unifi_protect"] = {
            "host": form["unifi_protect"]["host"],
            "username": form["unifi_protect"].get("username", ""),
            "password": form["unifi_protect"].get("password", ""),
            "verify_ssl": False,
        }

    if form.get("tuya_cloud", {}).get("access_id"):
        config["tuya"]["cloud"] = {
            "access_id": form["tuya_cloud"]["access_id"],
            "access_key": form["tuya_cloud"].get("access_key", ""),
            "api_endpoint": form["tuya_cloud"].get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": form["tuya_cloud"].get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
            "camera_device_ids": [],
        }

    return config


CONFIG_HEADER = (
    "# Wygenerowane przez kreator pierwszej konfiguracji (/setup).\n"
    "# Ten plik jest w pełni edytowalny ręcznie - kreator NIE URUCHOMI SIĘ\n"
    "# PONOWNIE, dopóki ten plik istnieje (żeby nie nadpisać Twoich zmian).\n"
    "# Pełny opis wszystkich pól, w tym tych pominiętych przez kreator\n"
    "# (sceny, harmonogramy, eskalacja, alarmy progowe) - patrz config.example.yaml.\n\n"
)

SENSORS_HEADER = (
    "# Lista czujników eBUS - dodaj je przez panel webowy (/config) po\n"
    "# uruchomieniu bridge'a, albo ręcznie - patrz sensors.example.yaml.\n\n"
)
