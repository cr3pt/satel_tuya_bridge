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
import io
import logging
import os
import pathlib
import sqlite3
import threading
import time
import zipfile

import yaml
from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException, InternalServerError
from werkzeug.security import generate_password_hash

from bridge.config import BridgeConfig
from bridge.config_store import ConfigStore, resolve_db_and_legacy_paths
from bridge.log_capture import attach_log_capture
from bridge.logging_config import apply_log_levels, VALID_LEVELS as LOG_LEVELS
from bridge.history_store import HistoryStore
from bridge.satel_client import test_connection as test_satel_connection
from bridge.satel_client import test_output as test_satel_output
from bridge.satel_client import test_virtual_input_http as test_satel_virtual_input_http
from bridge.satel_client import DEFAULT_VIRTUAL_INPUT_HTTP_PORT, DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE
from bridge.satel_client import discover_active_zones_and_outputs
from bridge.tuya_local import scan_network as scan_tuya_network, fetch_cloud_devices as fetch_tuya_cloud_devices
from dashboard.auth import AuthGuard, require_auth, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from dashboard.setup import (
    build_config_dict, validate, build_automations_dict, validate_automations,
    build_satel_fields_dict, build_tuya_devices_dict,
    build_connections_patch, validate_connections, connections_to_form, list_plugins,
    build_location_patch, validate_location,
    draft_path_for, load_draft, save_draft, delete_draft,
)

app = Flask(__name__)
# "bridge.dashboard" (nie "dashboard.app"/__name__) - żeby trafiał pod
# logger-rodzica "bridge" i był przechwytywany przez HistoryLogHandler
# (patrz bridge/log_capture.py: attach_log_capture, podpięte pod logger
# "bridge" w create_app() niżej) - widoczny w zakładce ⚙ Ustawienia ogólne
# -> Logi, komponent "panel".
logger = logging.getLogger("bridge.dashboard")
history: HistoryStore | None = None  # ustawiane w create_app()
config_store: ConfigStore | None = None  # ustawiane w create_app() - baza z dokumentami config/sensors/automations/satel_fields/tuya_devices
auth_guard: AuthGuard | None = None  # ustawiane w create_app()
satel_partitions_cfg: dict = {}  # nazwa -> numer strefy, ustawiane w create_app()
config_raw: dict = {}  # ostatnio wczytany config.raw (do odczytu automatyzacji/kontekstu), ustawiane w create_app()
config_path: pathlib.Path | None = None  # ustawiane w main() - ścieżka do bazy config.db, potrzebna, zanim w ogóle istnieje


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    """Bez tego NIEOCZEKIWANY wyjątek w dowolnym endponcie /api/* (np. zły
    format liczby w jednym z wielu pól formularza kreatora - patrz walidacja
    w dashboard/setup.py, która nie sprawdza WSZYSTKICH pól) leciałby przez
    domyślny handler Flaska, który zwraca stronę HTML z kodem 500 - a JS
    panelu wszędzie robi `await resp.json()`, więc taka odpowiedź kończy się
    kompletnie nieczytelnym `SyntaxError: Unexpected token '<'` w
    przeglądarce, bez żadnej wskazówki, co faktycznie poszło nie tak.
    Strony HTML (nie /api/*) zostają nietknięte - tam strona błędu Flaska
    ma sens.

    UWAGA: dla stron spoza /api/* NIE wolno zrobić tu `raise exc` - to
    NIE przywraca domyślnej obsługi Flaska (np. poprawnej strony 404 dla
    NotFound), bo jesteśmy już WEWNĄTRZ zarejestrowanego errorhandlera;
    ponownie rzucony wyjątek ucieka z całego pipeline'u Flaska i
    zamienia się w generyczny status 500, NIEZALEŻNIE od oryginalnego
    kodu (np. zwykłe 404 na /favicon.ico stawało się w logach "500
    Internal Server Error" - zaobserwowane w praktyce). Zamiast tego
    trzeba samodzielnie zbudować odpowiedź, jaką Flask dałby domyślnie:
    `exc.get_response()` dla HTTPException (poprawny status/treść), a
    dla nieoczekiwanych wyjątków - generyczne 500 Werkzeuga."""
    if not request.path.startswith("/api/"):
        if isinstance(exc, HTTPException):
            return exc.get_response()
        logger.exception("Nieoczekiwany błąd serwera (%s %s)", request.method, request.path)
        return InternalServerError().get_response()
    if isinstance(exc, HTTPException):
        return jsonify({"error": exc.description or exc.name}), exc.code
    if isinstance(exc, (PermissionError, sqlite3.OperationalError)):
        # Najczęstsza przyczyna w Dockerze: wolumen z danymi (./data ->
        # /config) nie jest zapisywalny dla użytkownika appuser (UID 1000),
        # bo pominięto `chown 1000:1000 data` przed pierwszym uruchomieniem
        # (patrz README, sekcja "Uruchomienie w Dockerze") - bez tej
        # gałęzi leciałoby jako nieczytelny "nieoczekiwany błąd serwera".
        logger.exception("Nieoczekiwany błąd serwera (%s %s)", request.method, request.path)
        return jsonify({
            "error": "Brak dostępu do katalogu/pliku z konfiguracją (uprawnienia systemu plików). "
                     "W Dockerze: katalog danych (./data, montowany jako /config) musi być "
                     "zapisywalny dla użytkownika o UID 1000 - uruchom na hoście "
                     "`sudo chown -R 1000:1000 data` i zrestartuj kontenery "
                     "(patrz README, sekcja \"Uruchomienie w Dockerze\").",
        }), 500
    logger.exception("Nieoczekiwany błąd serwera (%s %s)", request.method, request.path)
    return jsonify({
        "error": "Wystąpił nieoczekiwany błąd serwera. Sprawdź dziennik logów "
                 "(zakładka ⚙ Ustawienia ogólne -> Logi, komponent \"panel\") po szczegóły.",
    }), 500


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
    # Konfiguracja istnieje albo jako baza config.db, albo (stara instalacja
    # jeszcze niezmigrowana) jako config.yaml obok niej - BridgeConfig.load()
    # migruje automatycznie przy pierwszym użyciu (patrz bridge/config.py).
    if config_path is None:
        return False
    db_path, legacy_yaml_path = resolve_db_and_legacy_paths(config_path)
    return db_path.exists() or legacy_yaml_path.exists()


def create_app(config: BridgeConfig) -> Flask:
    global history, config_store, auth_guard, satel_partitions_cfg, config_raw
    db_path = config.raw.get("dashboard", {}).get("db_path", "bridge_history.db")
    history = HistoryStore(db_path)
    config_store = config.store
    # NIE tworzymy nowego AuthGuard, jeśli już istnieje - create_app() jest
    # wołane ponownie po KAŻDYM zapisie configu z panelu (patrz wywołania
    # niżej w tym module). Sesje przetrwałyby nawet nowy obiekt (token sam
    # niesie wszystko, sekret podpisujący jest trwały w bazie - patrz
    # obszerny docstring dashboard/auth.py), ale nowy obiekt zgubiłby
    # liczniki nieudanych prób logowania/listę jawnie wylogowanych tokenów
    # trzymane w pamięci procesu (patrz docstring AuthGuard.update_config) -
    # stąd reużycie zamiast tworzenia od nowa.
    if auth_guard is None:
        auth_guard = AuthGuard(config_store, config.raw.get("dashboard", {}).get("auth"))
    else:
        auth_guard.update_config(config_store, config.raw.get("dashboard", {}).get("auth"))
    satel_partitions_cfg = config.satel.get("partitions", {"Dom": config.satel.get("partition", 1)})
    config_raw = config.raw
    apply_log_levels(config_raw)
    attach_log_capture(history, "dashboard")
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


# _dict_to_name_number_rows/_zones_to_rows/_virtual_inputs_to_rows/
# _tuya_devices_to_rows/_zigbee_gateways_to_rows przeniesione do
# dashboard/setup.py (patrz import wyżej) - żeby plugins/satel/routes.py,
# plugins/tuya_local/routes.py i plugins/ebus/routes.py mogły z nich
# korzystać bez importowania tego modułu (dashboard.app), co dałoby cykl
# importów (ten moduł rejestruje blueprinty tych wtyczek).


def _reload_bridge_config_from_disk() -> None:
    """Ponownie wczytuje config.yaml do TEGO procesu (panel) - używane przez
    wtyczki po zapisie własnych plików (satel_fields.yaml/tuya_devices.yaml/
    itd.), żeby np. `config_raw` widziane przez inne endpointy od razu
    odzwierciedlało zmianę, bez czekania na restart panelu."""
    create_app(BridgeConfig.load(config_path))


# Rejestracja routingu wtyczek z WŁASNĄ stroną konfiguracji (plugins/<id>/) -
# patrz plugins/__init__.py po wyjaśnienie wzorca wstrzykiwania zależności
# zamiast `import dashboard.app` (cykl importów + duplikat modułu pod
# `python -m dashboard.app`). Jinja musi dodatkowo umieć znaleźć szablony
# leżące w plugins/<id>/templates/ (nie w domyślnym templates/ tego
# modułu) - stąd ChoiceLoader niżej.
from jinja2 import ChoiceLoader, FileSystemLoader
import plugins as _plugins

_plugin_metas = _plugins.register_all(
    app,
    require_auth=require_auth,
    get_auth_guard=lambda: auth_guard,
    get_config_raw=lambda: config_raw,
    get_config_store=lambda: config_store,
    get_config_path=lambda: config_path,
    reload_bridge_config=_reload_bridge_config_from_disk,
    enqueue_bridge_reload=_enqueue_bridge_reload,
)
app.jinja_loader = ChoiceLoader([
    app.jinja_loader,
    FileSystemLoader([str(pathlib.Path(__file__).resolve().parent.parent / "plugins" / meta.id / "templates")
                       for meta in _plugin_metas]),
])


@app.before_request
def _redirect_to_setup_if_unconfigured():
    if request.endpoint in _SETUP_EXEMPT_ENDPOINTS:
        return
    if not is_configured():
        return redirect(url_for("setup_page"))


# Zalogowanie jest wymagane dla WSZYSTKIEGO - łącznie z samym oglądaniem
# strony głównej (status na żywo, historia, dziennik zdarzeń, alarmy
# progowe). Wcześniej strona główna (podgląd, bez ustawień/sterowania)
# była celowo publiczna - na wyraźną prośbę zarzucone na rzecz zasady
# "przed zalogowaniem nic nie jest widoczne".
#
# Zaprojektowane jako DOMYŚLNIE ZABRONIONE (allowlist rzeczy jawnie
# publicznych) zamiast odwrotnie (lista rzeczy do ochrony) - żeby nowy
# endpoint dodany w przyszłości (w tym w KAŻDEJ wtyczce pod plugins/) był
# bezpieczny z definicji, bez pamiętania o dopisaniu go do jakiejś listy.
# Publiczne zostaje TYLKO to, co musi działać PRZED zalogowaniem: sama
# strona/mechanika logowania, kreator pierwszej konfiguracji (nie ma
# jeszcze żadnego konta, więc nie da się tego zgate'ować logowaniem),
# statyka (CSS/JS/logo/favicon - potrzebne, żeby w ogóle wyrenderować
# stronę logowania) i healthcheck (zewnętrzny monitoring typu
# uptime-kuma/systemd nie wysyła poświadczeń panelu).
_PUBLIC_PATHS = {
    "/login", "/api/login", "/api/logout", "/api/auth/status",
    "/setup", "/api/setup/draft", "/api/setup/test-satel-connection", "/api/setup/save",
    "/api/discover/satel", "/api/discover/tuya", "/api/discover/tuya-cloud",
    "/healthz",
}
_PUBLIC_PATH_PREFIXES = ("/static/",)


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PATH_PREFIXES)


@app.before_request
def _require_login_for_settings_and_control():
    if request.endpoint is None:
        return  # ścieżka niedopasowana do żadnej trasy - niech Flask zwróci normalne 404
    if _is_public_path(request.path):
        return
    if not is_configured() or auth_guard is None:
        return  # _redirect_to_setup_if_unconfigured() wyżej i tak to obsłuży
    ok, _message = auth_guard.check()
    if ok:
        return
    if request.path.startswith("/api/"):
        response = jsonify({
            "error": "Wymagane zalogowanie - ta treść jest dostępna tylko dla zalogowanych administratorów panelu",
        })
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="Panel administracyjny"'
        return response
    return redirect(url_for("login_page", next=request.path))


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
    username = payload.get("username", "")
    password = payload.get("password", "")
    ok, message = auth_guard.check_password(username, password)
    if not ok:
        return jsonify({"error": message}), 401

    token = auth_guard.create_session(username)
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
    nic nie ujawnia poza tym, czy TA przeglądarka ma ważną sesję i (jeśli
    tak) jaka nazwa konta jest nią zalogowana. Wiele kont administratorskich
    (patrz bridge/config_store.py: tabela users) - nie ma już jednej "tej"
    nazwy użytkownika do podpowiedzenia przed zalogowaniem."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    logged_in = auth_guard.check_session(token)
    return jsonify({
        "logged_in": logged_in,
        "configured": auth_guard.configured,
        "username": auth_guard.session_username(token) if logged_in else None,
    })


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
    # Opcjonalny klucz szyfrowania transmisji (AES, patrz obszerny
    # docstring bridge/satel_client.py) - podany WPROST w formularzu
    # kreatora (PRZED zapisem configu, stąd nie ma go jeszcze skąd wziąć z
    # zapisanej konfiguracji). Test i tak jest publiczny (host/port też
    # przychodzą wprost w ciele żądania), więc to nie zwiększa ryzyka.
    # Przełącznik "encryption_enabled" gate'uje klucz - patrz analogiczny
    # komentarz w bridge/rules_engine.py, dlaczego sam fakt podania klucza
    # nie wystarcza.
    encryption_key = (payload.get("encryption_key") or "").strip() or None
    if not payload.get("encryption_enabled"):
        encryption_key = None

    try:
        asyncio.run(test_satel_connection(host, port, encryption_key=encryption_key))
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


@app.route("/api/satel/test-output", methods=["POST"])
@require_auth()
def api_satel_test_output():
    """Przycisk "TEST" obok pola klucza integracji (zakładka ⚙ Ustawienia ->
    Połączenia) - jednorazowo wyzwala wyjście/wirtualne wejście IP, tak jak
    JUŻ ZAPISANY tryb (satel.virtual_inputs.mode, patrz obszerny docstring
    bridge/satel_client.py) - świadomie BEZ osobnego pola klucza integracji
    w tym żądaniu (nie duplikujemy go). Wymaga zalogowania - w odróżnieniu
    od "Testuj połączenie" wyżej, TO polecenie faktycznie steruje wyjściem/
    wejściem centrali (nie tylko sprawdza osiągalność sieciową).

    Tryb "library" (domyślny): body {output_number, state} - set_output
    przez połączenie integracji, wymaga zapisanego klucza integracji.
    Tryb "http": body {name, state} - `name` to nazwa JUŻ SKONFIGUROWANEGO
    wirtualnego wejścia (satel.virtual_inputs.inputs), użyta WPROST jako
    fragment ścieżki URL - zwykły, NIEUWIERZYTELNIONY GET, BEZ żadnego
    klucza/kodu DLOADX (potwierdzone na prawdziwym sprzęcie - patrz
    obszerny docstring bridge/satel_client.py).

    UWAGA: czytamy sekcję "satel" ŚWIEŻO z config_store, NIE z config_raw
    (to migawka configu sprzed startu procesu, aktualizowana tylko przy
    pełnym restarcie) - inaczej zapisanie nowego trybu/portu wejść IP w
    sekcji 🔌 Połączenie i od razu kliknięcie TEST wciąż testowałoby
    STARY tryb, dając mylący błąd z niewłaściwej gałęzi (np. "Podaj numer
    wyjścia" mimo wybranego i zapisanego trybu HTTP).

    UWAGA 2: nazwane wirtualne wejścia (satel.virtual_inputs.inputs) po
    zapisaniu przez /api/plugins/satel/fields żyją w OSOBNYM dokumencie
    "satel_fields", NIE w "config" (tam zostaje tylko to, co ktoś wpisał
    wprost w config.yaml, ewentualnie puste - patrz build_satel_connection_patch,
    który tego pola nigdy nie dotyka). Branie ich TYLKO z config_store.get("config")
    dawało więc "Wybierz skonfigurowane wirtualne wejście" nawet dla
    NAPRAWDĘ skonfigurowanego wejścia. Odtwarzamy niżej DOKŁADNIE ten sam
    wzorzec scalania co BridgeConfig.load (bridge/config.py): "satel_fields"
    nadpisuje "inputs" TYLKO jeśli faktycznie ma ten klucz, inaczej pod uwagę
    idzie to, co jest wprost w "config"."""
    satel_cfg = config_store.get("config").get("satel", {})
    host = (satel_cfg.get("host") or "").strip()
    port = satel_cfg.get("port")
    if not host or not port:
        return jsonify({"error": "Najpierw zapisz adres IP i port centrali (sekcja 🔌 Połączenie, /plugins/satel)"}), 400

    vi_cfg = satel_cfg.get("virtual_inputs", {})
    mode = vi_cfg.get("mode", "library")
    payload = request.get_json(silent=True) or {}
    state = bool(payload.get("state", True))

    if mode == "http":
        name = (payload.get("name") or "").strip()
        # Ten sam wzorzec scalania co BridgeConfig.load (bridge/config.py):
        # dokument "satel_fields" nadpisuje "inputs" TYLKO jeśli faktycznie
        # ma ten klucz - inaczej (np. świeża instalacja bez satel_fields.yaml,
        # wejścia wpisane wprost w config.yaml) trzeba wziąć to, co jest w
        # samej sekcji "satel" dokumentu "config".
        satel_fields = config_store.get("satel_fields")
        inputs = satel_fields["virtual_inputs"] if "virtual_inputs" in satel_fields else vi_cfg.get("inputs", {})
        if name not in inputs:
            return jsonify({"error": "Wybierz skonfigurowane wirtualne wejście"}), 400
        vi_port = vi_cfg.get("port", DEFAULT_VIRTUAL_INPUT_HTTP_PORT)
        # CELOWO stała, NIE vi_cfg.get("url_template", ...) - to szczegół
        # protokołu, żaden formularz go nie edytuje. Czytanie zapisanej
        # wartości groziło (i groziło naprawdę - zgłoszony błąd, KeyError:
        # 'key') zamrożeniem STAREGO formatu URL w instalacjach, które
        # zapisały połączenie PRZED poprawką protokołu.
        try:
            asyncio.run(test_satel_virtual_input_http(host, int(vi_port), DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE, name))
        except ConnectionError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"Błąd testu wejścia IP (http): {exc}"}), 502
        return jsonify({
            "message": f"Wysłano żądanie HTTP dla wejścia '{name}' (tryb http) - "
                       "ETHM-1 nie potwierdza odbioru, brak wyjątku sieciowego = wysłano.",
        })

    integration_key = satel_cfg.get("integration_key") or ""
    if not integration_key:
        return jsonify({"error": "Najpierw zapisz klucz integracji (sekcja 🔌 Połączenie, /plugins/satel)"}), 400
    try:
        output_number = int(payload.get("output_number"))
    except (TypeError, ValueError):
        return jsonify({"error": "Podaj numer wyjścia (1-256)"}), 400
    if not (1 <= output_number <= 256):
        return jsonify({"error": "Numer wyjścia musi być w zakresie 1-256"}), 400

    try:
        asyncio.run(test_satel_output(
            host, int(port), integration_key, output_number, state,
            encryption_key=satel_cfg.get("encryption_key") if satel_cfg.get("encryption_enabled") else None,
        ))
    except ImportError:
        return jsonify({"error": "Biblioteka satel_integra nie jest zainstalowana na serwerze"}), 500
    except (ConnectionError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd testu wyjścia: {exc}"}), 502

    return jsonify({
        "message": f"Wyjście {output_number} ustawione na "
                    f"{'włączone' if state else 'wyłączone'} - centrala potwierdziła komendę.",
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
    # "encryption_enabled" z ciała żądania (kreator, PRZED zapisem configu)
    # ma pierwszeństwo, jeśli podane - w przeciwnym razie liczy się już
    # zapisany przełącznik (patrz analogiczny komentarz w
    # bridge/rules_engine.py, dlaczego sam fakt istnienia klucza nie
    # wystarcza).
    encryption_enabled = (
        payload["encryption_enabled"] if "encryption_enabled" in payload
        else satel_cfg.get("encryption_enabled")
    )
    encryption_key = (payload.get("encryption_key") or satel_cfg.get("encryption_key") or None) if encryption_enabled else None

    try:
        result = asyncio.run(discover_active_zones_and_outputs(
            host, port, timeout_seconds=timeout_seconds, encryption_key=encryption_key,
        ))
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

    # `validate()` sprawdza WYMAGANE pola (niepuste), ale świadomie nie
    # sprawdza formatu WSZYSTKICH pól liczbowych (numery stref/wyjść/wejść,
    # porty, czas trwania w scenach/regułach ruchu, szerokość/długość
    # geograficzna itd.) - jest ich zbyt wiele, żeby dublować to osobno w
    # dwóch miejscach. Builder (int()/float()) rzuciłby więc ValueError na
    # np. wpisaną literką zamiast liczby, co bez tego try/except leciałoby
    # jako nieobsłużony wyjątek -> HTML 500 zamiast JSON-a, którego oczekuje
    # JS (patrz handle_unexpected_error wyżej - to i tak złapałby wyjątek,
    # ale tu dajemy dokładniejszy, faktycznie pomocny komunikat).
    try:
        config_dict = build_config_dict(form)
        automations_dict = build_automations_dict(form)
        satel_fields_dict = build_satel_fields_dict(form)
        tuya_devices_dict = build_tuya_devices_dict(form)
    except (ValueError, TypeError) as exc:
        logger.warning("Błąd budowania configu z formularza kreatora: %s", exc)
        return jsonify({
            "error": "Popraw błędy:",
            "details": [
                f"Jedno z pól liczbowych (numer strefy/wyjścia/wejścia, port, czas trwania, "
                f"współrzędne itd.) ma nieprawidłowy format: {exc}",
            ],
        }), 400

    # Pierwszy zapis tworzy plik bazy config.db pod ścieżką z --config (patrz
    # main()) - wszystkie "dokumenty" (config/sensors/automations/
    # satel_fields/tuya_devices) trafiają do TEJ SAMEJ bazy SQLite zamiast
    # osobnych plików .yaml (patrz bridge/config_store.py po uzasadnienie).
    # resolve_db_and_legacy_paths (nie config_path wprost) - ta sama
    # normalizacja co main()/BridgeConfig.load, na wypadek gdyby
    # config_path wciąż kończył się na .yaml (patrz komentarz w main()).
    db_path, _ = resolve_db_and_legacy_paths(config_path)
    store = ConfigStore(db_path)
    store.set("config", config_dict)
    store.set("sensors", {"sensors": []})
    store.set("automations", automations_dict)
    store.set("satel_fields", satel_fields_dict)
    store.set("tuya_devices", tuya_devices_dict)

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
    token = auth_guard.create_session(config_dict["dashboard"]["auth"]["username"])
    resp.set_cookie(SESSION_COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="Lax")
    return resp


@app.route("/api/config/sensors", methods=["GET"])
def api_config_sensors_get():
    """Metadane czujników (etykiety/tematy MQTT) dla panelu głównego
    (index.html) - jak reszta /api/*, chronione globalnie przez
    _require_login_for_settings_and_control (allowlist wyżej), mimo braku
    @require_auth() wprost na tym routingu. Zapis przeniesiony do
    /api/plugins/ebus/sensors (patrz zgłoszenie: rozdzielić dawną wspólną
    zakładkę Ustawienia/Połączenia na właściwe strony wtyczek)."""
    return jsonify(config_store.get("sensors").get("sensors", []))


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


@app.route("/api/config/location", methods=["GET"])
@require_auth()
def api_config_location_get():
    """Współrzędne do liczenia wschodu/zachodu słońca (bridge/scheduling.py,
    okna czasowe "sunset"/"sunrise" w HARMONOGRAM reguł ruchu) - zakładka
    🔌 Połączenia, sekcja "Lokalizacja". W odróżnieniu od reszty pól tej
    zakładki (host/port/klucze) NIE wymaga restartu procesu po zapisie -
    patrz POST niżej i RulesEngine.reload_config."""
    return jsonify(config_raw.get("location") or {})


@app.route("/api/config/location", methods=["POST"])
@require_auth()
def api_config_location_post():
    form = request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate_location(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    current = config_store.get("config")
    new_config = build_location_patch(form, current)
    config_store.set("config", new_config)

    config = BridgeConfig.load(config_path)
    create_app(config)
    _enqueue_bridge_reload()

    return jsonify({"message": _RELOAD_MESSAGE})


@app.route("/api/config/export-yaml", methods=["GET"])
@require_auth()
def api_config_export_yaml():
    """Cała konfiguracja żyje w bazie config.db (patrz bridge/config_store.py),
    nie w plikach .yaml - ten endpoint to furtka bezpieczeństwa: backup,
    ręczna inspekcja, migracja między instalacjami. Zwraca JSON
    {"config.yaml": "...", "satel_fields.yaml": "...", ...} - rekonstrukcję
    dokładnie tych plików, które istniały przed migracją na bazę."""
    return jsonify(config_store.export_yaml_bundle())


@app.route("/api/config/import-yaml", methods=["POST"])
@require_auth()
def api_config_import_yaml():
    """Odwrotność eksportu wyżej - nadpisuje wskazane dokumenty w bazie
    zawartością przesłanych plików YAML (ten sam kształt, jaki zwraca
    export-yaml). Nierozpoznane nazwy plików są pomijane. Zmiany połączeń
    (host/klucze) wymagają pełnego restartu - patrz /api/system/restart."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        return jsonify({"error": "Oczekiwano {'nazwa_pliku.yaml': 'tekst_yaml', ...}"}), 400
    try:
        config_store.import_yaml_bundle(payload)
    except yaml.YAMLError as exc:
        return jsonify({"error": f"Błąd parsowania YAML: {exc}"}), 400

    config = BridgeConfig.load(config_path)
    create_app(config)
    _enqueue_bridge_reload()

    return jsonify({
        "imported": list(payload.keys()),
        "message": "Zaimportowano. Zmiany połączeń (host/klucze) wymagają pełnego restartu - "
                    "użyj przycisku restartu w zakładce Połączenia.",
    })


@app.route("/api/config/export-zip", methods=["GET"])
@require_auth()
def api_config_export_zip():
    """Jak export-yaml wyżej, ale jako prawdziwe archiwum .zip (pliki
    .yaml w środku, do rozpakowania/wglądu dowolnym programem) zamiast
    jednego pliku JSON z zaszytym tekstem YAML w wartościach - wygodniejsze
    jako format kopii zapasowej "do ręki"."""
    bundle = config_store.export_yaml_bundle()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in bundle.items():
            zf.writestr(filename, content)
    buf.seek(0)
    download_name = f"satel-tuya-bridge-config-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=download_name)


@app.route("/api/config/import-zip", methods=["POST"])
@require_auth()
def api_config_import_zip():
    """Odwrotność export-zip wyżej - PODWÓJNIE sprawdza importowaną
    konfigurację PRZED zapisem (patrz bridge/config_store.py:
    import_yaml_bundle_validated - składnia YAML + realny start procesu
    na tymczasowej kopii bazy), więc uszkodzone/niekompletne archiwum jest
    odrzucane w całości, zanim cokolwiek trafi do prawdziwej bazy."""
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Nie przesłano pliku .zip"}), 400

    try:
        with zipfile.ZipFile(io.BytesIO(uploaded.read())) as zf:
            files = {
                name: zf.read(name).decode("utf-8")
                for name in zf.namelist()
                if name.endswith(".yaml") and not name.endswith("/")
            }
    except zipfile.BadZipFile:
        return jsonify({"error": "Przesłany plik nie jest poprawnym archiwum .zip"}), 400

    try:
        imported = config_store.import_yaml_bundle_validated(files)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    config = BridgeConfig.load(config_path)
    create_app(config)
    _enqueue_bridge_reload()

    return jsonify({
        "imported": imported,
        "message": "Zaimportowano (zweryfikowano składnię i start procesu przed zapisem). "
                    "Zmiany połączeń (host/klucze) wymagają pełnego restartu - "
                    "użyj przycisku restartu w zakładce Połączenia.",
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

# Wtyczki, które mają już własną, dedykowaną stronę konfiguracji pod
# /plugins/<id>/ (patrz plugins/__init__.py) - tu przełączamy WYŁĄCZNIE pole
# "enabled" w ich sekcji config.yaml, bez dotykania reszty ustawień (host/
# klucze zostają, jak są). Zawsze wymaga pełnego restartu procesu - żywe
# połączenie sieciowe (TCP do centrali/MQTT/itd.) startuje tylko raz, przy
# starcie RulesEngine (patrz rules_engine.py: start()).
_TOGGLE_ONLY_PLUGIN_IDS = {"satel", "tuya_local", "ebus", "unifi_protect", "homewhiz", "notifications"}
_TOGGLE_ONLY_CONFIG_KEY = {
    "satel": "satel", "tuya_local": "tuya", "ebus": "ebus",
    "unifi_protect": "unifi_protect", "homewhiz": "homewhiz", "notifications": "notifications",
}


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
    mqtt_publish/presence_simulation/satel/tuya_local/ebus/unifi_protect/
    notifications) albo interwał odpytywania (satel/tuya_local) wskazanych
    wtyczek (patrz _CONNECTIONS_PLUGIN_IDS_RESTART/_CONNECTIONS_PLUGIN_IDS_LIVE/
    _AUTOMATIONS_PLUGIN_IDS/_TOGGLE_ONLY_PLUGIN_IDS wyżej) - reszta ustawień
    danej integracji (host/klucze/urządzenia/itd.) zostaje nietknięta. Do
    PEŁNEJ konfiguracji (np. pierwsze podanie poświadczeń Tuya Cloud czy
    hosta UniFi Protect) wciąż trzeba przejść do właściwej zakładki (własna
    strona wtyczki / 🔌 Połączenia / 🎬 Automatyzacje) - to przełącznik/
    regulator, nie edytor."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Nieprawidłowe dane"}), 400

    unknown = set(body) - _CONNECTIONS_PLUGIN_IDS - _AUTOMATIONS_PLUGIN_IDS - _TOGGLE_ONLY_PLUGIN_IDS
    if unknown:
        return jsonify({"error": f"Nieznane/nieprzełączalne wtyczki: {', '.join(sorted(unknown))}"}), 400

    saved = {}

    toggle_only_body = {
        k: v for k, v in body.items()
        if k in _TOGGLE_ONLY_PLUGIN_IDS and (v or {}).get("enabled") is not None
    }
    if toggle_only_body:
        current = config_store.get("config")
        for plugin_id, patch in toggle_only_body.items():
            config_key = _TOGGLE_ONLY_CONFIG_KEY[plugin_id]
            if current.get(config_key) is None:
                return jsonify({
                    "error": f"Najpierw skonfiguruj wtyczkę '{plugin_id}' w jej zakładce, zanim ją włączysz.",
                }), 400
            current[config_key]["enabled"] = bool(patch.get("enabled"))
        config_store.set("config", current)
        saved.update(toggle_only_body)

    connections_body = {k: v for k, v in body.items() if k in _CONNECTIONS_PLUGIN_IDS}
    if connections_body:
        current = config_store.get("config")
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
        config_store.set("config", new_config)
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
        config_store.set("automations", automations_dict)
        saved.update(automations_body)

    config = BridgeConfig.load(config_path)
    create_app(config)
    # Interwały odpytywania (satel/tuya_local) i symulacja obecności działają
    # na żywo przez tę samą kolejkę komend co reszta panelu - w odróżnieniu
    # od włączenia/wyłączenia integracji, które nadal wymaga restartu.
    live_connections_ids = set(connections_body) & _CONNECTIONS_PLUGIN_IDS_LIVE
    if automations_body or live_connections_ids:
        _enqueue_bridge_reload()

    restart_ids = (set(connections_body) & _CONNECTIONS_PLUGIN_IDS_RESTART) | set(toggle_only_body)
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
    urządzeń Tuya, wirtualnych wejść i stref Satela już skonfigurowanych w
    config.yaml (panel /automations tylko ODCZYTUJE te rzeczy, nie edytuje
    - edycja urządzeń/wejść/stref zostaje ręczna w config.yaml/zakładce
    Wtyczki, patrz README). Publiczny - same nazwy nie są wrażliwe (bez
    local_key/kluczy).

    "tuya_devices" łączy urządzenia lokalne (tuya.devices) I chmurowe
    (tuya.cloud_devices, zakładka ☁ Tuya Cloud) - obie sterowane przez
    ten sam TuyaController (bridge/tuya_dispatch.py), więc oba rodzaje
    muszą dać się wybrać jako cel sceny/reguły ruchu, inaczej urządzenia
    sterowane wyłącznie przez chmurę nigdy nie dałoby się użyć w
    automatyzacji mimo że REALNIE da się nim sterować."""
    tuya = config_raw.get("tuya", {})
    tuya_devices = sorted(set(tuya.get("devices", {})) | set(tuya.get("cloud_devices", {})))
    virtual_inputs = list(config_raw.get("satel", {}).get("virtual_inputs", {}).get("inputs", {}).keys())
    satel_partitions = config_raw.get("satel", {}).get("partitions", {})
    # Klucze statusów (wejścia/wyjścia Satela, Tuya, kocioł...) - do budowy
    # warunków typu "state" (patrz bridge/conditions.py) w edytorze reguł:
    # użytkownik wybiera z listy zamiast pamiętać dokładny klucz jak
    # "satel_input:3". Nazwy reguł ruchu - do wyboru celu "wyzwól kolejną
    # regułę" (then_trigger_rule, patrz RulesEngine._chain_trigger).
    status_keys = [
        {"key": s["key"], "label": s["label"], "category": s["category"]}
        for s in history.get_status()
    ]
    motion_rule_names = [
        r["name"] for r in config_raw.get("reguły_ruchu", []) if r.get("name")
    ]
    # Urządzenia ze ściemniaczem (tuya.devices[].type == "dimmer") - edytor
    # pokazuje pole "Jasność %" TYLKO dla takich urządzeń, żeby nie sugerować
    # ściemniania na zwykłym gniazdku/przekaźniku. Tylko urządzenia LOKALNE
    # mogą mieć ten typ (chmurowe sterowanie jest wyłącznie on/off - patrz
    # TuyaCloudController).
    dimmer_devices = sorted(
        name for name, cfg in tuya.get("devices", {}).items() if cfg.get("type") == "dimmer"
    )
    # Nazwy skonfigurowanych wyjść Satela (zakładka ⚙ Ustawienia -> Satel,
    # satel.monitored_outputs: {nazwa: numer}) - do wyboru jako "system:
    # Satel (wyjście)" celu akcji reguły ruchu, obok urządzeń Tuya.
    satel_outputs = sorted(config_raw.get("satel", {}).get("monitored_outputs", {}).keys())
    # Nazwane wejścia Satela (satel.monitored_zones: {nazwa: numer}) - do
    # wyboru "Numer wejścia w Satelu" z LISTY skonfigurowanych wejść,
    # zamiast wpisywania gołego numeru ręcznie (ten sam powód co
    # satel_outputs wyżej - lista już istnieje w ⚙ Ustawienia, więc trzeba
    # z niej skorzystać zamiast prosić o numer na pamięć).
    satel_zones = sorted(
        ({"name": name, "number": number}
         for name, number in config_raw.get("satel", {}).get("monitored_zones", {}).items()),
        key=lambda z: z["name"],
    )
    # Nazwy skonfigurowanych urządzeń HomeWhiz (zakładka 🧩 Wtyczki ->
    # Zmywarki/AGD HomeWhiz, homewhiz.appliances: {nazwa: {appliance_id}}) -
    # do wyboru jako "system: HomeWhiz" WEJŚCIA reguły ruchu (stan
    # urządzenia jako wyzwalacz), patrz RulesEngine.handle_device_state.
    homewhiz_appliances = sorted((config_raw.get("homewhiz") or {}).get("appliances", {}).keys())
    return jsonify({
        "tuya_devices": tuya_devices, "virtual_inputs": virtual_inputs, "satel_partitions": satel_partitions,
        "status_keys": status_keys, "motion_rule_names": motion_rule_names,
        "dimmer_devices": dimmer_devices, "satel_outputs": satel_outputs, "satel_zones": satel_zones,
        "homewhiz_appliances": homewhiz_appliances,
    })


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

    # Zakładka Automatyzacje nie zna pola "log_level" (żyje tylko na
    # dedykowanej stronie /plugins/presence_simulation - patrz
    # plugins/presence_simulation/routes.py) - trzeba je jawnie przenieść z
    # tego, co już zapisane, inaczej KAŻDY zapis stąd (nawet samych scen)
    # po cichu skasowałby wcześniej ustawiony poziom logowania.
    existing_log_level = config_store.get("automations").get("presence_simulation", {}).get("log_level")
    if existing_log_level and isinstance(form.get("presence_simulation"), dict):
        form["presence_simulation"].setdefault("log_level", existing_log_level)

    automations_dict = build_automations_dict(form)
    config_store.set("automations", automations_dict)

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


@app.route("/api/satel/zone-types", methods=["GET"])
def api_satel_zone_types():
    """Publiczny podzbiór /api/config/satel-fields (patrz niżej, teraz
    wymaga zalogowania - zawiera m.in. klucz wirtualnych wejść) - TYLKO
    mapy nazwa -> rodzaj (np. "drzwi"/"ruch"), których panel główny
    (index.html, celowo publiczny - patrz _require_login_for_settings
    poniżej) potrzebuje do ikon/etykiet rodzaju przy statusach wejść/
    wyjść. Same nazwy rodzajów nie są niczym wrażliwym."""
    satel = config_raw.get("satel", {})
    return jsonify({
        "zone_types": satel.get("zone_types", {}),
        "output_types": satel.get("output_types", {}),
    })


@app.route("/api/satel/virtual-inputs", methods=["GET"])
def api_satel_virtual_inputs():
    """Publiczna lista NAZW skonfigurowanych wirtualnych wejść IP (klucze
    satel.virtual_inputs.inputs, zakładka 🧩 Wtyczki -> Satel) - do sekcji
    "Wirtualne wejścia IP" na stronie głównej (index.html). Same nazwy nie
    są wrażliwe (bez numerów wejść/kluczy DLOADX) - analogicznie do
    /api/satel/zone-types wyżej. Faktyczna aktywacja (POST .../trigger)
    wciąż wymaga zalogowania."""
    inputs = config_raw.get("satel", {}).get("virtual_inputs", {}).get("inputs", {})
    return jsonify({"virtual_inputs": sorted(inputs.keys())})


@app.route("/api/satel/partitions")
def api_satel_partitions():
    """Lista nazwanych stref do wyboru w panelu - WYMAGA zalogowania (patrz
    _require_login_for_settings_and_control niżej): sterowanie alarmem
    (i sama sekcja kontrolek na stronie głównej) jest dostępne tylko po
    zalogowaniu, nie tylko samo wysłanie komendy uzbrojenia/rozbrojenia."""
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


@app.route("/api/satel/virtual-inputs/<name>/trigger", methods=["POST"])
@require_auth()
def api_satel_virtual_input_trigger(name):
    """Ręczna aktywacja wirtualnego wejścia IP z panelu głównego - ten sam
    mechanizm co akcja "Aktywuj wejście IP Satela" reguły ruchu (patrz
    RulesEngine._fire_motion_rule), tylko wywołana wprost przez
    użytkownika. Idzie przez tę samą kolejkę komend co uzbrojenie/
    rozbrojenie wyżej (panel to OSOBNY proces bez żywego połączenia z
    Satelem)."""
    inputs = config_raw.get("satel", {}).get("virtual_inputs", {}).get("inputs", {})
    if name not in inputs:
        return jsonify({"error": f"Nieznane wirtualne wejście '{name}'"}), 404
    command_id = history.enqueue_command("satel_virtual_input", {"name": name})
    history.write_event(
        "system", source="dashboard", detail={"message": f"Zlecono aktywację wirtualnego wejścia '{name}'"},
    )
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/commands/<int:command_id>")
def api_command_status(command_id: int):
    """Odczyt statusu wcześniej zleconej komendy (do polowania z frontendu,
    żeby pokazać czy uzbrojenie faktycznie się powiodło) - chronione tak
    samo jak reszta panelu przez globalny hook
    (_require_login_for_settings_and_control), mimo że sam odczyt statusu
    jest nieszkodliwy (nie pozwala niczego zmienić)."""
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


# ============================================================================
# ⚙ Ustawienia ogólne - konta użytkowników, tryb logowania, poziom logów
# domyślny, wgląd w czas serwera. Rzeczy, które NIE są przypisane do żadnej
# konkretnej wtyczki (patrz zakładka 🧩 Wtyczki dla ustawień per integracja).
# ============================================================================

@app.route("/system")
def system_page():
    return render_template("system.html")


@app.route("/api/system/users", methods=["GET"])
@require_auth()
def api_system_users_get():
    """Lista kont administratorskich - nigdy nie zwraca hashy haseł.
    Wymaga zalogowania (jak reszta panelu Ustawień ogólne) - to nie jest
    tajemnica jak samo hasło, ale i tak nie ma po co ujawniać publicznie."""
    return jsonify(config_store.list_users())


@app.route("/api/system/users", methods=["POST"])
@require_auth()
def api_system_users_post():
    """Tworzy nowe konto administratorskie - płaski model uprawnień (jak
    dotychczas): każde konto ma pełny dostęp do zapisu, nie ma ról."""
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username:
        return jsonify({"error": "Nazwa użytkownika jest wymagana"}), 400
    if len(password) < 8:
        return jsonify({"error": "Hasło: minimum 8 znaków"}), 400

    try:
        user_id = config_store.add_user(username, generate_password_hash(password))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"id": user_id, "username": username})


@app.route("/api/system/users/<int:user_id>/password", methods=["POST"])
@require_auth()
def api_system_user_password_post(user_id: int):
    """Zmiana hasła dowolnego konta - płaski model uprawnień, każde
    zalogowane konto może zarządzać każdym innym (tak jak już mogło
    zmieniać KAŻDE ustawienie systemu, nie tylko własne konto)."""
    payload = request.get_json(silent=True) or {}
    password = payload.get("password") or ""
    if len(password) < 8:
        return jsonify({"error": "Hasło: minimum 8 znaków"}), 400
    config_store.update_user(user_id, password_hash=generate_password_hash(password))
    return jsonify({"message": "Hasło zmienione"})


@app.route("/api/system/users/<int:user_id>", methods=["DELETE"])
@require_auth()
def api_system_user_delete(user_id: int):
    """Usuwa konto - NIGDY ostatnie pozostałe (zablokowałoby dostęp do
    panelu dla wszystkich, bez żadnej furtki poza eksportem/importem YAML
    bazy z poziomu dysku)."""
    if config_store.user_count() <= 1:
        return jsonify({"error": "Nie można usunąć ostatniego konta - panel zostałby bez żadnego dostępu"}), 400
    config_store.delete_user(user_id)
    return jsonify({"message": "Konto usunięte"})


@app.route("/api/system/settings", methods=["GET"])
@require_auth()
def api_system_settings_get():
    """Tryb logowania (sesja/Basic Auth, limity prób) i domyślny poziom
    logów - jedyne miejsce w panelu, nieprzypisane do żadnej wtyczki.
    Czas serwera jest tu WYŁĄCZNIE informacyjny (patrz README, sekcja
    'Synchronizacja czasu') - synchronizacja NTP dzieje się na hoście, nie
    da się jej ustawić bezpiecznie z wnętrza kontenera."""
    auth_cfg = config_raw.get("dashboard", {}).get("auth", {})
    return jsonify({
        "max_failed_attempts": auth_cfg.get("max_failed_attempts", 5),
        "lockout_seconds": auth_cfg.get("lockout_seconds", 300),
        "allow_basic_auth": auth_cfg.get("allow_basic_auth", True),
        "session_ttl_seconds": SESSION_TTL_SECONDS,
        "default_log_level": (config_raw.get("logging") or {}).get("default_level", "INFO"),
        "log_levels": LOG_LEVELS,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "timezone_env": os.environ.get("TZ", "(nieustawiona - domyślna kontenera/hosta)"),
    })


@app.route("/api/system/settings", methods=["POST"])
@require_auth()
def api_system_settings_post():
    payload = request.get_json(silent=True) or {}
    errors = []
    try:
        max_attempts = int(payload.get("max_failed_attempts", 5))
    except (TypeError, ValueError):
        errors.append("Limit nieudanych prób musi być liczbą")
        max_attempts = 5
    try:
        lockout_seconds = int(payload.get("lockout_seconds", 300))
    except (TypeError, ValueError):
        errors.append("Czas blokady musi być liczbą (sekundy)")
        lockout_seconds = 300
    default_log_level = payload.get("default_log_level", "INFO")
    if default_log_level not in LOG_LEVELS:
        errors.append(f"Nieprawidłowy poziom logów - dozwolone: {', '.join(LOG_LEVELS)}")
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    current = config_store.get("config")
    current.setdefault("dashboard", {}).setdefault("auth", {})
    current["dashboard"]["auth"]["max_failed_attempts"] = max_attempts
    current["dashboard"]["auth"]["lockout_seconds"] = lockout_seconds
    current["dashboard"]["auth"]["allow_basic_auth"] = bool(payload.get("allow_basic_auth", True))
    current.setdefault("logging", {})["default_level"] = default_log_level
    config_store.set("config", current)

    config = BridgeConfig.load(config_path)
    create_app(config)
    _enqueue_bridge_reload()

    return jsonify({"message": "Zapisano. Poziom logów zadziała na żywo w ciągu ok. 1-2s, bez restartu."})


@app.route("/api/system/logs/loggers", methods=["GET"])
@require_auth()
def api_system_logs_loggers_get():
    """Nazwy loggerów WYSTĘPUJĄCYCH w przechwyconych logach (np.
    'bridge.satel') - do wypełnienia rozwijanej listy filtra w /system
    (zamiast pola tekstowego, gdzie trzeba było znać dokładną nazwę).
    Opcjonalny filtr component (bridge|dashboard) - te same nazwy co
    /api/system/logs, więc wybór procesu zawęża też tę listę."""
    component = request.args.get("component") or None
    return jsonify(history.get_log_loggers(component))


@app.route("/api/system/logs", methods=["GET"])
@require_auth()
def api_system_logs_get():
    """Przechwycone logi (bridge + panel), patrz bridge/log_capture.py.
    Filtrowanie: component (bridge|dashboard), logger (pełna nazwa, np.
    'bridge.satel'), level (minimalny - pokazuje też poważniejsze), hours
    (ile ostatnich godzin)."""
    component = request.args.get("component") or None
    logger_name = request.args.get("logger") or None
    level = request.args.get("level") or None
    hours = float(request.args.get("hours", 24))
    limit = int(request.args.get("limit", 300))
    ts_from = time.time() - hours * 3600
    return jsonify(history.get_logs(component, logger_name, level, ts_from, limit))


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
    parser.add_argument("--config", default="config.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    global config_path
    # Normalizacja RAZ, na starcie - jeśli ktoś wciąż poda stary
    # `--config config.yaml` (np. niezaktualizowany systemd unit sprzed
    # migracji na bazę), od tego miejsca w dół cały kod procesu widzi
    # spójnie ścieżkę do config.db (ta sama funkcja, której używa
    # BridgeConfig.load i api_setup_save - musi być identyczna wszędzie).
    config_path, _ = resolve_db_and_legacy_paths(args.config)

    from waitress import serve

    if is_configured():
        create_app(BridgeConfig.load(config_path))
        print(f"Panel dostępny pod http://{args.host}:{args.port}")
    else:
        print(
            f"Brak {config_path} - uruchamiam kreator pierwszej konfiguracji.\n"
            f"Otwórz http://{args.host if args.host != '0.0.0.0' else '<adres-rpi>'}:{args.port}/setup w przeglądarce."
        )

    # threads=8 (nie 2) - uruchamiane w Dockerze, gdzie zasoby nie są tak
    # ciasne jak na gołym RPi Zero W; kilku administratorów/kart przeglądarki
    # może pracować na panelu równocześnie bez kolejkowania żądań.
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
