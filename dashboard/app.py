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
import os
import pathlib
import threading
import time

import yaml
from flask import Flask, jsonify, redirect, render_template, request, url_for

from bridge.config import BridgeConfig
from bridge.ebus_client import discover_topics as discover_ebus_topics, BackgroundTopicDiscovery
from bridge.history_store import HistoryStore
from bridge.satel_client import test_connection as test_satel_connection
from bridge.satel_client import discover_active_zones_and_outputs
from bridge.tuya_local import scan_network as scan_tuya_network, fetch_cloud_devices as fetch_tuya_cloud_devices
from dashboard.auth import AuthGuard, require_auth, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from dashboard.setup import (
    CONFIG_HEADER, SENSORS_HEADER, AUTOMATIONS_HEADER, SATEL_FIELDS_HEADER, TUYA_DEVICES_HEADER,
    build_config_dict, validate, build_automations_dict, validate_automations,
    build_satel_fields_dict, build_tuya_devices_dict, validate_satel_fields, validate_tuya_devices,
    build_connections_patch, validate_connections, connections_to_form, list_plugins,
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
    "api_discover_satel",
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


_RELOAD_MESSAGE = (
    "Zapisano. Bridge sam przeładuje konfigurację w ciągu ok. 1-2s, bez restartu procesu "
    "(patrz RulesEngine.reload_config w bridge/rules_engine.py) - nic więcej nie trzeba robić."
)


def _enqueue_bridge_reload() -> None:
    """Prosi główny proces bridge'a o przeładowanie 'lekkiej' konfiguracji
    (czujniki eBUS/pola Satela/urządzenia Tuya/automatyzacje) BEZ restartu
    procesu - ten sam mechanizm kolejki komend co uzbrojenie/rozbrojenie
    (patrz history_store.py: enqueue_command, rules_engine.py:
    _command_poll_loop/_execute_command/reload_config). Panel i bridge to
    DWA OSOBNE procesy, więc to jedyny sposób, żeby panel poprosił bridge
    o cokolwiek."""
    history.enqueue_command("reload_config", {})


def _dict_to_name_number_rows(d: dict) -> list[dict]:
    return [{"name": name, "number": number} for name, number in d.items()]


def _zones_to_rows(numbers: dict, types: dict) -> list[dict]:
    """Jak _dict_to_name_number_rows, ale dokłada opcjonalny "type" (rodzaj
    czujki/wyjścia - patrz SATEL_ZONE_TYPES/SATEL_OUTPUT_TYPES w setup.py) do
    edycji w /config i wyświetlania ikon w panelu głównym."""
    return [{"name": name, "number": number, "type": types.get(name, "")} for name, number in numbers.items()]


def _virtual_inputs_to_rows(inputs: dict) -> list[dict]:
    return [{"name": name, "number": v.get("number"), "key": v.get("key")} for name, v in inputs.items()]


def _tuya_devices_to_rows(devices: dict) -> list[dict]:
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


def _zigbee_gateways_to_rows(gateways: dict) -> list[dict]:
    return [
        {
            "name": name, "id": cfg.get("id", ""), "ip": cfg.get("ip", ""),
            "local_key": cfg.get("local_key", ""), "version": cfg.get("version", 3.4),
        }
        for name, cfg in gateways.items()
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


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    """Prawdziwe logowanie sesyjne - hasło w ciele żądania POST (zamaskowane
    pole formularza w templates/login.html, NIGDY prompt()), token sesji
    wraca w ciasteczku HttpOnly. Rate-limiting po IP jak reszta AuthGuard."""
    payload = request.get_json(silent=True) or {}
    password = payload.get("password", "")
    ok, message = auth_guard.check_password(password)
    if not ok:
        return jsonify({"error": message}), 401

    token = auth_guard.create_session()
    resp = jsonify({"message": "Zalogowano"})
    resp.set_cookie(SESSION_COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="Lax")
    return resp


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Zawsze zwraca 200 (nawet bez aktywnej sesji) - wylogowanie kogoś, kto
    już nie jest zalogowany, nie jest błędem."""
    auth_guard.destroy_session(request.cookies.get(SESSION_COOKIE_NAME))
    resp = jsonify({"message": "Wylogowano"})
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.route("/api/auth/status")
def api_auth_status():
    """Do pokazania stanu (Zaloguj/Wyloguj) w nawigacji paneli - publiczny,
    nic nie ujawnia poza tym, czy TA przeglądarka ma ważną sesję."""
    logged_in = auth_guard.check_session(request.cookies.get(SESSION_COOKIE_NAME))
    return jsonify({"logged_in": logged_in, "configured": auth_guard.configured})


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


@app.route("/api/discover/satel", methods=["POST"])
def api_discover_satel():
    """Best-effort wykrywanie AKTYWNYCH stref/wyjść Satela (krok 1
    kreatora, przycisk "🔍 Wykryj aktywne strefy/wyjścia") - klucz
    integracji NIE jest potrzebny (jak w teście połączenia wyżej). Publiczny
    - to tylko nasłuch, nic nie zmienia w centrali. Przeczytaj docstring
    bridge/satel_client.py:discover_active_zones_and_outputs PRZED użyciem
    gdziekolwiek indziej - to NIE jest pełny import konfiguracji, tylko
    numery, które fizycznie zmienią stan podczas nasłuchu."""
    payload = request.get_json(silent=True) or {}
    satel_cfg = config_raw.get("satel", {})
    # Domyślnie host/port z JUŻ ZAPISANEGO config.yaml (tak samo jak
    # api_discover_ebus niżej dla mqtt_host) - dzięki temu zakładka
    # ⚙ Ustawienia może wołać ten endpoint BEZ znajomości hasła
    # administratora (host/port nie są sekretem, w odróżnieniu od klucza
    # integracji - patrz api_config_connections_get) i bez przepisywania ich
    # w JS z osobnej, zablokowanej hasłem zakładki "Połączenia". Kreator
    # (/setup, PRZED zapisaniem config.yaml) wciąż podaje je wprost w ciele
    # żądania, bo `satel_cfg` jest wtedy puste.
    host = (payload.get("host") or satel_cfg.get("host") or "").strip()
    if not host:
        return jsonify({"error": "Brak adresu IP centrali"}), 400
    try:
        port = int(payload.get("port") or satel_cfg.get("port") or 7094)
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowy port"}), 400
    try:
        timeout_seconds = min(float(payload.get("timeout_seconds") or 30), 120)
    except (TypeError, ValueError):
        timeout_seconds = 30.0

    try:
        result = asyncio.run(discover_active_zones_and_outputs(host, port, timeout_seconds=timeout_seconds))
    except ImportError:
        return jsonify({"error": "Biblioteka satel_integra nie jest zainstalowana na serwerze"}), 500
    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd wykrywania: {exc}"}), 502

    return jsonify(result)


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

    resp = jsonify({
        "message": "Konfiguracja zapisana. Uruchom teraz główny proces bridge'a, żeby zacząć zbierać dane "
                    "(panel sam się już przeładował i jest gotowy).",
        "next_steps": [
            "python main.py --config " + str(config_path),
            "Dodaj czujniki kotła eBUS, strefy/wyjścia Satela i urządzenia Tuya w zakładce ⚙ Ustawienia",
            "Dodaj/edytuj sceny, harmonogramy, eskalację i symulację obecności w zakładce 🎬 Automatyzacje",
        ],
    })
    # Logujemy od razu tym samym hasłem, które właśnie ustawiono w kreatorze
    # - ciasteczkiem sesji (HttpOnly), NIE przez przekazywanie hasła w
    # fragmencie URL (tak robiła wcześniejsza wersja - #auth=base64(hasło),
    # co jest jawnym, odwracalnym zakodowaniem hasła w adresie strony).
    # Panel po przeładowaniu jest więc od razu zalogowany, bez ponownego
    # wpisywania hasła 30 sekund później.
    token = auth_guard.create_session()
    resp.set_cookie(SESSION_COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="Lax")
    return resp


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
    _enqueue_bridge_reload()

    return jsonify({"saved": len(cleaned), "message": _RELOAD_MESSAGE})


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


# Jedna instancja procesu-wide - jeden nasłuch w tle naraz wystarcza dla
# panelu administrowanego przez jedną osobę (patrz
# bridge/ebus_client.py:BackgroundTopicDiscovery).
_ebus_background_discovery = BackgroundTopicDiscovery()


@app.route("/api/discover/ebus/start", methods=["POST"])
def api_discover_ebus_start():
    """Rozpoczyna nasłuch MQTT ebusd W TLE (domyślnie 5 minut) - w
    odróżnieniu od /api/discover/ebus wyżej (blokujące ~4s), to żądanie
    wraca NATYCHMIAST; wyniki odbiera się przez /api/discover/ebus/status
    (odpytywane cyklicznie przez panel). Dłuższy nasłuch łapie też
    rzadziej publikowane pola, których krótkie okno mogło nie zauważyć."""
    payload = request.get_json(silent=True) or {}
    ebus_cfg = config_raw.get("ebus", {})
    mqtt_host = payload.get("mqtt_host") or ebus_cfg.get("mqtt_host")
    mqtt_port = payload.get("mqtt_port") or ebus_cfg.get("mqtt_port", 1883)
    if not mqtt_host:
        return jsonify({"error": "Brak adresu brokera MQTT"}), 400
    try:
        duration_seconds = min(float(payload.get("duration_seconds") or 300), 600)
    except (TypeError, ValueError):
        duration_seconds = 300.0

    try:
        _ebus_background_discovery.start(mqtt_host, int(mqtt_port), duration_seconds=duration_seconds)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd uruchamiania nasłuchu: {exc}"}), 502

    return jsonify(_ebus_background_discovery.status())


@app.route("/api/discover/ebus/status")
def api_discover_ebus_status():
    return jsonify(_ebus_background_discovery.status())


@app.route("/api/discover/ebus/stop", methods=["POST"])
def api_discover_ebus_stop():
    return jsonify(_ebus_background_discovery.stop())


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
        "monitored_zones": _zones_to_rows(satel.get("monitored_zones", {}), satel.get("zone_types", {})),
        "monitored_outputs": _zones_to_rows(satel.get("monitored_outputs", {}), satel.get("output_types", {})),
        "virtual_inputs": _virtual_inputs_to_rows(satel.get("virtual_inputs", {}).get("inputs", {})),
        # Mapy nazwa -> rodzaj, osobno (obok wierszy wyżej) - wygodne dla
        # panelu głównego (index.html), który dopasowuje typ do statusu po
        # etykiecie (status.label), bez potrzeby osobnego zapytania.
        "zone_types": satel.get("zone_types", {}),
        "output_types": satel.get("output_types", {}),
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
    _enqueue_bridge_reload()

    return jsonify({
        "saved": {k: len(v) if isinstance(v, dict) else v for k, v in satel_fields_dict.items()},
        "message": _RELOAD_MESSAGE,
    })


@app.route("/api/config/tuya-devices", methods=["GET"])
def api_config_tuya_devices_get():
    return jsonify({
        "devices": _tuya_devices_to_rows(config_raw.get("tuya", {}).get("devices", {})),
        "zigbee_gateways": _zigbee_gateways_to_rows(config_raw.get("tuya", {}).get("zigbee_gateways", {})),
    })


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
    _enqueue_bridge_reload()

    return jsonify({"saved": len(tuya_devices_dict["devices"]), "message": _RELOAD_MESSAGE})


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


# Które z przełączalnych/regulowalnych wtyczek (patrz PLUGIN_DEFS w
# dashboard/setup.py) żyją w config.yaml (zakładka 🔌 Połączenia) a które w
# automations.yaml (zakładka 🎬 Automatyzacje, podłapywane na żywo przez
# RulesEngine.reload_config() - patrz też komentarz w
# _build_presence_simulation w setup.py o jednym wyjątku).
#
# W obrębie config.yaml dwie różne rzeczy wymagają różnego traktowania:
# WŁĄCZENIE/WYŁĄCZENIE integracji (tuya_cloud/home_assistant/mqtt_publish)
# dotyka żywych połączeń sieciowych - wymaga PEŁNEGO RESTARTU procesu,
# tak jak reszta zakładki 🔌 Połączenia. INTERWAŁ ODPYTYWANIA (satel/
# tuya_local) to tylko wartość, którą pętla w tle czyta na bieżąco przy
# każdym obrocie (patrz satel_client.py: _periodic_status_sync,
# rules_engine.py: _tuya_status_poll_loop) - działa NA ŻYWO, bez restartu.
_CONNECTIONS_PLUGIN_IDS_RESTART = {"tuya_cloud", "home_assistant", "mqtt_publish"}
_CONNECTIONS_PLUGIN_IDS_LIVE = {"satel", "tuya_local"}
_CONNECTIONS_PLUGIN_IDS = _CONNECTIONS_PLUGIN_IDS_RESTART | _CONNECTIONS_PLUGIN_IDS_LIVE
_AUTOMATIONS_PLUGIN_IDS = {"presence_simulation"}


@app.route("/plugins")
def plugins_page():
    return render_template("plugins.html")


@app.route("/api/plugins", methods=["GET"])
def api_plugins_get():
    """Przegląd wszystkich integracji (zakładka 🧩 Wtyczki) - publiczny,
    zwraca tylko stan włączenia/skonfigurowania, żadnych sekretów (patrz
    list_plugins w dashboard/setup.py)."""
    return jsonify(list_plugins(config_raw))


@app.route("/api/plugins", methods=["POST"])
@require_auth()
def api_plugins_post():
    """Przełącza WYŁĄCZNIE pole "enabled" (tuya_cloud/home_assistant/
    mqtt_publish/presence_simulation) albo interwał odpytywania (satel/
    tuya_local) wskazanych wtyczek (patrz _CONNECTIONS_PLUGIN_IDS_RESTART/
    _CONNECTIONS_PLUGIN_IDS_LIVE/_AUTOMATIONS_PLUGIN_IDS wyżej) - reszta
    ustawień danej integracji (host/klucze/urządzenia/itd.) zostaje
    nietknięta. Do PEŁNEJ konfiguracji (np. pierwsze podanie poświadczeń
    Tuya Cloud) wciąż trzeba przejść do właściwej zakładki (🔌 Połączenia /
    🎬 Automatyzacje) - to przełącznik/regulator, nie edytor."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Nieprawidłowe dane"}), 400

    unknown = set(body) - _CONNECTIONS_PLUGIN_IDS - _AUTOMATIONS_PLUGIN_IDS
    if unknown:
        return jsonify({"error": f"Nieznane/nieprzełączalne wtyczki: {', '.join(sorted(unknown))}"}), 400

    saved = {}

    connections_body = {k: v for k, v in body.items() if k in _CONNECTIONS_PLUGIN_IDS}
    if connections_body:
        with open(config_path, "r", encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}
        form = connections_to_form(current)
        for plugin_id, patch in connections_body.items():
            patch = patch or {}
            if plugin_id in _CONNECTIONS_PLUGIN_IDS_RESTART:
                form.setdefault(plugin_id, {})["enabled"] = bool(patch.get("enabled"))
            else:
                # satel/tuya_local - TYLKO interwał odpytywania, na żywo
                if "status_poll_interval_seconds" in patch:
                    form.setdefault(plugin_id, {})["status_poll_interval_seconds"] = patch["status_poll_interval_seconds"]

        errors = validate_connections(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        new_config = build_connections_patch(form, current)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(CONFIG_HEADER)
            yaml.safe_dump(new_config, f, allow_unicode=True, sort_keys=False)
        saved.update(connections_body)

    automations_body = {k: v for k, v in body.items() if k in _AUTOMATIONS_PLUGIN_IDS}
    if automations_body:
        form = {
            "scenes": config_raw.get("scenes", []),
            "motion_rules": config_raw.get("reguły_ruchu", []),
            "presence_simulation": dict(config_raw.get("presence_simulation", {})),
        }
        if "presence_simulation" in automations_body:
            form["presence_simulation"]["enabled"] = bool(
                (automations_body["presence_simulation"] or {}).get("enabled")
            )

        errors = validate_automations(form)
        if errors:
            return jsonify({"error": "Popraw błędy:", "details": errors}), 400

        automations_dict = build_automations_dict(form)
        with open(automations_file_path, "w", encoding="utf-8") as f:
            f.write(AUTOMATIONS_HEADER)
            yaml.safe_dump(automations_dict, f, allow_unicode=True, sort_keys=False)
        saved.update(automations_body)

    config = BridgeConfig.load(config_path)
    create_app(config)
    # Interwały odpytywania (satel/tuya_local) i symulacja obecności działają
    # na żywo przez tę samą kolejkę komend co reszta panelu - w odróżnieniu
    # od włączenia/wyłączenia integracji, które nadal wymaga restartu.
    live_connections_ids = set(connections_body) & _CONNECTIONS_PLUGIN_IDS_LIVE
    if automations_body or live_connections_ids:
        _enqueue_bridge_reload()

    restart_ids = set(connections_body) & _CONNECTIONS_PLUGIN_IDS_RESTART
    message = "Zapisano."
    if restart_ids:
        message += (" Zmiany dla " + ", ".join(sorted(restart_ids)) + " wymagają pełnego restartu "
                    "procesu bridge'a (np. `sudo systemctl restart satel-tuya-bridge`).")
    if live_connections_ids:
        message += " Zmiany interwału odpytywania zadziałają w ciągu ok. 1-2s, bez restartu."
    if automations_body:
        message += " Zmiany dla symulacji obecności zadziałają w ciągu ok. 1-2s, bez restartu."

    return jsonify({"saved": saved, "message": message})


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
    # (żeby GET zaraz potem pokazał świeże dane), i prosimy bridge o
    # przeładowanie SWOJEJ konfiguracji (osobny proces) przez kolejkę
    # komend - patrz _enqueue_bridge_reload/RulesEngine.reload_config.
    config = BridgeConfig.load(config_path)
    create_app(config)
    _enqueue_bridge_reload()

    return jsonify({
        "saved": {
            "scenes": len(automations_dict["scenes"]),
            "motion_rules": len(automations_dict["reguły_ruchu"]),
        },
        "message": _RELOAD_MESSAGE,
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


@app.route("/api/system/restart", methods=["POST"])
@require_auth()
def api_system_restart():
    """Pełny restart CAŁEGO bridge'a (obu kontenerów Dockera - bridge +
    panel), nie tylko reload_config() (który celowo NIE dotyka żywych
    połączeń sieciowych, patrz RulesEngine.reload_config). Przydatny gdy
    trzeba naprawdę zerwać i odtworzyć połączenie z centralą Satel/Tuya
    (np. po zmianie hosta/portu/kluczy w zakładce "Połączenia", albo gdy
    połączenie utknęło w złym stanie).

    Docker Compose ma dla obu serwisów `restart: unless-stopped` (patrz
    docker-compose.yml) - więc zwykłe zakończenie procesu wystarcza,
    Docker sam podniesie kontener ponownie. Bridge (osobny proces/kontener)
    dostaje polecenie przez tę samą kolejkę komend co uzbrojenie/reload
    (panel nie ma bezpośredniego dostępu do jego procesu) - PANEL restartuje
    się sam, bezpośrednio, bo to ten sam proces, który obsługuje to żądanie."""
    command_id = history.enqueue_command("restart_process", {})
    history.write_event("system", source="dashboard", detail={"message": "Zlecono pełny restart bridge'a (panel + bridge)"})

    def _restart_dashboard() -> None:
        os._exit(0)

    # Krótkie opóźnienie na osobnym wątku - dajemy Flaskowi/waitressowi
    # chwilę na dosłanie odpowiedzi JSON do przeglądarki, zanim ten proces
    # zniknie (os._exit() jest natychmiastowy, nie da szansy na "dokończ
    # bieżące żądanie" jak zwykły sys.exit()).
    threading.Timer(0.5, _restart_dashboard).start()

    return jsonify({
        "command_id": command_id,
        "message": "Restartuję panel i bridge - Docker podniesie oba kontenery ponownie "
                   "(restart: unless-stopped) w ciągu kilku-kilkunastu sekund. Ta strona "
                   "przestanie odpowiadać na chwilę.",
    })


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
