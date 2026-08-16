"""Panel webowy - podgląd historii i zdarzeń z bridge'a.

Celowo lekki: Flask + wbudowany serwer WSGI (waitress) - żadna z tych
bibliotek nie ma problemu z architekturą ARMv6 RPi Zero W, w przeciwieństwie
do Grafany/InfluxDB (Go, brak binarek dla armv6). Cały ciężki render
wykresów robi przeglądarka (Chart.js) - RPi tylko serwuje JSON z SQLite.

Jeśli config.yaml jeszcze nie istnieje, panel automatycznie przekierowuje
na /setup (kreator pierwszej konfiguracji) - patrz dashboard/setup.py.

Uruchomienie (osobny proces od głównego bridge'a - czyta ten sam plik
SQLite, więc nie trzeba go integrować z pętlą asyncio):
    python -m dashboard.app --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import time

import yaml
from flask import Flask, jsonify, redirect, render_template, request, url_for

from bridge.config import BridgeConfig
from bridge.ebus_client import discover_topics as discover_ebus_topics
from bridge.history_store import HistoryStore
from bridge.satel_client import test_connection as test_satel_connection
from bridge.tuya_local import scan_network as scan_tuya_network, fetch_cloud_devices as fetch_tuya_cloud_devices
from dashboard.auth import AuthGuard, require_auth
from dashboard.setup import (
    CONFIG_HEADER, SENSORS_HEADER, AUTOMATIONS_HEADER, SATEL_FIELDS_HEADER, TUYA_DEVICES_HEADER,
    build_config_dict, validate, build_automations_dict, validate_automations,
    build_satel_fields_dict, build_tuya_devices_dict, validate_satel_fields, validate_tuya_devices,
    build_connections_patch, validate_connections, connections_to_form,
    draft_path_for, load_draft, save_draft, delete_draft,
)

app = Flask(__name__)
history: HistoryStore | None = None  # ustawiane w create_app()
sensors_file_path = None  # ustawiane w create_app() - ścieżka do sensors.yaml
automations_file_path = None  # ustawiane w create_app() - ścieżka do automations.yaml
satel_fields_file_path = None  # ustawiane w create_app() - ścieżka do satel_fields.yaml
tuya_devices_file_path = None  # ustawiane w create_app() - ścieżka do tuya_devices.yaml
auth_guard: AuthGuard | None = None  # ustawiane w create_app()
satel_partitions_cfg: dict = {}  # nazwa -> numer strefy, ustawiane w create_app()
config_raw: dict = {}  # ostatnio wczytany config.raw (do odczytu automatyzacji/kontekstu), ustawiane w create_app()
config_path: pathlib.Path | None = None  # ustawiane w main() - potrzebne, zanim config.yaml w ogóle istnieje

# Endpointy dostępne, ZANIM config.yaml istnieje (kreator + statyka) -
# wszystko inne przekierowuje na /setup, dopóki konfiguracja nie powstanie.
# Testy/wykrywanie (satel-connection, tuya-cloud, tuya LAN scan) są tu
# celowo - kreator sam z nich korzysta, ZANIM config.yaml w ogóle istnieje
# (patrz templates/setup.html, kroki "Centrala Satel"/"Urządzenia Tuya").
_SETUP_EXEMPT_ENDPOINTS = {
    "setup_page", "api_setup_save", "api_setup_draft", "static",
    "api_setup_test_satel_connection", "api_discover_tuya_cloud", "api_discover_tuya",
}


def is_configured() -> bool:
    return config_path is not None and config_path.exists()


def create_app(config: BridgeConfig) -> Flask:
    global history, sensors_file_path, automations_file_path, satel_fields_file_path, tuya_devices_file_path
    global auth_guard, satel_partitions_cfg, config_raw
    db_path = config.raw.get("dashboard", {}).get("db_path", "bridge_history.db")
    history = HistoryStore(db_path)
    sensors_file_path = config.sensors_file
    automations_file_path = config.automations_file
    satel_fields_file_path = config.satel_fields_file
    tuya_devices_file_path = config.tuya_devices_file
    auth_guard = AuthGuard(config.raw.get("dashboard", {}).get("auth"))
    satel_partitions_cfg = config.satel.get("partitions", {"Dom": config.satel.get("partition", 1)})
    config_raw = config.raw
    return app


def _dict_to_name_number_rows(d: dict) -> list[dict]:
    return [{"name": name, "number": number} for name, number in d.items()]


def _virtual_inputs_to_rows(inputs: dict) -> list[dict]:
    return [{"name": name, "number": v.get("number"), "key": v.get("key")} for name, v in inputs.items()]


def _tuya_devices_to_rows(devices: dict) -> list[dict]:
    return [
        {
            "name": name, "id": cfg.get("id", ""), "ip": cfg.get("ip", ""),
            "local_key": cfg.get("local_key", ""), "type": cfg.get("type", "socket"),
            "dimmer_dp": cfg.get("dimmer_dp", ""),
        }
        for name, cfg in devices.items()
    ]


@app.before_request
def _redirect_to_setup_if_unconfigured():
    if request.endpoint in _SETUP_EXEMPT_ENDPOINTS:
        return
    if not is_configured():
        return redirect(url_for("setup_page"))


def _parse_range() -> tuple[float, float]:
    now = time.time()
    hours = float(request.args.get("hours", 24))
    ts_from = float(request.args.get("from", now - hours * 3600))
    ts_to = float(request.args.get("to", now))
    return ts_from, ts_to


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/setup")
def setup_page():
    if is_configured():
        return redirect(url_for("index"))
    return render_template("setup.html")


@app.route("/api/setup/draft", methods=["GET", "POST"])
def api_setup_draft():
    """Stan roboczy wieloetapowego kreatora - zapisywany przez frontend po
    KAŻDYM kliknięciu "Dalej" (POST), wczytywany przy otwarciu /setup (GET),
    żeby dało się wznowić konfigurację po przerwie (patrz dashboard/setup.py).
    Osobny plik od config.yaml - kreator NIGDY nie zapisuje niepełnej/
    niezwalidowanej konfiguracji pod docelową nazwą."""
    if is_configured():
        return jsonify({"error": "Konfiguracja już istnieje."}), 409

    if request.method == "GET":
        return jsonify(load_draft(draft_path_for(config_path)))

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Nieprawidłowe dane"}), 400
    save_draft(draft_path_for(config_path), data)
    return jsonify({"message": "Zapisano postęp"})


@app.route("/api/setup/test-satel-connection", methods=["POST"])
def api_setup_test_satel_connection():
    """"Testuj połączenie" w kroku 1 kreatora (i w zakładce ⚙ Ustawienia po
    zapisaniu) - sprawdza, czy pod danym adresem/portem w ogóle coś
    odpowiada po TCP. NIE waliduje samego klucza integracji (patrz
    docstring bridge/satel_client.py:test_connection - protokół integracji
    wysyła klucz dopiero przy konkretnych poleceniach, nie przy samym
    połączeniu). Publiczny - to tylko test osiągalności, nic nie zmienia."""
    payload = request.get_json(silent=True) or {}
    host = (payload.get("host") or "").strip()
    if not host:
        return jsonify({"error": "Brak adresu IP centrali"}), 400
    try:
        port = int(payload.get("port") or 7094)
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowy port"}), 400

    try:
        asyncio.run(test_satel_connection(host, port))
    except ImportError:
        return jsonify({"error": "Biblioteka satel_integra nie jest zainstalowana na serwerze"}), 500
    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd testu połączenia: {exc}"}), 502

    return jsonify({
        "message": f"Połączono z {host}:{port}. Pamiętaj: to test osiągalności, nie klucza integracji "
                    "- błędny klucz ujawni się dopiero przy uzbrajaniu/rozbrajaniu/sterowaniu wyjściem.",
    })


@app.route("/api/setup/save", methods=["POST"])
def api_setup_save():
    if is_configured():
        # Kreator NIE nadpisuje istniejącej konfiguracji - żeby ręczne
        # zmiany w config.yaml nigdy nie zniknęły przez przypadkowe
        # ponowne wejście na /setup (np. ktoś inny w sieci domowej).
        return jsonify({"error": "Konfiguracja już istnieje. Usuń config.yaml ręcznie, żeby uruchomić kreator ponownie."}), 409

    form = request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    config_dict = build_config_dict(form)
    automations_dict = build_automations_dict(form)
    satel_fields_dict = build_satel_fields_dict(form)
    tuya_devices_dict = build_tuya_devices_dict(form)

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(CONFIG_HEADER)
        yaml.safe_dump(config_dict, f, allow_unicode=True, sort_keys=False)

    sensors_path = config_path.parent / config_dict["ebus"]["sensors_file"]
    with open(sensors_path, "w", encoding="utf-8") as f:
        f.write(SENSORS_HEADER)
        yaml.safe_dump({"sensors": []}, f, allow_unicode=True, sort_keys=False)

    automations_path = config_path.parent / config_dict["automations_file"]
    with open(automations_path, "w", encoding="utf-8") as f:
        f.write(AUTOMATIONS_HEADER)
        yaml.safe_dump(automations_dict, f, allow_unicode=True, sort_keys=False)

    satel_fields_path = config_path.parent / config_dict["satel"]["fields_file"]
    with open(satel_fields_path, "w", encoding="utf-8") as f:
        f.write(SATEL_FIELDS_HEADER)
        yaml.safe_dump(satel_fields_dict, f, allow_unicode=True, sort_keys=False)

    tuya_devices_path = config_path.parent / config_dict["tuya"]["devices_file"]
    with open(tuya_devices_path, "w", encoding="utf-8") as f:
        f.write(TUYA_DEVICES_HEADER)
        yaml.safe_dump(tuya_devices_dict, f, allow_unicode=True, sort_keys=False)

    # Konfiguracja jest już kompletna i zapisana - plik roboczy kreatora
    # (postęp między krokami) nie jest już potrzebny.
    delete_draft(draft_path_for(config_path))

    # Od razu wczytujemy świeżo zapisaną konfigurację do TEGO procesu -
    # panel jest natychmiast użyteczny bez ręcznego restartu (sam proces
    # bridge'a to osobny proces i wciąż wymaga uruchomienia/restartu -
    # patrz komunikat zwracany niżej).
    config = BridgeConfig.load(config_path)
    create_app(config)

    return jsonify({
        "message": "Konfiguracja zapisana. Uruchom teraz główny proces bridge'a, żeby zacząć zbierać dane "
                    "(panel sam się już przeładował i jest gotowy).",
        "next_steps": [
            "python main.py --config " + str(config_path),
            "Dodaj czujniki kotła eBUS, strefy/wyjścia Satela i urządzenia Tuya w zakładce ⚙ Ustawienia",
            "Dodaj/edytuj sceny, harmonogramy, eskalację i symulację obecności w zakładce 🎬 Automatyzacje",
        ],
    })


@app.route("/config")
def config_page():
    return render_template("config.html")


@app.route("/api/config/sensors", methods=["GET"])
def api_config_sensors_get():
    if not sensors_file_path.exists():
        return jsonify([])
    with open(sensors_file_path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f) or {}
    return jsonify(content.get("sensors", []))


@app.route("/api/config/sensors", methods=["POST"])
@require_auth()
def api_config_sensors_post():
    """Nadpisuje CAŁĄ zawartość sensors.yaml przesłaną listą. Walidacja jest
    celowo minimalna (tylko to, co faktycznie zepsułoby resztę bridge'a przy
    starcie) - reszta pól ma sensowne wartości domyślne."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        return jsonify({"error": "Oczekiwano listy czujników w ciele żądania"}), 400

    cleaned = []
    seen_names = set()
    for i, item in enumerate(payload):
        name = (item.get("name") or "").strip()
        mqtt_topic = (item.get("mqtt_topic") or "").strip()
        if not name or not mqtt_topic:
            return jsonify({"error": f"Wiersz {i + 1}: pola 'name' i 'mqtt_topic' są wymagane"}), 400
        if name in seen_names:
            return jsonify({"error": f"Zduplikowana nazwa czujnika: '{name}'"}), 400
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

    with open(sensors_file_path, "w", encoding="utf-8") as f:
        f.write("# Wygenerowane z panelu webowego (/config) - patrz sensors.example.yaml po opis pól\n")
        yaml.safe_dump({"sensors": cleaned}, f, allow_unicode=True, sort_keys=False)

    return jsonify({
        "saved": len(cleaned),
        "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a "
                    "(nie samego panelu) - np. `sudo systemctl restart satel-tuya-bridge`.",
    })


@app.route("/api/discover/ebus", methods=["POST"])
def api_discover_ebus():
    """Krótkie (kilka sekund) nasłuchiwanie na MQTT ebusd - podpowiedź
    tematów/pól do dodania jako czujnik w /config (patrz przycisk "Wykryj
    czujniki"). Publiczny - to tylko podgląd sieci, nic nie zmienia.

    UCZCIWIE: to najprostsza możliwa heurystyka (kto faktycznie opublikował
    coś w krótkim oknie czasowym), NIE pełna lista pól dostępnych w ebusd -
    rzadziej publikowane pola mogą się nie pojawić. Można kliknąć ponownie
    albo dodać czujnik ręcznie (patrz README)."""
    payload = request.get_json(silent=True) or {}
    ebus_cfg = config_raw.get("ebus", {})
    mqtt_host = payload.get("mqtt_host") or ebus_cfg.get("mqtt_host")
    mqtt_port = payload.get("mqtt_port") or ebus_cfg.get("mqtt_port", 1883)
    if not mqtt_host:
        return jsonify({"error": "Brak adresu brokera MQTT"}), 400
    try:
        topics = discover_ebus_topics(mqtt_host, int(mqtt_port))
    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd wykrywania: {exc}"}), 502
    return jsonify({"topics": topics})


@app.route("/api/discover/tuya", methods=["POST"])
def api_discover_tuya():
    """Skanowanie sieci LAN (broadcast UDP) w poszukiwaniu urządzeń Tuya -
    podpowiedź id/ip/version do panelu /config. Publiczny - tylko odczyt.

    UWAGA: NIE zwraca `local_key` (nie jest rozgłaszany ze względów
    bezpieczeństwa) - trzeba go osobno wyciągnąć narzędziem
    `python -m tinytuya wizard` (patrz README). Skan trwa ok. 18 sekund."""
    try:
        devices = scan_tuya_network()
    except Exception as exc:
        return jsonify({"error": f"Błąd skanowania: {exc}"}), 502
    return jsonify({"devices": devices})


@app.route("/api/discover/tuya-cloud", methods=["POST"])
def api_discover_tuya_cloud():
    """Import urządzeń z Tuya Cloud (nazwa + ID + local_key na raz) -
    alternatywa dla ręcznego dodawania jednego urządzenia na raz (przydatna
    przy wielu urządzeniach), dostępna w kreatorze (krok "Urządzenia Tuya")
    i w zakładce ⚙ Ustawienia. Publiczny - poświadczenia Tuya Cloud
    przychodzą w ciele żądania i NIE są nigdzie zapisywane przez ten
    endpoint (o zapis do config.yaml/tuya_devices.yaml dba osobny krok
    zapisu formularza - patrz api_setup_save/api_config_tuya_devices_post)."""
    payload = request.get_json(silent=True) or {}
    api_region = (payload.get("api_region") or "").strip()
    access_id = (payload.get("access_id") or "").strip()
    access_key = (payload.get("access_key") or "").strip()
    if not api_region or not access_id or not access_key:
        return jsonify({"error": "Region, Access ID i Access Secret są wymagane"}), 400

    try:
        devices = fetch_tuya_cloud_devices(api_region, access_id, access_key)
    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd importu z Tuya Cloud: {exc}"}), 502
    return jsonify({"devices": devices})


@app.route("/api/config/satel-fields", methods=["GET"])
def api_config_satel_fields_get():
    satel = config_raw.get("satel", {})
    return jsonify({
        "partitions": _dict_to_name_number_rows(satel.get("partitions", {})),
        "monitored_zones": _dict_to_name_number_rows(satel.get("monitored_zones", {})),
        "monitored_outputs": _dict_to_name_number_rows(satel.get("monitored_outputs", {})),
        "virtual_inputs": _virtual_inputs_to_rows(satel.get("virtual_inputs", {}).get("inputs", {})),
    })


@app.route("/api/config/satel-fields", methods=["POST"])
@require_auth()
def api_config_satel_fields_post():
    """Nadpisuje CAŁĄ zawartość satel_fields.yaml (strefy/wyjścia/wejścia
    IP/partycje) przesłanym formularzem - ten sam kształt danych, jaki
    zbiera kreator w kroku "Centrala Satel Integra" (patrz dashboard/setup.py:
    validate_satel_fields/build_satel_fields_dict)."""
    form = request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate_satel_fields(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    satel_fields_dict = build_satel_fields_dict(form)
    with open(satel_fields_file_path, "w", encoding="utf-8") as f:
        f.write(SATEL_FIELDS_HEADER)
        yaml.safe_dump(satel_fields_dict, f, allow_unicode=True, sort_keys=False)

    config = BridgeConfig.load(config_path)
    create_app(config)

    return jsonify({
        "saved": {k: len(v) if isinstance(v, dict) else v for k, v in satel_fields_dict.items()},
        "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a "
                    "(nie samego panelu) - np. `sudo systemctl restart satel-tuya-bridge`.",
    })


@app.route("/api/config/tuya-devices", methods=["GET"])
def api_config_tuya_devices_get():
    return jsonify({"devices": _tuya_devices_to_rows(config_raw.get("tuya", {}).get("devices", {}))})


@app.route("/api/config/tuya-devices", methods=["POST"])
@require_auth()
def api_config_tuya_devices_post():
    """Nadpisuje CAŁĄ zawartość tuya_devices.yaml przesłanym formularzem -
    ten sam kształt danych, jaki zbiera kreator w kroku "Urządzenia Tuya"
    (patrz dashboard/setup.py: validate_tuya_devices/build_tuya_devices_dict)."""
    form = request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate_tuya_devices(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    tuya_devices_dict = build_tuya_devices_dict(form)
    with open(tuya_devices_file_path, "w", encoding="utf-8") as f:
        f.write(TUYA_DEVICES_HEADER)
        yaml.safe_dump(tuya_devices_dict, f, allow_unicode=True, sort_keys=False)

    config = BridgeConfig.load(config_path)
    create_app(config)

    return jsonify({
        "saved": len(tuya_devices_dict["devices"]),
        "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a "
                    "(nie samego panelu) - np. `sudo systemctl restart satel-tuya-bridge`.",
    })


@app.route("/api/config/connections", methods=["GET"])
@require_auth()
def api_config_connections_get():
    """Zakładka "Połączenia" panelu ⚙ Ustawienia - jedyne miejsce, w
    którym da się PO PIERWSZEJ KONFIGURACJI zmienić host/port/klucze/tematy
    MQTT (kreator /setup jest celowo zablokowany po zapisie, patrz
    api_setup_save - to była świadoma decyzja, żeby przypadkowe ponowne
    wejście na /setup nigdy nie nadpisało całego config.yaml). Wymaga
    hasła administratora NAWET do odczytu (w odróżnieniu od reszty
    endpointów /api/config/*) - zwracane dane obejmują, czy klucz integracji
    Satela / Tuya Cloud Access Secret SĄ ustawione, nigdy ich wartości."""
    with open(config_path, "r", encoding="utf-8") as f:
        current = yaml.safe_load(f) or {}
    return jsonify(connections_to_form(current))


@app.route("/api/config/connections", methods=["POST"])
@require_auth()
def api_config_connections_post():
    """Nadpisuje TYLKO pola połączeń w config.yaml (patrz
    dashboard/setup.py: build_connections_patch) - reszta pliku (reguły
    kamer/alarmów, automatyka kotła, ustawienia konserwacji itd.) zostaje
    nietknięta."""
    form = request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate_connections(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    with open(config_path, "r", encoding="utf-8") as f:
        current = yaml.safe_load(f) or {}
    new_config = build_connections_patch(form, current)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(CONFIG_HEADER)
        yaml.safe_dump(new_config, f, allow_unicode=True, sort_keys=False)

    config = BridgeConfig.load(config_path)
    create_app(config)

    return jsonify({
        "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a "
                    "(nie samego panelu) - np. `sudo systemctl restart satel-tuya-bridge`.",
    })


@app.route("/automations")
def automations_page():
    return render_template("automations.html")


@app.route("/api/config/automation-context", methods=["GET"])
def api_config_automation_context():
    """Dane pomocnicze do budowania formularza w /automations - nazwy
    urządzeń Tuya i wirtualnych wejść Satela już skonfigurowanych w
    config.yaml (te dwie rzeczy panel /automations tylko ODCZYTUJE, nie
    edytuje - edycja urządzeń/wejść zostaje ręczna w config.yaml, patrz
    README). Publiczny - same nazwy nie są wrażliwe (bez local_key/kluczy)."""
    tuya_devices = list(config_raw.get("tuya", {}).get("devices", {}).keys())
    virtual_inputs = list(config_raw.get("satel", {}).get("virtual_inputs", {}).get("inputs", {}).keys())
    return jsonify({"tuya_devices": tuya_devices, "virtual_inputs": virtual_inputs})


@app.route("/api/config/automations", methods=["GET"])
def api_config_automations_get():
    """Aktualne sceny/reguły ruchu/symulacja obecności - z LIVE config.raw
    (nie bezpośrednio z pliku), żeby poprawnie obsłużyć zarówno nowe configi
    (automations.yaml) jak i starsze, gdzie te sekcje są wciąż wpisane
    bezpośrednio w config.yaml (patrz BridgeConfig.load)."""
    return jsonify({
        "scenes": config_raw.get("scenes", []),
        "motion_rules": config_raw.get("reguły_ruchu", []),
        "presence_simulation": config_raw.get("presence_simulation", {"enabled": False}),
    })


@app.route("/api/config/automations", methods=["POST"])
@require_auth()
def api_config_automations_post():
    """Nadpisuje CAŁĄ zawartość automations.yaml (sceny + reguły ruchu +
    symulacja obecności) przesłanym formularzem - ten sam kształt danych,
    jaki zbiera kreator w krokach "Sceny"/"Reguły ruchu" (patrz
    dashboard/setup.py: validate_automations/build_automations_dict)."""
    form = request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate_automations(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    automations_dict = build_automations_dict(form)
    with open(automations_file_path, "w", encoding="utf-8") as f:
        f.write(AUTOMATIONS_HEADER)
        yaml.safe_dump(automations_dict, f, allow_unicode=True, sort_keys=False)

    # Jak przy zapisie czujników - odświeżamy config.raw w TYM procesie
    # (żeby GET zaraz potem pokazał świeże dane), ale sam proces bridge'a
    # (osobny proces) wciąż wymaga ręcznego restartu.
    config = BridgeConfig.load(config_path)
    create_app(config)

    return jsonify({
        "saved": {
            "scenes": len(automations_dict["scenes"]),
            "motion_rules": len(automations_dict["reguły_ruchu"]),
        },
        "message": "Zapisano. Zmiany zaczną działać po restarcie procesu bridge'a "
                    "(nie samego panelu) - np. `sudo systemctl restart satel-tuya-bridge`.",
    })


@app.route("/api/sensors")
def api_sensors():
    return jsonify(history.get_sensors())


@app.route("/api/history")
def api_history():
    sensor = request.args.get("sensor")
    if not sensor:
        return jsonify({"error": "brak parametru 'sensor'"}), 400
    ts_from, ts_to = _parse_range()
    return jsonify(history.get_history(sensor, ts_from, ts_to))


@app.route("/api/events")
def api_events():
    event_type = request.args.get("type") or None
    ts_from, ts_to = _parse_range()
    limit = int(request.args.get("limit", 500))
    return jsonify(history.get_events(event_type, ts_from, ts_to, limit))


@app.route("/api/alerts/active")
def api_alerts_active():
    return jsonify(history.get_active_alerts())


@app.route("/api/alerts/history")
def api_alerts_history():
    ts_from, ts_to = _parse_range()
    limit = int(request.args.get("limit", 200))
    return jsonify(history.get_alerts_history(ts_from, ts_to, limit))


@app.route("/api/status")
def api_status():
    category = request.args.get("category") or None
    return jsonify(history.get_status(category))


@app.route("/api/satel/partitions")
def api_satel_partitions():
    """Lista nazwanych stref do wyboru w panelu - samo nazewnictwo nie jest
    wrażliwe, więc ten endpoint zostaje publiczny (bez autoryzacji).
    Faktyczne uzbrojenie/rozbrojenie wymaga hasła - patrz endpointy niżej."""
    return jsonify(satel_partitions_cfg)


@app.route("/api/satel/arm", methods=["POST"])
@require_auth()
def api_satel_arm():
    payload = request.get_json(silent=True) or {}
    partitions = payload.get("partitions") or list(satel_partitions_cfg.values())
    mode = payload.get("mode", 0)
    command_id = history.enqueue_command("satel_arm", {"partitions": partitions, "mode": mode})
    history.write_event("system", source="dashboard", detail={"message": f"Zlecono uzbrojenie stref {partitions}"})
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/satel/disarm", methods=["POST"])
@require_auth()
def api_satel_disarm():
    payload = request.get_json(silent=True) or {}
    partitions = payload.get("partitions") or list(satel_partitions_cfg.values())
    command_id = history.enqueue_command("satel_disarm", {"partitions": partitions})
    history.write_event("system", source="dashboard", detail={"message": f"Zlecono rozbrojenie stref {partitions}"})
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/commands/<int:command_id>")
def api_command_status(command_id: int):
    """Odczyt statusu wcześniej zleconej komendy (do polowania z frontendu,
    żeby pokazać czy uzbrojenie faktycznie się powiodło) - samo zlecenie
    komendy wymagało hasła, sam odczyt statusu jest nieszkodliwy (nie
    pozwala niczego zmienić), więc zostaje publiczny."""
    cmd = history.get_command(command_id)
    if cmd is None:
        return jsonify({"error": "Nie znaleziono komendy"}), 404
    return jsonify(cmd)


@app.route("/healthz")
def healthz():
    """Prosty endpoint zdrowia do zewnętrznego monitoringu (np. uptime-kuma,
    healthcheck w systemd/monit). Zwraca 200 jeśli wszystkie monitorowane
    komponenty są połączone, 503 jeśli którykolwiek zgłasza rozłączenie."""
    health_statuses = history.get_status("system_health")
    disconnected = [s for s in health_statuses if not s["value"].startswith("połączono")]
    body = {
        "ok": len(disconnected) == 0,
        "components": {s["key"].replace("health:", ""): s["value"] for s in health_statuses},
    }
    return jsonify(body), 200 if not disconnected else 503


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    global config_path
    config_path = pathlib.Path(args.config)

    from waitress import serve

    if config_path.exists():
        create_app(BridgeConfig.load(config_path))
        print(f"Panel dostępny pod http://{args.host}:{args.port}")
    else:
        print(
            f"Brak {config_path} - uruchamiam kreator pierwszej konfiguracji.\n"
            f"Otwórz http://{args.host if args.host != '0.0.0.0' else '<adres-rpi>'}:{args.port}/setup w przeglądarce."
        )

    serve(app, host=args.host, port=args.port, threads=2)


if __name__ == "__main__":
    main()
