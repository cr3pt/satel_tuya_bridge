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
from quart import Quart, jsonify, redirect, render_template, request, send_file, url_for
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
    build_connections_patch, validate_connections, connections_to_form, list_plugins, list_plugin_load_errors,
    build_location_patch, validate_location,
    draft_path_for, load_draft, save_draft, delete_draft,
    validate_flows, build_flows,
)

app = Quart(__name__)
logger = logging.getLogger("bridge.dashboard")
history: HistoryStore | None = None
config_store: ConfigStore | None = None
auth_guard: AuthGuard | None = None
satel_partitions_cfg: dict = {}
config_raw: dict = {}
config_path: pathlib.Path | None = None


@app.errorhandler(Exception)
async def handle_unexpected_error(exc: Exception):
    if not request.path.startswith("/api/"):
        if isinstance(exc, HTTPException):
            return exc.get_response()
        logger.exception("Nieoczekiwany błąd serwera (%s %s)", request.method, request.path)
        return InternalServerError().get_response()
    if isinstance(exc, HTTPException):
        return jsonify({"error": exc.description or exc.name}), exc.code
    if isinstance(exc, (PermissionError, sqlite3.OperationalError)):
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


_SETUP_EXEMPT_ENDPOINTS = {
    "setup_page", "api_setup_save", "api_setup_draft", "static",
    "api_setup_test_satel_connection", "api_discover_tuya_cloud", "api_discover_tuya",
    "api_discover_satel",
}


def is_configured() -> bool:
    if config_path is None:
        return False
    db_path, legacy_yaml_path = resolve_db_and_legacy_paths(config_path)
    return db_path.exists() or legacy_yaml_path.exists()


def create_app(config: BridgeConfig) -> Quart:
    global history, config_store, auth_guard, satel_partitions_cfg, config_raw
    db_path = config.raw.get("dashboard", {}).get("db_path", "bridge_history.db")
    history = HistoryStore(db_path)
    config_store = config.store
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
    history.enqueue_command("reload_config", {})


def _reload_bridge_config_from_disk() -> None:
    create_app(BridgeConfig.load(config_path))


from jinja2 import ChoiceLoader, FileSystemLoader
import plugins as _plugins

_plugin_metas, _plugin_load_errors = _plugins.register_all(
    app,
    require_auth=require_auth,
    get_auth_guard=lambda: auth_guard,
    get_config_raw=lambda: config_raw,
    get_config_store=lambda: config_store,
    get_config_path=lambda: config_path,
    reload_bridge_config=_reload_bridge_config_from_disk,
    enqueue_bridge_reload=_enqueue_bridge_reload,
    get_history=lambda: history,
    get_satel_partitions=lambda: satel_partitions_cfg,
)
if _plugin_load_errors:
    for _err in _plugin_load_errors:
        logger.warning("Wtyczka '%s' pominięta: %s", _err.plugin_dir, _err.error)
app.jinja_loader = ChoiceLoader([
    app.jinja_loader,
    FileSystemLoader([str(pathlib.Path(__file__).resolve().parent.parent / "plugins" / meta.id / "templates")
                       for meta in _plugin_metas]),
])


@app.before_request
async def _redirect_to_setup_if_unconfigured():
    if request.endpoint in _SETUP_EXEMPT_ENDPOINTS:
        return
    if not is_configured():
        return redirect(url_for("setup_page"))


_PUBLIC_PATHS = {
    "/login", "/api/login", "/api/logout", "/api/auth/status", "/healthz",
}
_SETUP_ONLY_PUBLIC_PATHS = {
    "/setup", "/api/setup/draft", "/api/setup/test-satel-connection", "/api/setup/save",
    "/api/discover/satel", "/api/discover/tuya", "/api/discover/tuya-cloud",
}
_PUBLIC_PATH_PREFIXES = ("/static/",)


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PATH_PREFIXES):
        return True
    if path in _SETUP_ONLY_PUBLIC_PATHS:
        return not is_configured()
    return False


@app.before_request
async def _require_login_for_settings_and_control():
    if request.endpoint is None:
        return
    if _is_public_path(request.path):
        return
    if not is_configured() or auth_guard is None:
        return
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
async def index():
    return await render_template("index.html")


@app.route("/login")
async def login_page():
    return await render_template("login.html")


@app.route("/api/login", methods=["POST"])
async def api_login():
    payload = await request.get_json(silent=True) or {}
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
async def api_logout():
    auth_guard.destroy_session(request.cookies.get(SESSION_COOKIE_NAME))
    resp = jsonify({"message": "Wylogowano"})
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.route("/api/auth/status")
async def api_auth_status():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    logged_in = auth_guard.check_session(token)
    return jsonify({
        "logged_in": logged_in,
        "configured": auth_guard.configured,
        "username": auth_guard.session_username(token) if logged_in else None,
    })


@app.route("/setup")
async def setup_page():
    if is_configured():
        return redirect(url_for("index"))
    return await render_template("setup.html")


@app.route("/api/setup/draft", methods=["GET", "POST"])
async def api_setup_draft():
    if is_configured():
        return jsonify({"error": "Konfiguracja już istnieje."}), 409

    if request.method == "GET":
        return jsonify(load_draft(draft_path_for(config_path)))

    data = await request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Nieprawidłowe dane"}), 400
    save_draft(draft_path_for(config_path), data)
    return jsonify({"message": "Zapisano postęp"})


@app.route("/api/setup/test-satel-connection", methods=["POST"])
async def api_setup_test_satel_connection():
    payload = await request.get_json(silent=True) or {}
    host = (payload.get("host") or "").strip()
    if not host:
        return jsonify({"error": "Brak adresu IP centrali"}), 400
    try:
        port = int(payload.get("port") or 7094)
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowy port"}), 400
    encryption_key = (payload.get("encryption_key") or "").strip() or None
    if not payload.get("encryption_enabled"):
        encryption_key = None

    try:
        await test_satel_connection(host, port, encryption_key=encryption_key)
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
async def api_satel_test_output():
    satel_cfg = config_store.get("config").get("satel", {})
    host = (satel_cfg.get("host") or "").strip()
    port = satel_cfg.get("port")
    if not host or not port:
        return jsonify({"error": "Najpierw zapisz adres IP i port centrali (sekcja 🔌 Połączenie, /plugins/satel)"}), 400

    vi_cfg = satel_cfg.get("virtual_inputs", {})
    mode = vi_cfg.get("mode", "library")
    payload = await request.get_json(silent=True) or {}
    state = bool(payload.get("state", True))

    if mode == "http":
        name = (payload.get("name") or "").strip()
        satel_fields = config_store.get("satel_fields")
        inputs = satel_fields["virtual_inputs"] if "virtual_inputs" in satel_fields else vi_cfg.get("inputs", {})
        if name not in inputs:
            return jsonify({"error": "Wybierz skonfigurowane wirtualne wejście"}), 400
        vi_port = vi_cfg.get("port", DEFAULT_VIRTUAL_INPUT_HTTP_PORT)
        try:
            await test_satel_virtual_input_http(host, int(vi_port), DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE, name)
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
        return jsonify({"error": "Najpierw zapisz kod użytkownika centrali (sekcja 🔌 Połączenie, /plugins/satel)"}), 400
    try:
        output_number = int(payload.get("output_number"))
    except (TypeError, ValueError):
        return jsonify({"error": "Podaj numer wyjścia (1-256)"}), 400
    if not (1 <= output_number <= 256):
        return jsonify({"error": "Numer wyjścia musi być w zakresie 1-256"}), 400

    try:
        await test_satel_output(
            host, int(port), integration_key, output_number, state,
            encryption_key=satel_cfg.get("encryption_key") if satel_cfg.get("encryption_enabled") else None,
        )
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
async def api_discover_satel():
    payload = await request.get_json(silent=True) or {}
    satel_cfg = config_raw.get("satel", {})
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
    encryption_enabled = (
        payload["encryption_enabled"] if "encryption_enabled" in payload
        else satel_cfg.get("encryption_enabled")
    )
    encryption_key = (payload.get("encryption_key") or satel_cfg.get("encryption_key") or None) if encryption_enabled else None

    try:
        result = await discover_active_zones_and_outputs(
            host, port, timeout_seconds=timeout_seconds, encryption_key=encryption_key,
        )
    except ImportError:
        return jsonify({"error": "Biblioteka satel_integra nie jest zainstalowana na serwerze"}), 500
    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd wykrywania: {exc}"}), 502

    return jsonify(result)


@app.route("/api/setup/save", methods=["POST"])
async def api_setup_save():
    if is_configured():
        return jsonify({"error": "Konfiguracja już istnieje. Usuń config.yaml ręcznie, żeby uruchomić kreator ponownie."}), 409

    form = await request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

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

    db_path, _ = resolve_db_and_legacy_paths(config_path)
    store = ConfigStore(db_path)
    store.set("config", config_dict)
    store.set("sensors", {"sensors": []})
    store.set("automations", automations_dict)
    store.set("satel_fields", satel_fields_dict)
    store.set("tuya_devices", tuya_devices_dict)

    delete_draft(draft_path_for(config_path))

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
    token = auth_guard.create_session(config_dict["dashboard"]["auth"]["username"])
    resp.set_cookie(SESSION_COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="Lax")
    return resp


@app.route("/api/config/sensors", methods=["GET"])
async def api_config_sensors_get():
    return jsonify(config_store.get("sensors").get("sensors", []))


@app.route("/api/discover/tuya", methods=["POST"])
async def api_discover_tuya():
    try:
        devices = await asyncio.get_event_loop().run_in_executor(None, scan_tuya_network)
    except Exception as exc:
        return jsonify({"error": f"Błąd skanowania: {exc}"}), 502
    return jsonify({"devices": devices})


@app.route("/api/discover/tuya-cloud", methods=["POST"])
async def api_discover_tuya_cloud():
    payload = await request.get_json(silent=True) or {}
    api_region = (payload.get("api_region") or "").strip()
    access_id = (payload.get("access_id") or "").strip()
    access_key = (payload.get("access_key") or "").strip()
    if not api_region or not access_id or not access_key:
        return jsonify({"error": "Region, Access ID i Access Secret są wymagane"}), 400

    try:
        devices = await asyncio.get_event_loop().run_in_executor(
            None, fetch_tuya_cloud_devices, api_region, access_id, access_key,
        )
    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Błąd importu z Tuya Cloud: {exc}"}), 502
    return jsonify({"devices": devices})


@app.route("/api/config/location", methods=["GET"])
@require_auth()
async def api_config_location_get():
    return jsonify(config_raw.get("location") or {})


@app.route("/api/config/location", methods=["POST"])
@require_auth()
async def api_config_location_post():
    form = await request.get_json(silent=True)
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
async def api_config_export_yaml():
    return jsonify(config_store.export_yaml_bundle())


@app.route("/api/config/import-yaml", methods=["POST"])
@require_auth()
async def api_config_import_yaml():
    payload = await request.get_json(silent=True)
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
async def api_config_export_zip():
    bundle = config_store.export_yaml_bundle()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in bundle.items():
            zf.writestr(filename, content)
    buf.seek(0)
    download_name = f"satel-tuya-bridge-config-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return await send_file(buf, mimetype="application/zip", as_attachment=True, attachment_filename=download_name)


@app.route("/api/config/import-zip", methods=["POST"])
@require_auth()
async def api_config_import_zip():
    uploaded = (await request.files).get("file")
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


# Każda wtyczka deklaruje swój `toggle_route` w META (plugins/<id>/routes.py) - poniższe
# zestawy są z niego wyprowadzone, zamiast być osobno utrzymywaną listą id w tym pliku.
_CONNECTIONS_PLUGIN_IDS_RESTART = {
    m.id for m in _plugin_metas if m.toggle_route == "connections_restart"
}
_CONNECTIONS_PLUGIN_IDS_LIVE = {
    m.id for m in _plugin_metas if m.toggle_route == "toggle_prereq_connections_live"
}
_CONNECTIONS_PLUGIN_IDS = _CONNECTIONS_PLUGIN_IDS_RESTART | _CONNECTIONS_PLUGIN_IDS_LIVE
_AUTOMATIONS_PLUGIN_IDS = {m.id for m in _plugin_metas if m.toggle_route == "automations"}

_TOGGLE_ONLY_PLUGIN_IDS = {
    m.id for m in _plugin_metas
    if m.toggle_route in ("toggle_no_prereq", "toggle_prereq", "toggle_prereq_connections_live")
}
_TOGGLE_ONLY_CONFIG_KEY = {
    m.id: m.config_key for m in _plugin_metas if m.id in _TOGGLE_ONLY_PLUGIN_IDS
}
_TOGGLE_ONLY_NO_PREREQ_IDS = {
    m.id for m in _plugin_metas if m.toggle_route == "toggle_no_prereq"
}


@app.route("/plugins")
async def plugins_page():
    return await render_template("plugins.html")


@app.route("/api/plugins", methods=["GET"])
async def api_plugins_get():
    return jsonify(list_plugins(config_raw))


@app.route("/api/plugins/load-errors", methods=["GET"])
async def api_plugins_load_errors_get():
    return jsonify(list_plugin_load_errors())


@app.route("/api/plugins", methods=["POST"])
@require_auth()
async def api_plugins_post():
    body = await request.get_json(silent=True)
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
                if plugin_id not in _TOGGLE_ONLY_NO_PREREQ_IDS:
                    return jsonify({
                        "error": f"Najpierw skonfiguruj wtyczkę '{plugin_id}' w jej zakładce, zanim ją włączysz.",
                    }), 400
                current[config_key] = {}
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
async def automations_page():
    return await render_template("automations.html")


@app.route("/api/config/automation-context", methods=["GET"])
async def api_config_automation_context():
    tuya = config_raw.get("tuya", {})
    tuya_devices = sorted(set(tuya.get("devices", {})) | set(tuya.get("cloud_devices", {})))
    virtual_inputs = list(config_raw.get("satel", {}).get("virtual_inputs", {}).get("inputs", {}).keys())
    satel_partitions = config_raw.get("satel", {}).get("partitions", {})
    status_keys = [
        {"key": s["key"], "label": s["label"], "category": s["category"]}
        for s in history.get_status()
    ]
    motion_rule_names = [
        r["name"] for r in config_raw.get("reguły_ruchu", []) if r.get("name")
    ]
    dimmer_devices = sorted(
        name for name, cfg in tuya.get("devices", {}).items() if cfg.get("type") == "dimmer"
    )
    satel_outputs = sorted(config_raw.get("satel", {}).get("monitored_outputs", {}).keys())
    satel_zones = sorted(
        ({"name": name, "number": number}
         for name, number in config_raw.get("satel", {}).get("monitored_zones", {}).items()),
        key=lambda z: z["name"],
    )
    homewhiz_appliances = sorted((config_raw.get("homewhiz") or {}).get("appliances", {}).keys())
    return jsonify({
        "tuya_devices": tuya_devices, "virtual_inputs": virtual_inputs, "satel_partitions": satel_partitions,
        "status_keys": status_keys, "motion_rule_names": motion_rule_names,
        "dimmer_devices": dimmer_devices, "satel_outputs": satel_outputs, "satel_zones": satel_zones,
        "homewhiz_appliances": homewhiz_appliances,
    })


@app.route("/api/config/automations", methods=["GET"])
async def api_config_automations_get():
    return jsonify({
        "scenes": config_raw.get("scenes", []),
        "motion_rules": config_raw.get("reguły_ruchu", []),
        "presence_simulation": config_raw.get("presence_simulation", {"enabled": False}),
    })


@app.route("/api/config/automations", methods=["POST"])
@require_auth()
async def api_config_automations_post():
    form = await request.get_json(silent=True)
    if not isinstance(form, dict):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    errors = validate_automations(form)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    existing_log_level = config_store.get("automations").get("presence_simulation", {}).get("log_level")
    if existing_log_level and isinstance(form.get("presence_simulation"), dict):
        form["presence_simulation"].setdefault("log_level", existing_log_level)

    automations_dict = build_automations_dict(form)
    config_store.set("automations", automations_dict)

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


@app.route("/api/automation/scenes/<name>/run", methods=["POST"])
@require_auth()
async def api_automation_scene_run(name):
    scene_names = {s["name"] for s in config_raw.get("scenes", [])}
    if name not in scene_names:
        return jsonify({"error": f"Nieznana scena: '{name}'"}), 404
    command_id = history.enqueue_command("run_scene", {"name": name})
    return jsonify({"command_id": command_id, "message": f"Zlecono uruchomienie sceny '{name}'"})


@app.route("/flows")
async def flows_page():
    return await render_template("flows.html")


@app.route("/api/config/flows", methods=["GET"])
async def api_config_flows_get():
    return jsonify({"flows": config_raw.get("flows", [])})


@app.route("/api/config/flows", methods=["POST"])
@require_auth()
async def api_config_flows_post():
    payload = await request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("flows"), list):
        return jsonify({"error": "Nieprawidłowe dane formularza"}), 400

    scene_names = {s.get("name") for s in config_raw.get("scenes", []) if s.get("name")}
    errors = validate_flows(payload["flows"], scene_names)
    if errors:
        return jsonify({"error": "Popraw błędy:", "details": errors}), 400

    built_flows = build_flows(payload["flows"])
    current = config_store.get("automations")
    current["flows"] = built_flows
    config_store.set("automations", current)

    config = BridgeConfig.load(config_path)
    create_app(config)
    _enqueue_bridge_reload()

    return jsonify({"saved": {"flows": len(built_flows)}, "message": _RELOAD_MESSAGE})


@app.route("/api/flows/<flow_id>/run", methods=["POST"])
@require_auth()
async def api_flow_run(flow_id):
    flow_ids = {f.get("id") for f in config_raw.get("flows", [])}
    if flow_id not in flow_ids:
        return jsonify({"error": f"Nieznane flow: '{flow_id}'"}), 404
    command_id = history.enqueue_command("run_flow", {"flow_id": flow_id})
    return jsonify({"command_id": command_id, "message": f"Zlecono uruchomienie flow '{flow_id}'"})


@app.route("/api/sensors")
async def api_sensors():
    return jsonify(history.get_sensors())


@app.route("/api/history")
async def api_history():
    sensor = request.args.get("sensor")
    if not sensor:
        return jsonify({"error": "brak parametru 'sensor'"}), 400
    ts_from, ts_to = _parse_range()
    return jsonify(history.get_history(sensor, ts_from, ts_to))


@app.route("/api/events")
async def api_events():
    event_type = request.args.get("type") or None
    ts_from, ts_to = _parse_range()
    limit = int(request.args.get("limit", 500))
    return jsonify(history.get_events(event_type, ts_from, ts_to, limit))


@app.route("/api/alerts/active")
async def api_alerts_active():
    return jsonify(history.get_active_alerts())


@app.route("/api/alerts/history")
async def api_alerts_history():
    ts_from, ts_to = _parse_range()
    limit = int(request.args.get("limit", 200))
    return jsonify(history.get_alerts_history(ts_from, ts_to, limit))


@app.route("/api/status")
async def api_status():
    category = request.args.get("category") or None
    return jsonify(history.get_status(category))


@app.route("/api/satel/zone-types", methods=["GET"])
async def api_satel_zone_types():
    satel = config_raw.get("satel", {})
    return jsonify({
        "zone_types": satel.get("zone_types", {}),
        "output_types": satel.get("output_types", {}),
    })


@app.route("/api/satel/virtual-inputs", methods=["GET"])
async def api_satel_virtual_inputs():
    inputs = config_raw.get("satel", {}).get("virtual_inputs", {}).get("inputs", {})
    return jsonify({"virtual_inputs": sorted(inputs.keys())})


@app.route("/api/satel/partitions")
async def api_satel_partitions():
    return jsonify(satel_partitions_cfg)


@app.route("/api/satel/arm", methods=["POST"])
@require_auth()
async def api_satel_arm():
    payload = await request.get_json(silent=True) or {}
    partitions = payload.get("partitions") or list(satel_partitions_cfg.values())
    mode = payload.get("mode", 0)
    command_id = history.enqueue_command("satel_arm", {"partitions": partitions, "mode": mode})
    history.write_event("system", source="dashboard", detail={"message": f"Zlecono uzbrojenie stref {partitions}"})
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/satel/disarm", methods=["POST"])
@require_auth()
async def api_satel_disarm():
    payload = await request.get_json(silent=True) or {}
    partitions = payload.get("partitions") or list(satel_partitions_cfg.values())
    command_id = history.enqueue_command("satel_disarm", {"partitions": partitions})
    history.write_event("system", source="dashboard", detail={"message": f"Zlecono rozbrojenie stref {partitions}"})
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/satel/virtual-inputs/<name>/trigger", methods=["POST"])
@require_auth()
async def api_satel_virtual_input_trigger(name):
    inputs = config_raw.get("satel", {}).get("virtual_inputs", {}).get("inputs", {})
    if name not in inputs:
        return jsonify({"error": f"Nieznane wirtualne wejście '{name}'"}), 404
    command_id = history.enqueue_command("satel_virtual_input", {"name": name})
    history.write_event(
        "system", source="dashboard", detail={"message": f"Zlecono aktywację wirtualnego wejścia '{name}'"},
    )
    return jsonify({"command_id": command_id, "status": "pending"})


async def _enqueue_tuya_command(name: str, *, turn_on: bool):
    tuya_cfg = config_raw.get("tuya", {})
    if name not in tuya_cfg.get("devices", {}) and name not in tuya_cfg.get("cloud_devices", {}):
        return jsonify({"error": f"Nieznane urządzenie Tuya '{name}'"}), 404
    command_id = history.enqueue_command("tuya_on" if turn_on else "tuya_off", {"name": name})
    history.write_event(
        "system", source="dashboard",
        detail={"message": f"Zlecono {'włączenie' if turn_on else 'wyłączenie'} urządzenia Tuya '{name}'"},
    )
    return jsonify({"command_id": command_id, "status": "pending"})


@app.route("/api/tuya/<name>/on", methods=["POST"])
@require_auth()
async def api_tuya_on(name):
    return await _enqueue_tuya_command(name, turn_on=True)


@app.route("/api/tuya/<name>/off", methods=["POST"])
@require_auth()
async def api_tuya_off(name):
    return await _enqueue_tuya_command(name, turn_on=False)


@app.route("/api/commands/<int:command_id>")
async def api_command_status(command_id: int):
    cmd = history.get_command(command_id)
    if cmd is None:
        return jsonify({"error": "Nie znaleziono komendy"}), 404
    return jsonify(cmd)


@app.route("/api/system/restart", methods=["POST"])
@require_auth()
async def api_system_restart():
    command_id = history.enqueue_command("restart_process", {})
    history.write_event("system", source="dashboard", detail={"message": "Zlecono pełny restart bridge'a (panel + bridge)"})

    def _restart_dashboard() -> None:
        os._exit(0)

    threading.Timer(0.5, _restart_dashboard).start()

    return jsonify({
        "command_id": command_id,
        "message": "Restartuję panel i bridge - Docker podniesie oba kontenery ponownie "
                   "(restart: unless-stopped) w ciągu kilku-kilkunastu sekund. Ta strona "
                   "przestanie odpowiadać na chwilę.",
    })


@app.route("/system")
async def system_page():
    return await render_template("system.html")


@app.route("/api/system/users", methods=["GET"])
@require_auth()
async def api_system_users_get():
    return jsonify(config_store.list_users())


@app.route("/api/system/users", methods=["POST"])
@require_auth()
async def api_system_users_post():
    payload = await request.get_json(silent=True) or {}
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
async def api_system_user_password_post(user_id: int):
    payload = await request.get_json(silent=True) or {}
    password = payload.get("password") or ""
    if len(password) < 8:
        return jsonify({"error": "Hasło: minimum 8 znaków"}), 400
    config_store.update_user(user_id, password_hash=generate_password_hash(password))
    return jsonify({"message": "Hasło zmienione"})


@app.route("/api/system/users/<int:user_id>", methods=["DELETE"])
@require_auth()
async def api_system_user_delete(user_id: int):
    if config_store.user_count() <= 1:
        return jsonify({"error": "Nie można usunąć ostatniego konta - panel zostałby bez żadnego dostępu"}), 400
    config_store.delete_user(user_id)
    return jsonify({"message": "Konto usunięte"})


@app.route("/api/system/api-tokens", methods=["GET"])
@require_auth()
async def api_system_api_tokens_get():
    return jsonify(config_store.list_api_tokens())


@app.route("/api/system/api-tokens", methods=["POST"])
@require_auth()
async def api_system_api_tokens_post():
    payload = await request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nazwa tokenu jest wymagana"}), 400
    token_id, token = config_store.create_api_token(name)
    return jsonify({"id": token_id, "name": name, "token": token})


@app.route("/api/system/api-tokens/<int:token_id>", methods=["DELETE"])
@require_auth()
async def api_system_api_token_delete(token_id: int):
    config_store.delete_api_token(token_id)
    return jsonify({"message": "Token usunięty"})


@app.route("/api/system/settings", methods=["GET"])
@require_auth()
async def api_system_settings_get():
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
async def api_system_settings_post():
    payload = await request.get_json(silent=True) or {}
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
async def api_system_logs_loggers_get():
    component = request.args.get("component") or None
    return jsonify(history.get_log_loggers(component))


@app.route("/api/system/logs", methods=["GET"])
@require_auth()
async def api_system_logs_get():
    component = request.args.get("component") or None
    logger_name = request.args.get("logger") or None
    level = request.args.get("level") or None
    hours = float(request.args.get("hours", 24))
    limit = int(request.args.get("limit", 300))
    ts_from = time.time() - hours * 3600
    return jsonify(history.get_logs(component, logger_name, level, ts_from, limit))


@app.route("/healthz")
async def healthz():
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
    config_path, _ = resolve_db_and_legacy_paths(args.config)

    if is_configured():
        create_app(BridgeConfig.load(config_path))
        print(f"Panel dostępny pod http://{args.host}:{args.port}")
    else:
        print(
            f"Brak {config_path} - uruchamiam kreator pierwszej konfiguracji.\n"
            f"Otwórz http://{args.host if args.host != '0.0.0.0' else '<adres-rpi>'}:{args.port}/setup w przeglądarce."
        )

    from hypercorn.asyncio import serve
    from hypercorn.config import Config as HypercornConfig

    hconfig = HypercornConfig()
    hconfig.bind = [f"{args.host}:{args.port}"]
    asyncio.run(serve(app, hconfig))


if __name__ == "__main__":
    main()
