"""Kreator pierwszej konfiguracji - budowanie config.yaml i jego towarzyszy
(sensors.yaml/automations.yaml/satel_fields.yaml/tuya_devices.yaml) z danych
wypełnionych w formularzu webowym (/setup).

Wieloetapowy kreator (/setup): każdy krok to osobna "strona" w
templates/setup.html, przechodzona przyciskiem "Dalej". Po każdym takim
kliknięciu cały dotychczasowy stan formularza trafia do pliku roboczego
(patrz save_draft/load_draft niżej) - dzięki temu konfigurację da się
przerwać i wznowić później (np. inna sesja przeglądarki), zanim finalny
config.yaml w ogóle powstanie.

Pokrywa PODSTAWOWĄ konfigurację potrzebną do uruchomienia bridge'a (Satel,
Tuya lokalnie, eBUS/MQTT, hasło panelu, powiadomienia, opcjonalnie Home
Assistant/UniFi Protect/Tuya cloud) ORAZ bardziej zaawansowane rzeczy
zbudowane na wspólnym fundamencie scen (bridge/scenes.py): sceny,
harmonogramy czasowe (time_window) i eskalację przy powtarzających się
naruszeniach (obie ostatnie jako pola reguł ruchu).

Cztery rodzaje "pól" - czujniki eBUS, sceny/reguły ruchu/symulacja
obecności, strefy/wyjścia/wejścia IP/partycje Satela, urządzenia Tuya -
trafiają do OSOBNYCH plików (odpowiednio sensors.yaml/automations.yaml/
satel_fields.yaml/tuya_devices.yaml), NIE do config.yaml: to jedyne pliki,
które UI (kreator i panele /config, /automations - patrz dashboard/app.py)
wolno mu bezpiecznie nadpisywać w całości, bez ryzyka skasowania ręcznych
zmian i komentarzy w config.yaml. Po pierwszej konfiguracji wszystkie
cztery da się w pełni edytować z powrotem w przeglądarce, w zabezpieczonych
hasłem panelach - żadne z nich nie wymagają ręcznej edycji YAML-a (choć to
wciąż działa, patrz bridge/config.py - wsteczna zgodność ze starszymi
configami).

Rzeczy, których panel WCIĄŻ nie obsługuje (reguły_kamera_alarm,
reguły_swiatlo_do_satela, boiler_rules, alarm_rules) dostają puste/domyślne
wartości w config.yaml - dodaje się je później ręcznie (plik zostaje w
pełni edytowalny, to nie jest jednorazowy generator).
"""
from __future__ import annotations

import copy
import json
import pathlib

from werkzeug.security import generate_password_hash

DRAFT_FILENAME = ".setup_draft.json"

# Typy akcji scen obsługiwane przez SceneRunner (bridge/scenes.py) i pola,
# które KAŻDA z nich wymaga - używane do walidacji formularza kreatora.
SCENE_ACTION_REQUIRED_FIELDS = {
    "tuya_on": ["device"],
    "tuya_off": ["device"],
    "tuya_on_for_duration": ["device", "duration_seconds"],
    "satel_output": ["output_number"],
    "satel_virtual_input": ["virtual_input"],
    "satel_arm": [],
    "satel_disarm": [],
    "delay": ["seconds"],
}


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

    username = (form.get("admin_username") or "").strip()
    if not username:
        errors.append("Nazwa użytkownika administratora jest wymagana")

    password = form.get("admin_password", "")
    if len(password) < 8:
        errors.append("Hasło panelu: minimum 8 znaków")

    errors.extend(validate_satel_fields(form))
    errors.extend(validate_tuya_devices(form))
    errors.extend(validate_automations(form))

    return errors


def validate_satel_fields(form: dict) -> list[str]:
    """Waliduje TYLKO "pola" Satela (wirtualne wejścia IP - jedyne z nich
    wymagające kompletu danych, żeby scenom/regułom było do czego się
    odwołać). Strefy/wyjścia/partycje nie mają twardej walidacji - niepełny
    wiersz (brakująca nazwa albo numer) jest po prostu pomijany przy
    zapisie (patrz _build_monitored_zones/_build_monitored_outputs/
    _build_partitions). Współdzielone przez kreator i panel /config."""
    errors = []
    for i, vi in enumerate(form.get("virtual_inputs", [])):
        if not vi.get("name") or not vi.get("number") or not vi.get("key"):
            errors.append(f"Wirtualne wejście #{i + 1}: nazwa, numer i klucz z DLOADX są wymagane")
    return errors


def validate_tuya_devices(form: dict) -> list[str]:
    """Waliduje urządzenia Tuya ORAZ bramki Zigbee - współdzielone przez
    kreator i panel /config (patrz validate_satel_fields wyżej - ten sam
    wzorzec). Urządzenie WSKAZUJĄCE bramkę (pole "gateway" - podurządzenie
    Zigbee) nie ma własnego ip/local_key (tinytuya kieruje ruch przez
    połączenie bramki, patrz bridge/tuya_local.py) - wymaga za to, żeby
    wskazana bramka faktycznie istniała wśród zdefiniowanych niżej."""
    errors = []
    gateway_names = {
        g["name"].strip() for g in form.get("zigbee_gateways", [])
        if g.get("name") and g.get("id") and g.get("ip") and g.get("local_key")
    }
    for i, device in enumerate(form.get("tuya_devices", [])):
        label = f"Urządzenie Tuya #{i + 1}"
        gateway_name = (device.get("gateway") or "").strip()
        if gateway_name:
            if not device.get("name") or not device.get("id"):
                errors.append(f"{label} (podurządzenie Zigbee): nazwa i ID są wymagane")
            if gateway_name not in gateway_names:
                errors.append(
                    f"{label}: wskazana bramka Zigbee '{gateway_name}' nie istnieje wśród "
                    "zdefiniowanych bramek (sprawdź nazwę albo uzupełnij bramkę niżej)"
                )
        elif not device.get("name") or not device.get("id") or not device.get("ip") or not device.get("local_key"):
            errors.append(f"{label}: nazwa, ID, adres IP i local_key są wymagane")

    for i, gateway in enumerate(form.get("zigbee_gateways", [])):
        if not gateway.get("name") or not gateway.get("id") or not gateway.get("ip") or not gateway.get("local_key"):
            errors.append(f"Bramka Zigbee #{i + 1}: nazwa, ID, adres IP i local_key są wymagane")
    return errors


def validate_automations(form: dict) -> list[str]:
    """Waliduje TYLKO część "automatyzacje" formularza (scenes/motion_rules/
    presence_simulation) - używane zarówno przez kreator (`validate()`
    wyżej), jak i samodzielnie przez panel /automations (dashboard/app.py),
    który nie ma (i nie potrzebuje) reszty pól kreatora."""
    scene_names = {s.get("name", "").strip() for s in form.get("scenes", []) if s.get("name", "").strip()}
    errors = []
    errors.extend(_validate_scenes(form.get("scenes", [])))
    errors.extend(_validate_motion_rules(form.get("motion_rules", []), scene_names))
    errors.extend(_validate_presence_simulation(form.get("presence_simulation", {})))
    return errors


def _validate_scenes(scenes: list[dict]) -> list[str]:
    errors = []
    seen_names = set()
    for i, scene in enumerate(scenes):
        label = f"Scena #{i + 1}"
        name = (scene.get("name") or "").strip()
        if not name:
            errors.append(f"{label}: nazwa jest wymagana")
        elif name in seen_names:
            errors.append(f"{label}: nazwa '{name}' powtarza się - nazwy scen muszą być unikalne")
        else:
            seen_names.add(name)

        actions = scene.get("actions", [])
        if not actions:
            errors.append(f"{label} ('{name or '?'}'): musi mieć co najmniej jedną akcję")
        for j, action in enumerate(actions):
            action_label = f"{label} ('{name or '?'}'), akcja #{j + 1}"
            action_type = action.get("type")
            if action_type not in SCENE_ACTION_REQUIRED_FIELDS:
                errors.append(f"{action_label}: nieznany typ akcji '{action_type}'")
                continue
            for field in SCENE_ACTION_REQUIRED_FIELDS[action_type]:
                if action.get(field) in (None, ""):
                    errors.append(f"{action_label} ({action_type}): pole '{field}' jest wymagane")
    return errors


def _validate_motion_rules(rules: list[dict], scene_names: set[str]) -> list[str]:
    errors = []
    for i, rule in enumerate(rules):
        label = f"Reguła ruchu #{i + 1}"
        if not rule.get("zone_number"):
            errors.append(f"{label}: numer wejścia Satela jest wymagany")

        uses_scene = bool((rule.get("scene") or "").strip())
        if uses_scene:
            scene_name = rule["scene"].strip()
            if scene_name not in scene_names:
                errors.append(f"{label}: wybrana scena '{scene_name}' nie istnieje wśród zdefiniowanych scen")
        else:
            if not rule.get("target_device"):
                errors.append(f"{label}: wybierz urządzenie docelowe albo scenę")
            if not rule.get("duration_seconds"):
                errors.append(f"{label}: czas włączenia (sekundy) jest wymagany")

        window = rule.get("time_window") or {}
        if window.get("enabled"):
            if not window.get("start") or not window.get("end"):
                errors.append(f"{label}: harmonogram - podaj początek i koniec okna")

        escalation = rule.get("escalation") or {}
        if escalation.get("enabled"):
            if not escalation.get("threshold_count"):
                errors.append(f"{label}: eskalacja - liczba naruszeń jest wymagana")
            if not escalation.get("window_seconds"):
                errors.append(f"{label}: eskalacja - okno czasowe (sekundy) jest wymagane")
            escalated_scene = (escalation.get("escalated_scene") or "").strip()
            if not escalated_scene:
                errors.append(f"{label}: eskalacja - wybierz scenę wywoływaną po przekroczeniu progu")
            elif escalated_scene not in scene_names:
                errors.append(f"{label}: eskalacja - scena '{escalated_scene}' nie istnieje wśród zdefiniowanych scen")
    return errors


def _validate_presence_simulation(ps: dict) -> list[str]:
    errors = []
    if not ps or not ps.get("enabled"):
        return errors

    if not [d for d in ps.get("devices", []) if d]:
        errors.append("Symulacja obecności: wybierz co najmniej jedno urządzenie Tuya")

    window = ps.get("window") or {}
    if window.get("enabled") and not (window.get("start") and window.get("end")):
        errors.append("Symulacja obecności: harmonogram - podaj początek i koniec okna")

    lo, hi = ps.get("min_interval_minutes"), ps.get("max_interval_minutes")
    if lo and hi:
        try:
            if int(lo) > int(hi):
                errors.append("Symulacja obecności: minimalny odstęp nie może być większy niż maksymalny")
        except (TypeError, ValueError):
            errors.append("Symulacja obecności: odstępy muszą być liczbami")
    return errors


def _build_scene_action(action: dict) -> dict:
    action_type = action["type"]
    built = {"type": action_type}
    if action_type in ("tuya_on", "tuya_off", "tuya_on_for_duration"):
        built["device"] = action["device"]
        if action_type != "tuya_off" and action.get("dimmer_level_percent") not in (None, ""):
            built["dimmer_level_percent"] = int(action["dimmer_level_percent"])
        if action_type == "tuya_on_for_duration":
            built["duration_seconds"] = int(action["duration_seconds"])
    elif action_type == "satel_output":
        built["output_number"] = int(action["output_number"])
        built["state"] = bool(action.get("state", True))
    elif action_type == "satel_virtual_input":
        built["virtual_input"] = action["virtual_input"]
    elif action_type == "satel_arm":
        if action.get("mode") not in (None, ""):
            built["mode"] = int(action["mode"])
        if action.get("partitions"):
            built["partitions"] = [int(p) for p in action["partitions"]]
    elif action_type == "satel_disarm":
        # Opcjonalne - patrz komentarz w bridge/scenes.py: gdy pominięte,
        # dotyka domyślnej (jedynej) strefy skonfigurowanej dla instalacji.
        if action.get("partitions"):
            built["partitions"] = [int(p) for p in action["partitions"]]
    elif action_type == "delay":
        built["seconds"] = float(action["seconds"])
    return built


def _build_scenes(scenes_form: list[dict]) -> list[dict]:
    return [
        {
            "name": scene["name"].strip(),
            "actions": [_build_scene_action(a) for a in scene.get("actions", [])],
        }
        for scene in scenes_form
        if (scene.get("name") or "").strip()
    ]


def _build_motion_rules(rules_form: list[dict]) -> list[dict]:
    rules = []
    for rule in rules_form:
        if not rule.get("zone_number"):
            continue
        built = {"source": "satel_zone", "zone_number": int(rule["zone_number"])}

        scene_name = (rule.get("scene") or "").strip()
        if scene_name:
            built["scene"] = scene_name
        else:
            built["target_device"] = rule["target_device"]
            built["duration_seconds"] = int(rule["duration_seconds"])
            built["dimmer_level_percent"] = (
                int(rule["dimmer_level_percent"]) if rule.get("dimmer_level_percent") not in (None, "") else None
            )

        window = rule.get("time_window") or {}
        if window.get("enabled") and window.get("start") and window.get("end"):
            built["time_window"] = {"start": window["start"].strip(), "end": window["end"].strip()}

        escalation = rule.get("escalation") or {}
        if escalation.get("enabled") and escalation.get("escalated_scene"):
            built["escalation"] = {
                "threshold_count": int(escalation["threshold_count"]),
                "window_seconds": int(escalation["window_seconds"]),
                "escalated_scene": escalation["escalated_scene"].strip(),
            }

        rules.append(built)
    return rules


def _build_presence_simulation(form: dict) -> dict:
    ps = form.get("presence_simulation") or {}
    if not ps.get("enabled"):
        return {"enabled": False}

    built = {
        "enabled": True,
        "devices": [d for d in ps.get("devices", []) if d],
        "min_interval_minutes": int(ps.get("min_interval_minutes") or 10),
        "max_interval_minutes": int(ps.get("max_interval_minutes") or 40),
    }
    window = ps.get("window") or {}
    if window.get("enabled") and window.get("start") and window.get("end"):
        built["window"] = {"start": window["start"].strip(), "end": window["end"].strip()}
    return built


def _build_partitions(form: dict) -> dict:
    partitions = {p["name"]: int(p["number"]) for p in form.get("partitions", []) if p.get("name") and p.get("number")}
    return partitions or {"Dom": 1}


def _build_monitored_zones(form: dict) -> dict:
    return {z["name"]: int(z["number"]) for z in form.get("monitored_zones", []) if z.get("name") and z.get("number")}


def _build_monitored_outputs(form: dict) -> dict:
    return {o["name"]: int(o["number"]) for o in form.get("monitored_outputs", []) if o.get("name") and o.get("number")}


# Rodzaje wejść (czujek) i wyjść Satela - CZYSTO opisowe (ikona/etykieta w
# panelu, patrz index.html), nie wpływają na logikę bridge'a. "inne"/brak
# wyboru = zachowanie jak dotychczas (generyczna czujka/wyjście).
SATEL_ZONE_TYPES = {
    "drzwi": "Drzwi", "okno": "Okno", "ruch": "Czujka ruchu (PIR)",
    "dym": "Czujka dymu", "zalanie": "Czujka zalania", "wibracja": "Czujka wibracji/stłuczenia",
    "sabotaz": "Sabotaż/24h", "inne": "Inne",
}
SATEL_OUTPUT_TYPES = {
    "syrena": "Syrena", "przekaznik": "Przekaźnik/gniazdko", "oswietlenie": "Oświetlenie",
    "brama": "Brama/rolety", "ogrzewanie": "Ogrzewanie", "inne": "Inne",
}


def _build_zone_types(form: dict) -> dict:
    return {
        z["name"]: z["type"] for z in form.get("monitored_zones", [])
        if z.get("name") and z.get("number") and z.get("type") in SATEL_ZONE_TYPES
    }


def _build_output_types(form: dict) -> dict:
    return {
        o["name"]: o["type"] for o in form.get("monitored_outputs", [])
        if o.get("name") and o.get("number") and o.get("type") in SATEL_OUTPUT_TYPES
    }


def _build_virtual_inputs(form: dict) -> dict:
    return {
        vi["name"]: {"number": int(vi["number"]), "key": vi["key"]}
        for vi in form.get("virtual_inputs", [])
        if vi.get("name")
    }


def _build_tuya_devices(form: dict) -> dict:
    devices = {}
    for d in form.get("tuya_devices", []):
        if not d.get("name"):
            continue
        gateway_name = (d.get("gateway") or "").strip()
        if gateway_name:
            # Podurządzenie Zigbee bramki - BEZ własnego ip/local_key/version,
            # ruch idzie przez połączenie bramki (patrz TuyaLocalController w
            # bridge/tuya_local.py). cid domyślnie = id, jeśli puste.
            entry = {"id": d["id"], "gateway": gateway_name, "type": d.get("type", "socket")}
            if d.get("cid"):
                entry["cid"] = d["cid"]
        else:
            entry = {
                "id": d["id"], "ip": d["ip"], "local_key": d["local_key"],
                "version": float(d.get("version", 3.4)), "type": d.get("type", "socket"),
            }
        if d.get("type") == "dimmer":
            entry["dimmer_dp"] = d["dimmer_dp"]
            entry["dimmer_range"] = [10, 1000]
        devices[d["name"]] = entry
    return devices


def _build_zigbee_gateways(form: dict) -> dict:
    return {
        g["name"]: {
            "id": g["id"], "ip": g["ip"], "local_key": g["local_key"],
            "version": float(g.get("version", 3.4)),
        }
        for g in form.get("zigbee_gateways", [])
        if g.get("name") and g.get("id") and g.get("ip") and g.get("local_key")
    }


def validate_connections(form: dict) -> list[str]:
    """Waliduje formularz zakładki "Połączenia" panelu /config (host/klucze/
    tematy MQTT już skonfigurowanej instalacji) - współdzielony wzorzec z
    validate() (kreator), ale bez wymogu hasła (to ustawia się tylko raz,
    w kreatorze, i panel /config go nie zmienia)."""
    errors = []
    satel = form.get("satel", {})
    if not satel.get("host"):
        errors.append("Satel: adres IP centrali/modułu ETHM jest wymagany")

    ebus = form.get("ebus", {})
    if not ebus.get("mqtt_host"):
        errors.append("eBUS: adres brokera MQTT jest wymagany")

    tuya_cloud = form.get("tuya_cloud", {})
    if tuya_cloud.get("enabled") and not tuya_cloud.get("access_id"):
        errors.append("Tuya Cloud: Access ID jest wymagany, jeśli integracja włączona")

    ha = form.get("home_assistant", {})
    if ha.get("enabled") and not (ha.get("mqtt_host") or ebus.get("mqtt_host")):
        errors.append("Home Assistant: adres brokera MQTT jest wymagany, jeśli integracja włączona")

    # Interwały odpytywania (zakładka 🧩 Wtyczki) - minimum 3s, żeby nie
    # zalewać centrali/urządzeń zapytaniami przy pomyłce we wpisanej wartości.
    satel_interval = satel.get("status_poll_interval_seconds")
    if satel_interval not in (None, ""):
        try:
            if int(satel_interval) < 3:
                errors.append("Satel: interwał odpytywania musi wynosić co najmniej 3 sekundy")
        except (TypeError, ValueError):
            errors.append("Satel: interwał odpytywania musi być liczbą")

    tuya_local_interval = form.get("tuya_local", {}).get("status_poll_interval_seconds")
    if tuya_local_interval not in (None, ""):
        try:
            if int(tuya_local_interval) < 3:
                errors.append("Tuya (lokalnie): interwał odpytywania musi wynosić co najmniej 3 sekundy")
        except (TypeError, ValueError):
            errors.append("Tuya (lokalnie): interwał odpytywania musi być liczbą")

    return errors


def build_connections_patch(form: dict, existing: dict) -> dict:
    """Buduje NOWY słownik config.yaml na bazie JUŻ ISTNIEJĄCEGO (`existing`)
    - nadpisuje TYLKO pola połączeń (host/port/klucze/tematy MQTT per
    integracja), zostawiając nietknięte wszystkie inne sekcje
    (reguły_kamera_alarm, boiler_rules, alarm_rules, maintenance,
    dashboard.auth, itd.). W przeciwieństwie do build_config_dict()
    (kreator, buduje config.yaml OD ZERA) - to jedyny bezpieczny sposób na
    "rekonfigurację" już działającej instalacji bez ryzyka skasowania
    ręcznych zmian w config.yaml (stąd /setup zostaje zablokowany po
    pierwszym zapisie - patrz CONFIG_HEADER i api_setup_save).

    Pola-sekrety (klucz integracji Satela, Tuya Cloud Access Secret): pusta
    wartość w formularzu = ZACHOWAJ dotychczasową (formularz w przeglądarce
    nigdy nie dostaje prawdziwego sekretu z powrotem - patrz
    connections_to_form - więc "nic nie wpisano" nie może oznaczać
    "wyczyść")."""
    config = copy.deepcopy(existing)
    satel_form = form.get("satel", {})
    ebus_form = form.get("ebus", {})

    config.setdefault("satel", {})
    config["satel"]["host"] = satel_form.get("host", "").strip()
    config["satel"]["port"] = int(satel_form.get("port") or config["satel"].get("port") or 7094)
    config["satel"]["partition"] = int(satel_form.get("partition") or config["satel"].get("partition") or 1)
    if (satel_form.get("integration_key") or "").strip():
        config["satel"]["integration_key"] = satel_form["integration_key"].strip()
    else:
        config["satel"].setdefault("integration_key", "")
    config["satel"].setdefault("virtual_inputs", {})
    config["satel"]["virtual_inputs"]["port"] = int(
        satel_form.get("virtual_inputs_port") or config["satel"]["virtual_inputs"].get("port") or 8888
    )
    config["satel"]["virtual_inputs"].setdefault(
        "url_template", "http://{host}:{port}/input.cgi?key={key}&input={input_no}"
    )
    config["satel"].setdefault("outputs", {})
    config["satel"].setdefault("fields_file", "satel_fields.yaml")
    config["satel"]["status_poll_interval_seconds"] = int(
        satel_form.get("status_poll_interval_seconds")
        or config["satel"].get("status_poll_interval_seconds") or 15
    )

    config.setdefault("ebus", {})
    config["ebus"]["mqtt_host"] = ebus_form.get("mqtt_host", "").strip()
    config["ebus"]["mqtt_port"] = int(ebus_form.get("mqtt_port") or config["ebus"].get("mqtt_port") or 1883)
    config["ebus"].setdefault("sensors_file", "sensors.yaml")

    config.setdefault("boiler_output", {})
    config["boiler_output"].setdefault("mqtt", {})
    config["boiler_output"]["mqtt"]["host"] = config["ebus"]["mqtt_host"]
    config["boiler_output"]["mqtt"]["port"] = config["ebus"]["mqtt_port"]
    config["boiler_output"]["mqtt"]["topic_prefix"] = (
        (ebus_form.get("topic_prefix") or "").strip()
        or config["boiler_output"]["mqtt"].get("topic_prefix", "home/boiler")
    )
    config["boiler_output"].setdefault("tuya_virtual_device", {"device_id": ""})

    mqtt_publish_form = form.get("mqtt_publish", {})
    config["mqtt_publish"] = {
        "enabled": bool(mqtt_publish_form.get("enabled", True)),
        "topics": {
            "satel": (mqtt_publish_form.get("satel_topic") or "satel").strip(),
            "tuya": (mqtt_publish_form.get("tuya_topic") or "tuya").strip(),
        },
    }

    config.setdefault("tuya", {})
    tuya_cloud_form = form.get("tuya_cloud", {})
    if tuya_cloud_form.get("enabled") and (tuya_cloud_form.get("access_id") or "").strip():
        existing_cloud = config["tuya"].get("cloud", {})
        config["tuya"]["cloud"] = {
            "access_id": tuya_cloud_form["access_id"].strip(),
            "access_key": (tuya_cloud_form.get("access_key") or "").strip() or existing_cloud.get("access_key", ""),
            "api_endpoint": (tuya_cloud_form.get("api_endpoint") or "").strip()
                or existing_cloud.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": (tuya_cloud_form.get("mq_endpoint") or "").strip()
                or existing_cloud.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
            "camera_device_ids": existing_cloud.get("camera_device_ids", []),
        }
    else:
        config["tuya"].pop("cloud", None)
    tuya_local_form = form.get("tuya_local", {})
    config["tuya"]["status_poll_interval_seconds"] = int(
        tuya_local_form.get("status_poll_interval_seconds")
        or config["tuya"].get("status_poll_interval_seconds") or 30
    )
    config["tuya"].setdefault("devices_file", "tuya_devices.yaml")

    ha_form = form.get("home_assistant", {})
    existing_ha = config.get("home_assistant", {})
    if ha_form.get("enabled"):
        config["home_assistant"] = {
            "enabled": True,
            "mqtt_host": (ha_form.get("mqtt_host") or "").strip() or config["ebus"]["mqtt_host"],
            "mqtt_port": int(ha_form.get("mqtt_port") or existing_ha.get("mqtt_port") or 1883),
            "discovery_prefix": existing_ha.get("discovery_prefix", "homeassistant"),
            "state_topic_prefix": existing_ha.get("state_topic_prefix", "home/bridge"),
            "device_name": existing_ha.get("device_name", "Satel-Tuya-eBUS Bridge"),
        }
    else:
        config["home_assistant"] = {**existing_ha, "enabled": False}

    notif_form = form.get("notifications", {})
    config.setdefault("notifications", {})
    config["notifications"]["ntfy_topic_url"] = (
        notif_form.get("ntfy_topic_url", "") or ""
    ).strip() or config["notifications"].get("ntfy_topic_url", "")
    config["notifications"].setdefault("grace_seconds", 120)

    return config


def connections_to_form(existing: dict) -> dict:
    """Odwrotność build_connections_patch - konwertuje AKTUALNY config.yaml
    na kształt formularza zakładki "Połączenia" (GET /api/config/connections).
    Sekretne pola (klucz integracji Satela, Tuya Cloud Access Secret) NIE są
    tu NIGDY zwracane w jawnej postaci - tylko flaga `*_set`, czy są w ogóle
    ustawione, żeby nie wyciekały przez GET do każdego w sieci LAN (w
    odróżnieniu od np. local_key urządzeń Tuya w /api/config/tuya-devices -
    tam ryzyko jest dużo mniejsze niż tu, bo klucz integracji Satela pozwala
    rozbroić fizyczny system alarmowy)."""
    satel = existing.get("satel", {})
    tuya = existing.get("tuya", {})
    ebus = existing.get("ebus", {})
    tuya_cloud = tuya.get("cloud", {})
    ha = existing.get("home_assistant", {})
    mqtt_publish = existing.get("mqtt_publish", {})
    boiler_mqtt = existing.get("boiler_output", {}).get("mqtt", {})
    return {
        "satel": {
            "host": satel.get("host", ""),
            "port": satel.get("port", 7094),
            "partition": satel.get("partition", 1),
            "virtual_inputs_port": satel.get("virtual_inputs", {}).get("port", 8888),
            "integration_key_set": bool(satel.get("integration_key")),
            "status_poll_interval_seconds": satel.get("status_poll_interval_seconds", 15),
        },
        "tuya_local": {
            "status_poll_interval_seconds": tuya.get("status_poll_interval_seconds", 30),
        },
        "ebus": {
            "mqtt_host": ebus.get("mqtt_host", ""),
            "mqtt_port": ebus.get("mqtt_port", 1883),
            "topic_prefix": boiler_mqtt.get("topic_prefix", "home/boiler"),
        },
        "mqtt_publish": {
            "enabled": mqtt_publish.get("enabled", True),
            "satel_topic": mqtt_publish.get("topics", {}).get("satel", "satel"),
            "tuya_topic": mqtt_publish.get("topics", {}).get("tuya", "tuya"),
        },
        "tuya_cloud": {
            "enabled": bool(tuya_cloud),
            "access_id": tuya_cloud.get("access_id", ""),
            "access_key_set": bool(tuya_cloud.get("access_key")),
            "api_endpoint": tuya_cloud.get("api_endpoint", "https://openapi.tuyaeu.com"),
            "mq_endpoint": tuya_cloud.get("mq_endpoint", "wss://mqe.tuyaeu.com:8285/"),
        },
        "home_assistant": {
            "enabled": bool(ha.get("enabled")),
            "mqtt_host": ha.get("mqtt_host", ""),
            "mqtt_port": ha.get("mqtt_port", 1883),
        },
        "notifications": {
            "ntfy_topic_url": existing.get("notifications", {}).get("ntfy_topic_url", ""),
        },
    }


# Integracje pokazywane na zakładce 🧩 Wtyczki - jedno spójne miejsce z
# przeglądem WSZYSTKICH integracji (dotychczas rozrzucone po zakładkach
# ⚙ Ustawienia/🔌 Połączenia/🎬 Automatyzacje). "toggleable" = ma osobną,
# zapisaną flagę enabled, którą da się przełączyć wprost stąd (POST
# /api/plugins) - reszta jest włączona/wyłączona PRZEZ SAMĄ OBECNOŚĆ
# konfiguracji (np. dodanie choć jednego urządzenia Tuya) i wciąż wymaga
# przejścia do właściwej zakładki, żeby to zmienić - to NIE jest w pełni
# dynamiczny system wtyczek (start/stop w locie bez restartu) - taka
# architektura wymagałaby dużo większej, ryzykowniejszej przebudowy
# cyklu życia żywych połączeń sieciowych (Satel TCP, klienci MQTT, Tuya
# Cloud Pulsar), a przełączanie integracji jest i tak rzadką operacją.
PLUGIN_DEFS = [
    {"id": "satel", "name": "Centrala Satel Integra", "icon": "🔒", "toggleable": True, "restart_required": True,
     "has_poll_interval": True, "has_log_level": True,
     "description": "Odczyt wejść/wyjść/stref i sterowanie (uzbrajanie, wyjścia)."},
    {"id": "tuya_local", "name": "Tuya - urządzenia lokalne (WiFi)", "icon": "🔌", "toggleable": True, "restart_required": True,
     "has_poll_interval": True, "has_log_level": True,
     "description": "Sterowanie gniazdkami/przełącznikami/ściemniaczami Tuya bezpośrednio w LAN, bez chmury (tinytuya) - obejmuje też podurządzenia Zigbee."},
    {"id": "tuya_zigbee", "name": "Tuya - bramki Zigbee", "icon": "🐝", "toggleable": False, "restart_required": False,
     "has_log_level": True,
     "description": "Czujniki/przełączniki Zigbee podpięte pod bramkę Tuya (Tuya Zigbee Gateway/Hub) - dzieli połączenie z 'Tuya lokalnie' wyżej, brak osobnego przełącznika."},
    {"id": "tuya_cloud", "name": "Tuya Cloud (kamery + import urządzeń)", "icon": "☁️", "toggleable": True, "restart_required": True,
     "has_log_level": True,
     "description": "Detekcja ruchu z kamer Tuya oraz import urządzeń z konta Tuya Cloud - wymaga projektu na developer.tuya.com."},
    {"id": "ebus", "name": "eBUS / kocioł (dane grzewcze)", "icon": "🔥", "toggleable": True, "restart_required": True,
     "has_log_level": True,
     "description": "Odczyt czujników kotła (ebusd) przez MQTT - historia, alarmy progowe, automatyka."},
    {"id": "home_assistant", "name": "Home Assistant (MQTT Discovery)", "icon": "🏠", "toggleable": True, "restart_required": True,
     "has_log_level": True,
     "description": "Automatyczne udostępnienie encji (wejścia/wyjścia Satela, urządzenia Tuya, czujniki kotła) w Home Assistant."},
    {"id": "mqtt_publish", "name": "Publikacja stanu do MQTT", "icon": "📡", "toggleable": True, "restart_required": True,
     "has_log_level": True,
     "description": "Stan Satela/Tuya na osobnym temacie MQTT, niezależnie od Home Assistant."},
    {"id": "unifi_protect", "name": "Kamery UniFi Protect", "icon": "📹", "toggleable": True, "restart_required": True,
     "has_log_level": True,
     "description": "Detekcja ruchu z kamer UniFi Protect jako alternatywa/uzupełnienie kamer Tuya."},
    {"id": "presence_simulation", "name": "Symulacja obecności", "icon": "🎭", "toggleable": True, "restart_required": False,
     "has_log_level": True,
     "description": "Losowe włączanie/wyłączanie wskazanych urządzeń Tuya pod nieobecność domowników."},
    {"id": "notifications", "name": "Powiadomienia push (ntfy)", "icon": "🔔", "toggleable": True, "restart_required": True,
     "has_log_level": True,
     "description": "Push przy utracie/przywróceniu połączenia z centralą/MQTT/Tuya/kamerami oraz przy alarmach progowych."},
]


def list_plugins(config_raw: dict) -> list[dict]:
    """Buduje listę integracji (zakładka 🧩 Wtyczki) na bazie AKTUALNEGO
    config.raw - każdy wpis mówi, czy jest włączony/skonfigurowany i czy da
    się to przełączyć wprost stąd (patrz PLUGIN_DEFS wyżej)."""
    satel = config_raw.get("satel", {})
    tuya = config_raw.get("tuya", {})
    ebus = config_raw.get("ebus", {})
    ha = config_raw.get("home_assistant", {})
    mqtt_publish = config_raw.get("mqtt_publish", {})
    presence = config_raw.get("presence_simulation", {})
    notifications = config_raw.get("notifications", {})

    tuya_devices = tuya.get("devices", {}) or {}
    zigbee_devices = {n: d for n, d in tuya_devices.items() if d.get("gateway")}
    zigbee_gateways = tuya.get("zigbee_gateways", {}) or {}
    ebus_sensors = config_raw.get("ebus", {}).get("sensors", []) or []

    unifi_protect_cfg = config_raw.get("unifi_protect") or {}

    state = {
        "satel": {
            "enabled": satel.get("enabled", True) and bool(satel.get("host") and satel.get("integration_key")),
            "configured": bool(satel.get("host") and satel.get("integration_key")),
            "detail": f"{satel.get('host', '?')}:{satel.get('port', '?')}" if satel.get("host") else "nieskonfigurowana",
            "poll_interval_seconds": satel.get("status_poll_interval_seconds", 15),
        },
        "tuya_local": {
            "enabled": tuya.get("enabled", True) and len(tuya_devices) > len(zigbee_devices),
            "configured": len(tuya_devices) > len(zigbee_devices),
            "detail": f"{len(tuya_devices) - len(zigbee_devices)} urządzeń WiFi",
            "poll_interval_seconds": tuya.get("status_poll_interval_seconds", 30),
        },
        "tuya_zigbee": {
            "enabled": tuya.get("enabled", True) and bool(zigbee_gateways),
            "configured": bool(zigbee_gateways),
            "detail": f"{len(zigbee_gateways)} bramek, {len(zigbee_devices)} urządzeń",
        },
        "tuya_cloud": {
            "enabled": tuya.get("cloud", {}).get("enabled", True) and bool(tuya.get("cloud", {}).get("access_id")),
            "configured": bool(tuya.get("cloud", {}).get("access_id")),
            "detail": "skonfigurowana" if tuya.get("cloud", {}).get("access_id") else "brak poświadczeń",
        },
        "ebus": {
            "enabled": ebus.get("enabled", True) and bool(ebus.get("mqtt_host")),
            "configured": bool(ebus.get("mqtt_host")),
            "detail": f"{len(ebus_sensors)} czujników" if ebus.get("mqtt_host") else "brak brokera MQTT",
        },
        "home_assistant": {
            "enabled": bool(ha.get("enabled")),
            "configured": bool(ha.get("mqtt_host") or ebus.get("mqtt_host")),
            "detail": ha.get("mqtt_host") or ebus.get("mqtt_host") or "brak brokera MQTT",
        },
        "mqtt_publish": {
            "enabled": bool(mqtt_publish.get("enabled", True)) and bool(ebus.get("mqtt_host")),
            "configured": bool(ebus.get("mqtt_host")),
            "detail": "używa brokera eBUS" if ebus.get("mqtt_host") else "brak brokera MQTT",
        },
        "unifi_protect": {
            "enabled": unifi_protect_cfg.get("enabled", True) and config_raw.get("unifi_protect") is not None,
            "configured": config_raw.get("unifi_protect") is not None,
            "detail": "skonfigurowane" if config_raw.get("unifi_protect") is not None else "nieskonfigurowane",
        },
        "presence_simulation": {
            "enabled": bool(presence.get("enabled")),
            "configured": bool(presence.get("devices")),
            "detail": f"{len(presence.get('devices', []))} urządzeń" if presence.get("devices") else "brak urządzeń",
        },
        "notifications": {
            "enabled": notifications.get("enabled", True) and bool(notifications.get("ntfy_topic_url")),
            "configured": bool(notifications.get("ntfy_topic_url")),
            "detail": "skonfigurowane" if notifications.get("ntfy_topic_url") else "nieskonfigurowane",
        },
    }

    from bridge.logging_config import plugin_config_section

    return [
        {
            "has_poll_interval": False, **plugin_def, **state[plugin_def["id"]],
            "log_level": plugin_config_section(config_raw, plugin_def["id"]).get("log_level") or "",
        }
        for plugin_def in PLUGIN_DEFS
    ]


def dict_to_name_number_rows(d: dict) -> list[dict]:
    return [{"name": name, "number": number} for name, number in d.items()]


def zones_to_rows(numbers: dict, types: dict) -> list[dict]:
    """Jak dict_to_name_number_rows, ale dokłada opcjonalny "type" (rodzaj
    czujki/wyjścia - patrz SATEL_ZONE_TYPES/SATEL_OUTPUT_TYPES niżej) do
    edycji w panelu i wyświetlania ikon na stronie głównej."""
    return [{"name": name, "number": number, "type": types.get(name, "")} for name, number in numbers.items()]


def virtual_inputs_to_rows(inputs: dict) -> list[dict]:
    return [{"name": name, "number": v.get("number"), "key": v.get("key")} for name, v in inputs.items()]


def tuya_devices_to_rows(devices: dict) -> list[dict]:
    return [
        {
            "name": name, "id": cfg.get("id", ""), "ip": cfg.get("ip", ""),
            "local_key": cfg.get("local_key", ""), "type": cfg.get("type", "socket"),
            "dimmer_dp": cfg.get("dimmer_dp", ""),
            # Podurządzenie Zigbee bramki (patrz TuyaLocalController) - puste
            # dla zwykłych urządzeń WiFi, które mają własne ip/local_key wyżej.
            "gateway": cfg.get("gateway", ""), "cid": cfg.get("cid", ""),
        }
        for name, cfg in devices.items()
    ]


def zigbee_gateways_to_rows(gateways: dict) -> list[dict]:
    return [
        {
            "name": name, "id": cfg.get("id", ""), "ip": cfg.get("ip", ""),
            "local_key": cfg.get("local_key", ""), "version": cfg.get("version", 3.4),
        }
        for name, cfg in gateways.items()
    ]


def build_satel_fields_dict(form: dict) -> dict:
    """Buduje zawartość satel_fields.yaml (strefy/wyjścia/wejścia IP/
    partycje) - używane zarówno przez kreator (razem z build_config_dict),
    jak i samodzielnie przez panel /config przy każdym zapisie."""
    return {
        "monitored_zones": _build_monitored_zones(form),
        "monitored_outputs": _build_monitored_outputs(form),
        "partitions": _build_partitions(form),
        "virtual_inputs": _build_virtual_inputs(form),
        "zone_types": _build_zone_types(form),
        "output_types": _build_output_types(form),
    }


def build_tuya_devices_dict(form: dict) -> dict:
    """Buduje zawartość tuya_devices.yaml - analogicznie do
    build_satel_fields_dict wyżej. "zigbee_gateways" to bramki Zigbee Tuya,
    na które mogą wskazywać poszczególne "devices" (pole "gateway") zamiast
    mieć własne ip/local_key - patrz TuyaLocalController."""
    return {"devices": _build_tuya_devices(form), "zigbee_gateways": _build_zigbee_gateways(form)}


def clean_sensors_payload(payload) -> tuple[list | None, str | None]:
    """Waliduje i normalizuje listę czujników eBUS przesłaną z panelu
    (`/api/config/sensors` w dashboard/app.py i `/api/plugins/ebus/sensors`
    w plugins/ebus/routes.py - ten sam kształt danych, więc jedna wspólna
    funkcja). Walidacja jest celowo minimalna (tylko to, co faktycznie
    zepsułoby resztę bridge'a przy starcie) - reszta pól ma sensowne
    wartości domyślne. Zwraca (cleaned, None) albo (None, komunikat_błędu)."""
    if not isinstance(payload, list):
        return None, "Oczekiwano listy czujników w ciele żądania"

    cleaned = []
    seen_names = set()
    for i, item in enumerate(payload):
        name = (item.get("name") or "").strip()
        mqtt_topic = (item.get("mqtt_topic") or "").strip()
        if not name or not mqtt_topic:
            return None, f"Wiersz {i + 1}: pola 'name' i 'mqtt_topic' są wymagane"
        if name in seen_names:
            return None, f"Zduplikowana nazwa czujnika: '{name}'"
        seen_names.add(name)

        targets = item.get("targets") or {}
        cleaned.append({
            "name": name,
            "label": (item.get("label") or name).strip(),
            "mqtt_topic": mqtt_topic,
            "targets": {
                "history": bool(targets.get("history", True)),
                "mqtt_publish": bool(targets.get("mqtt_publish", False)),
                "tuya_virtual_device": bool(targets.get("tuya_virtual_device", False)),
                "tuya_dp": (targets.get("tuya_dp") or "").strip(),
            },
        })
    return cleaned, None


def build_automations_dict(form: dict) -> dict:
    """Buduje zawartość automations.yaml (sceny/reguły ruchu/symulacja
    obecności) - używane zarówno przez kreator (razem z build_config_dict),
    jak i samodzielnie przez panel /automations przy każdym zapisie."""
    return {
        "scenes": _build_scenes(form.get("scenes", [])),
        "reguły_ruchu": _build_motion_rules(form.get("motion_rules", [])),
        "presence_simulation": _build_presence_simulation(form),
    }


def build_config_dict(form: dict) -> dict:
    """Buduje pełny słownik config.yaml z danych formularza. Pola spoza
    formularza (alarmy progowe, reguły kamer, automatyka kotła itd.) dostają
    bezpieczne, puste wartości domyślne - użytkownik dopisze je później
    ręcznie, jeśli będzie chciał (patrz docstring modułu i README)."""
    satel_form = form.get("satel", {})

    config = {
        "satel": {
            "host": satel_form["host"],
            "port": int(satel_form.get("port", 7094)),
            "integration_key": satel_form["integration_key"],
            "partition": int(satel_form.get("partition", 1)),
            "virtual_inputs": {
                "port": int(satel_form.get("virtual_inputs_port", 8888)),
                "url_template": "http://{host}:{port}/input.cgi?key={key}&input={input_no}",
            },
            "outputs": {},
            # Strefy/wyjścia/wejścia IP/partycje NIE są tutaj - trafiają do
            # osobnego satel_fields.yaml (patrz build_satel_fields_dict),
            # które zapisuje api_setup_save() zaraz po tym pliku.
            # "fields_file" to jedyny ślad tego w config.yaml - analogicznie
            # do ebus.sensors_file dla sensors.yaml.
            "fields_file": "satel_fields.yaml",
        },
        "tuya": {
            "status_poll_interval_seconds": 30,
            # Urządzenia Tuya NIE są tutaj - trafiają do osobnego
            # tuya_devices.yaml (patrz build_tuya_devices_dict), analogicznie
            # do satel.fields_file wyżej.
            "devices_file": "tuya_devices.yaml",
        },
        # Sceny/reguły ruchu/symulacja obecności NIE są tutaj - trafiają do
        # osobnego automations.yaml (patrz build_automations_dict), które
        # zapisuje api_setup_save() zaraz po tym pliku. "automations_file"
        # to jedyny ślad tego w config.yaml - analogicznie do
        # ebus.sensors_file dla sensors.yaml.
        "automations_file": "automations.yaml",
        "reguły_swiatlo_do_satela": [],
        "reguły_kamera_alarm": [],
        "alarm_rules": [],
        "boiler_rules": [],
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
                "username": (form.get("admin_username") or "admin").strip() or "admin",
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
            # `or` (nie .get(key, default)) celowo - kreator wysyła ""
            # (nie brak klucza) dla regionów, dla których nie zgadujemy
            # nieudokumentowanego adresu Pulsar (patrz setup.html:
            # TUYA_REGION_ENDPOINTS) - pusty string ma spaść do domyślnego
            # (UE), nie zostać zapisany jako pusty adres.
            "api_endpoint": form["tuya_cloud"].get("api_endpoint") or "https://openapi.tuyaeu.com",
            "mq_endpoint": form["tuya_cloud"].get("mq_endpoint") or "wss://mqe.tuyaeu.com:8285/",
            "camera_device_ids": [],
        }

    return config


def draft_path_for(config_path: pathlib.Path) -> pathlib.Path:
    """Ścieżka do pliku roboczego kreatora - obok docelowego config.yaml,
    ale z kropką na początku (ukryty, nigdy nie mylony z prawdziwą
    konfiguracją)."""
    return config_path.parent / DRAFT_FILENAME


def load_draft(path: pathlib.Path) -> dict:
    """Wczytuje zapisany wcześniej stan formularza. Zwraca pusty słownik,
    jeśli pliku nie ma albo jest uszkodzony (np. przerwany zapis) - kreator
    ma wtedy po prostu zacząć od kroku 1, zamiast się wywalić."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_draft(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Zapis przez plik tymczasowy + atomowa podmiana - żeby przerwany zapis
    # (np. utrata zasilania RPi) nie zostawił uszkodzonego/pustego pliku
    # roboczego, przez który load_draft zgubiłoby dotychczasowy postęp.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def delete_draft(path: pathlib.Path) -> None:
    path.unlink(missing_ok=True)


CONFIG_HEADER = (
    "# Wygenerowane przez kreator pierwszej konfiguracji (/setup).\n"
    "# Ten plik jest w pełni edytowalny ręcznie - kreator NIE URUCHOMI SIĘ\n"
    "# PONOWNIE, dopóki ten plik istnieje (żeby nie nadpisać Twoich zmian).\n"
    "# Strefy/wyjścia/wejścia IP/partycje Satela są w satel_fields.yaml,\n"
    "# urządzenia Tuya w tuya_devices.yaml, sceny/reguły ruchu/symulacja\n"
    "# obecności w automations.yaml - wszystkie edytowalne z panelu webowego\n"
    "# (/config, /automations - wymaga hasła administratora do zapisu).\n"
    "# Pełny opis wszystkich pól, w tym tych pominiętych przez kreator\n"
    "# (reguły_kamera_alarm, alarmy progowe, automatyka kotła) - patrz\n"
    "# config.example.yaml.\n\n"
)

SENSORS_HEADER = (
    "# Lista czujników eBUS - dodaj je przez panel webowy (/config) po\n"
    "# uruchomieniu bridge'a, albo ręcznie - patrz sensors.example.yaml.\n\n"
)

AUTOMATIONS_HEADER = (
    "# Sceny / reguły ruchu (harmonogram + eskalacja) / symulacja obecności.\n"
    "# W pełni edytowalne z panelu webowego (/automations, wymaga hasła\n"
    "# administratora) - każdy zapis stamtąd NADPISUJE cały ten plik, więc\n"
    "# ręczne zmiany tutaj przetrwają tylko do następnego zapisu z panelu.\n"
    "# Opis pól - patrz config.example.yaml (sekcje scenes/reguły_ruchu/\n"
    "# presence_simulation) i README, sekcja \"Automatyzacje\".\n\n"
)

SATEL_FIELDS_HEADER = (
    "# Strefy / wyjścia / wejścia IP / partycje Satela - w pełni edytowalne\n"
    "# z panelu webowego (/config, wymaga hasła administratora do zapisu) -\n"
    "# każdy zapis stamtąd NADPISUJE cały ten plik. Połączenie z centralą\n"
    "# (host/port/klucz integracji) zostaje w config.yaml.\n"
    "# Opis pól - patrz config.example.yaml (sekcja satel).\n\n"
)

TUYA_DEVICES_HEADER = (
    "# Urządzenia Tuya (lokalnie) - w pełni edytowalne z panelu webowego\n"
    "# (/config, wymaga hasła administratora do zapisu) - każdy zapis\n"
    "# stamtąd NADPISUJE cały ten plik. Konto/chmura Tuya (jeśli używana)\n"
    "# zostaje w config.yaml (sekcja tuya.cloud).\n"
    "# Opis pól - patrz config.example.yaml (sekcja tuya.devices).\n\n"
)
