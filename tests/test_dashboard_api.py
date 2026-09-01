"""Testy dla dashboard/app.py - endpointy API, w tym egzekwowanie autoryzacji."""
import base64
import io
import zipfile

import pytest
import yaml
from werkzeug.security import generate_password_hash

from bridge.config import BridgeConfig

CONFIG_TEMPLATE = """
satel:
  host: "127.0.0.1"
  port: 7094
  integration_key: "x"
  partitions: {{Dom: 1, Garaz: 2}}
  monitored_zones: {{Salon: 12}}
  monitored_outputs: {{Syrena: 1}}
  virtual_inputs:
    port: 8888
    url_template: "http://{{host}}:{{port}}/x"
    key: "wspolny_klucz_http"
    inputs:
      motion_hall: 1
tuya:
  devices:
    gniazdko:
      id: "d1"
      ip: "1.2.3.4"
      local_key: "k"
      version: 3.4
      type: socket
reguły_ruchu: []
ebus:
  mqtt_host: "127.0.0.1"
  sensors_file: "sensors.yaml"
dashboard:
  db_path: "{db_path}"
  auth:
    password_hash: "{password_hash}"
    max_failed_attempts: 5
    lockout_seconds: 300
"""


def _basic_auth_header(password: str, username: str = "admin") -> dict:
    # CONFIG_TEMPLATE wyżej nie ustawia dashboard.auth.username, więc
    # AuthGuard domyślnie używa "admin" (patrz dashboard/auth.py) - stąd
    # domyślna wartość tu, bez zmiany wszystkich dotychczasowych wywołań.
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(tmp_path):
    """Świeżo skonfigurowany klient testowy Flask, izolowany per test (własna
    baza SQLite i sensors.yaml w tmp_path)."""
    from dashboard.app import app as flask_app
    from dashboard.app import create_app

    (tmp_path / "sensors.yaml").write_text("sensors: []\n", encoding="utf-8")
    password_hash = generate_password_hash("test_haslo_123")
    config_text = CONFIG_TEMPLATE.format(
        db_path=str(tmp_path / "history.db"),
        password_hash=password_hash,
    )
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")

    config = BridgeConfig.load(tmp_path / "config.yaml")

    import dashboard.app as app_module
    # config_path MUSI wskazywać na config.db (świeżo utworzone przez
    # migrację przy load() wyżej), nie na oryginalny config.yaml - inaczej
    # is_configured()/before_request przekierowałoby wszystko na /setup
    # (patrz bridge/config.py: BridgeConfig.load normalizuje ścieżkę do
    # bazy niezależnie od tego, jaką konwencję dostał, ale downstream kod w
    # dashboard/app.py trzyma config_path jako osobną zmienną globalną).
    app_module.config_path = config.db_path
    # create_app() od teraz PONOWNIE UŻYWA istniejącego auth_guard (żeby
    # zapis configu z panelu nie wylogowywał wszystkich - patrz
    # AuthGuard.update_config) - dla izolacji testów między sobą trzeba
    # więc jawnie wymusić świeży obiekt na starcie każdego testu.
    app_module.auth_guard = None

    create_app(config)
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


class TestPublicEndpoints:
    """Jedyne, co ma prawo działać BEZ zalogowania - mechanika logowania,
    kreator pierwszej konfiguracji i healthcheck (zewnętrzny monitoring nie
    wysyła poświadczeń panelu). WSZYSTKO inne, łącznie z samym podglądem
    strony głównej, wymaga zalogowania - patrz TestSettingsRequireLogin
    niżej ("przed zalogowaniem nic nie jest widoczne")."""

    def test_healthz_ok_when_no_components_reported(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


class TestSettingsRequireLogin:
    """Widoczność WSZYSTKIEGO (łącznie ze stroną główną - status na żywo/
    historia/dziennik/alarmy) i sterowanie alarmem wymagają zalogowania -
    nie tylko sam zapis/wysłanie komendy (patrz dashboard/app.py:
    _require_login_for_settings_and_control). Strony przekierowują na
    /login (302), API zwraca 401 - dokładnie jak istniejące @require_auth()
    dla akcji zapisu, żeby ta sama logika authorizedFetch()/redirect-na-401
    w JS działała identycznie."""

    def test_index_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_index_page_loads_when_authenticated_via_session(self, client):
        login = client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"})
        assert login.status_code == 200
        assert client.get("/").status_code == 200

    def test_status_endpoint_requires_auth(self, client):
        assert client.get("/api/status").status_code == 401
        resp = client.get("/api/status", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_live_dashboard_data_endpoints_require_auth(self, client):
        """Dane strony głównej (status na żywo/historia/dziennik/alarmy) -
        wcześniej celowo publiczne razem ze stroną, teraz chronione tak samo
        jak reszta panelu (patrz TestPublicEndpoints wyżej)."""
        for path in ("/api/sensors", "/api/history?sensor=x", "/api/events",
                     "/api/alerts/active", "/api/alerts/history"):
            assert client.get(path).status_code == 401, path
            resp = client.get(path, headers=_basic_auth_header("test_haslo_123"))
            assert resp.status_code == 200, path

    def test_command_status_requires_auth(self, client):
        assert client.get("/api/commands/999").status_code == 401
        resp = client.get("/api/commands/999", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 404  # zalogowany, ale komenda o tym ID nie istnieje

    def test_satel_zone_types_requires_auth(self, client):
        assert client.get("/api/satel/zone-types").status_code == 401
        resp = client.get("/api/satel/zone-types", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"zone_types": {}, "output_types": {}}

    def test_plugins_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_automations_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/automations", follow_redirects=False)
        assert resp.status_code == 302

    def test_system_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/system", follow_redirects=False)
        assert resp.status_code == 302

    def test_config_sensors_requires_auth(self, client):
        assert client.get("/api/config/sensors").status_code == 401
        resp = client.get("/api/config/sensors", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_satel_partitions_requires_auth(self, client):
        assert client.get("/api/satel/partitions").status_code == 401
        resp = client.get("/api/satel/partitions", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"Dom": 1, "Garaz": 2}

    def test_unknown_path_still_404s_not_redirected(self, client):
        """Regresja: ścieżka niepasująca do ŻADNEJ trasy (request.endpoint
        is None) musi zostać zwykłym 404, nie przekierowaniem na /login."""
        resp = client.get("/nie-ma-takiej-strony", follow_redirects=False)
        assert resp.status_code == 404


class TestGlobalErrorHandlerNonApiRoutes:
    """Regresja: handle_unexpected_error (dashboard/app.py) robił `raise exc`
    dla ścieżek spoza /api/*, żeby "nie ruszać" domyślnej obsługi błędów
    Flaska - ale ponowne rzucenie wyjątku WEWNĄTRZ zarejestrowanego
    errorhandlera nie przywraca normalnego zachowania Flaska, tylko ucieka
    z całego pipeline'u i zamienia się w generyczne 500, niezależnie od
    prawdziwego kodu błędu. Zaobserwowane w praktyce: zwykłe 404 na
    /favicon.ico (przeglądarka pyta o nie przy każdym wejściu na stronę)
    kończyło się jako "500 Internal Server Error" w logach zamiast
    cichego 404."""

    def test_404_on_non_api_route_stays_404_not_500(self, client):
        resp = client.get("/favicon.ico")
        assert resp.status_code == 404

    def test_404_on_arbitrary_non_api_path_stays_404(self, client):
        resp = client.get("/this-page-does-not-exist")
        assert resp.status_code == 404


class TestSessionAuthEndpoints:
    """/login, /api/login, /api/logout, /api/auth/status - logowanie
    sesyjne, które zastąpiło prompt()-owy Basic Auth w panelu webowym."""

    def test_login_page_loads(self, client):
        assert client.get("/login").status_code == 200

    def test_auth_status_when_not_logged_in(self, client):
        resp = client.get("/api/auth/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"logged_in": False, "configured": True, "username": None}

    def test_login_with_correct_password_sets_session_cookie(self, client):
        resp = client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"})
        assert resp.status_code == 200
        assert "stb_session" in resp.headers.get("Set-Cookie", "")

    def test_login_with_wrong_password_returns_401(self, client):
        resp = client.post("/api/login", json={"username": "admin", "password": "złe hasło"})
        assert resp.status_code == 401

    def test_login_with_wrong_username_returns_401(self, client):
        resp = client.post("/api/login", json={"username": "ktoinny", "password": "test_haslo_123"})
        assert resp.status_code == 401

    def test_auth_status_true_after_login(self, client):
        client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"})
        resp = client.get("/api/auth/status")
        assert resp.get_json()["logged_in"] is True

    def test_session_cookie_authorizes_protected_endpoint_without_basic_auth(self, client):
        """Kluczowe zachowanie: PO /api/login żądania działają bez
        żadnego nagłówka Authorization - sam token w ciasteczku wystarcza
        (klient testowy Flask trzyma ciasteczka między żądaniami jak
        prawdziwa przeglądarka)."""
        client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"})
        resp = client.post("/api/plugins/ebus/sensors", json=[])
        assert resp.status_code == 200

    def test_logout_clears_session(self, client):
        client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"})
        client.post("/api/logout")
        resp = client.get("/api/auth/status")
        assert resp.get_json()["logged_in"] is False

    def test_logout_without_active_session_still_succeeds(self, client):
        resp = client.post("/api/logout")
        assert resp.status_code == 200

    def test_protected_endpoint_still_works_with_basic_auth_fallback(self, client):
        """Basic Auth wciąż działa (skrypty/curl/automatyzacja poza
        przeglądarką) - sesja to dodatek, nie zamiennik."""
        resp = client.post("/api/plugins/ebus/sensors", json=[], headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200


class TestProtectedArmDisarmEndpoints:
    def test_arm_without_auth_returns_401(self, client):
        resp = client.post("/api/satel/arm", json={"partitions": [1]})
        assert resp.status_code == 401

    def test_disarm_without_auth_returns_401(self, client):
        resp = client.post("/api/satel/disarm", json={"partitions": [1]})
        assert resp.status_code == 401

    def test_arm_with_auth_enqueues_command(self, client):
        resp = client.post(
            "/api/satel/arm", json={"partitions": [1], "mode": 0}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "pending"
        assert "command_id" in body

    def test_arm_command_visible_via_status_endpoint(self, client):
        resp = client.post(
            "/api/satel/arm", json={"partitions": [1]}, headers=_basic_auth_header("test_haslo_123"),
        )
        command_id = resp.get_json()["command_id"]
        status_resp = client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123"))
        assert status_resp.status_code == 200
        assert status_resp.get_json()["status"] == "pending"
        assert status_resp.get_json()["command"] == "satel_arm"

    def test_arm_defaults_to_all_configured_partitions(self, client):
        resp = client.post("/api/satel/arm", json={}, headers=_basic_auth_header("test_haslo_123"))
        command_id = resp.get_json()["command_id"]
        status_resp = client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123"))
        assert set(status_resp.get_json()["params"]["partitions"]) == {1, 2}


class TestVirtualInputEndpoints:
    """GET /api/satel/virtual-inputs (lista nazw - strona główna) i
    POST /api/satel/virtual-inputs/<nazwa>/trigger (ręczna aktywacja -
    ten sam mechanizm kolejki komend co uzbrojenie/rozbrojenie wyżej).
    CONFIG_TEMPLATE ma jedno wirtualne wejście: "motion_hall"."""

    def test_list_requires_auth(self, client):
        assert client.get("/api/satel/virtual-inputs").status_code == 401

    def test_list_returns_configured_names(self, client):
        resp = client.get("/api/satel/virtual-inputs", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"virtual_inputs": ["motion_hall"]}

    def test_trigger_without_auth_returns_401(self, client):
        resp = client.post("/api/satel/virtual-inputs/motion_hall/trigger")
        assert resp.status_code == 401

    def test_trigger_with_auth_enqueues_command(self, client):
        resp = client.post(
            "/api/satel/virtual-inputs/motion_hall/trigger", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "pending"
        assert "command_id" in body

    def test_trigger_command_visible_via_status_endpoint(self, client):
        resp = client.post(
            "/api/satel/virtual-inputs/motion_hall/trigger", headers=_basic_auth_header("test_haslo_123"),
        )
        command_id = resp.get_json()["command_id"]
        status_resp = client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123"))
        assert status_resp.status_code == 200
        assert status_resp.get_json()["status"] == "pending"
        assert status_resp.get_json()["command"] == "satel_virtual_input"
        assert status_resp.get_json()["params"] == {"name": "motion_hall"}

    def test_trigger_unknown_name_returns_404(self, client):
        resp = client.post(
            "/api/satel/virtual-inputs/nieistniejace/trigger", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 404


class TestSatelTestOutputEndpoint:
    """POST /api/satel/test-output - przycisk TEST w nowej sekcji "🧪 Test
    wyjścia / wejścia wirtualnego IP" na /plugins/satel. Musi czytać tryb
    (library/http) ŚWIEŻO z config_store, NIE z config_raw (migawka sprzed
    startu procesu) - inaczej zapisanie nowego trybu przez
    /api/plugins/satel/connection i od razu TEST wciąż testowałoby STARY
    tryb, dając błąd z niewłaściwej gałęzi walidacji (zgłoszony bug:
    "Podaj numer wyjścia" mimo wybranego i zapisanego trybu HTTP)."""

    def test_requires_auth(self, client):
        resp = client.post("/api/satel/test-output", json={"output_number": 1})
        assert resp.status_code == 401

    def test_library_mode_uses_freshly_saved_output_number(self, client, monkeypatch):
        captured = {}

        async def fake_test_output(host, port, integration_key, output_number, state, **_kwargs):
            captured["output_number"] = output_number

        monkeypatch.setattr("dashboard.app.test_satel_output", fake_test_output)
        resp = client.post(
            "/api/satel/test-output", json={"output_number": 15, "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert captured["output_number"] == 15

    def test_http_mode_saved_after_client_created_is_picked_up_without_restart(self, client, monkeypatch):
        # CONFIG_TEMPLATE nie ustawia virtual_inputs.mode - domyślnie
        # "library". Zmieniamy go tu na "http" TAK, jak robi to
        # /api/plugins/satel/connection (zapis do config_store), BEZ
        # ponownego wywołania create_app() - dokładnie odtwarzając
        # scenariusz "zapisz połączenie -> od razu TEST, bez restartu".
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        app_module.config_store.set("config", current)

        captured = {}

        async def fake_test_http(host, port, url_template, key, number, **_kwargs):
            captured["number"] = number

        monkeypatch.setattr("dashboard.app.test_satel_virtual_input_http", fake_test_http)
        resp = client.post(
            "/api/satel/test-output", json={"name": "motion_hall", "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert captured["number"] == 1

    def test_http_mode_with_output_number_payload_does_not_leak_library_error(self, client):
        # Regresja dla zgłoszonego buga: frontend w trybie HTTP wysyła
        # {name, state}, NIE {output_number, state} - gdyby backend mimo
        # to poszedł gałęzią "library" (bo czytał stary tryb), dostalibyśmy
        # mylące "Podaj numer wyjścia" zamiast błędu o brakującym wejściu.
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        app_module.config_store.set("config", current)

        resp = client.post(
            "/api/satel/test-output", json={"state": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        assert "wyjścia" not in resp.get_json()["error"]

    def test_http_mode_uses_shared_key_not_per_input_key(self, client, monkeypatch):
        """Zgłoszony błąd projektowy: hasło HTTP wejść IP jest WSPÓLNE dla
        całego modułu ETHM (satel.virtual_inputs.key, ustawiane RAZ w
        DLOADX), NIE osobne per wejście - patrz obszerny docstring
        bridge/satel_client.py. CONFIG_TEMPLATE ma "motion_hall" jako sam
        numer (bez własnego klucza) i wspólny virtual_inputs.key - TEST
        musi zadziałać, używając TEGO klucza."""
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        app_module.config_store.set("config", current)

        captured = {}

        async def fake_test_http(host, port, url_template, key, number, **_kwargs):
            captured["key"] = key

        monkeypatch.setattr("dashboard.app.test_satel_virtual_input_http", fake_test_http)
        resp = client.post(
            "/api/satel/test-output", json={"name": "motion_hall", "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert captured["key"] == "wspolny_klucz_http"

    def test_http_mode_without_shared_key_gives_clear_error(self, client):
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        current["satel"]["virtual_inputs"]["key"] = ""
        app_module.config_store.set("config", current)

        resp = client.post(
            "/api/satel/test-output", json={"name": "motion_hall", "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        assert "hasł" in resp.get_json()["error"].lower()


class TestSystemLogsLoggersEndpoint:
    """/api/system/logs/loggers - lista WYSTĘPUJĄCYCH nazw loggerów, do
    rozwijanej listy filtra w panelu (⚙ Ogólne -> Logi), patrz
    HistoryStore.get_log_loggers."""

    def test_requires_auth(self, client):
        resp = client.get("/api/system/logs/loggers")
        assert resp.status_code == 401

    def test_returns_distinct_logger_names(self, client):
        import dashboard.app as app_module
        app_module.history.write_log("bridge", "bridge.satel", "INFO", "a")
        app_module.history.write_log("bridge", "bridge.satel", "WARNING", "b")
        app_module.history.write_log("bridge", "bridge.tuya_local", "DEBUG", "c")

        resp = client.get("/api/system/logs/loggers", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == ["bridge.satel", "bridge.tuya_local"]

    def test_filters_by_component(self, client):
        import dashboard.app as app_module
        app_module.history.write_log("bridge", "bridge.satel", "INFO", "a")
        app_module.history.write_log("dashboard", "dashboard.app", "INFO", "b")

        resp = client.get(
            "/api/system/logs/loggers?component=dashboard", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.get_json() == ["dashboard.app"]


class TestSystemRestartEndpoint:
    """/api/system/restart - pełny restart obu kontenerów (bridge + panel),
    w odróżnieniu od reload_config() (patrz _enqueue_bridge_reload), który
    celowo nie dotyka żywych połączeń sieciowych. Testy monkeypatchują
    os._exit/threading.Timer.start - inaczej test zabiłby własny proces."""

    def _patch_exit(self, monkeypatch):
        exit_calls = []
        monkeypatch.setattr("dashboard.app.os._exit", lambda code: exit_calls.append(code))

        started_timers = []

        class FakeTimer:
            def __init__(self, interval, fn):
                self.interval = interval
                self.fn = fn

            def start(self):
                started_timers.append(self)  # NIE wołamy fn() - proces testowy ma żyć dalej

        monkeypatch.setattr("dashboard.app.threading.Timer", FakeTimer)
        return exit_calls, started_timers

    def test_requires_auth(self, client, monkeypatch):
        self._patch_exit(monkeypatch)
        resp = client.post("/api/system/restart")
        assert resp.status_code == 401

    def test_enqueues_restart_command(self, client, monkeypatch):
        self._patch_exit(monkeypatch)
        resp = client.post("/api/system/restart", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert "Restartuję" in resp.get_json()["message"]

        command_id = resp.get_json()["command_id"]
        status_resp = client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123"))
        assert status_resp.get_json()["command"] == "restart_process"

    def test_schedules_delayed_self_exit_without_exiting_immediately(self, client, monkeypatch):
        exit_calls, started_timers = self._patch_exit(monkeypatch)
        client.post("/api/system/restart", headers=_basic_auth_header("test_haslo_123"))
        assert len(started_timers) == 1
        assert started_timers[0].interval == 0.5
        assert exit_calls == []  # timer.start() spatchowany - fn() nie wywołane, proces żyje


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestConfigBackupZipEndpoints:
    """/api/config/export-zip + /api/config/import-zip - kopia zapasowa
    całej konfiguracji jako prawdziwe archiwum .zip, z podwójnym
    sprawdzeniem importu PRZED zapisem (patrz bridge/config_store.py:
    import_yaml_bundle_validated - to samo, co testuje tests/test_config.py,
    tu tylko przez warstwę HTTP/auth/reload)."""

    def test_export_requires_auth(self, client):
        assert client.get("/api/config/export-zip").status_code == 401

    def test_export_returns_valid_zip_with_config_yaml(self, client):
        resp = client.get("/api/config/export-zip", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            names = zf.namelist()
            assert "config.yaml" in names
            content = yaml.safe_load(zf.read("config.yaml"))
            assert content["satel"]["host"] == "127.0.0.1"

    def test_import_requires_auth(self, client):
        resp = client.post(
            "/api/config/import-zip",
            data={"file": (io.BytesIO(_zip_bytes({"config.yaml": "a: 1"})), "backup.zip")},
        )
        assert resp.status_code == 401

    def test_import_without_file_is_error(self, client):
        resp = client.post("/api/config/import-zip", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_import_non_zip_file_is_error(self, client):
        resp = client.post(
            "/api/config/import-zip",
            data={"file": (io.BytesIO(b"nie jestem zipem"), "backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        assert "zip" in resp.get_json()["error"].lower()

    def test_import_valid_roundtrip_from_export(self, client):
        export_resp = client.get("/api/config/export-zip", headers=_basic_auth_header("test_haslo_123"))
        import_resp = client.post(
            "/api/config/import-zip",
            data={"file": (io.BytesIO(export_resp.data), "backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert import_resp.status_code == 200
        assert "config" in import_resp.get_json()["imported"]

    def test_import_rejects_config_missing_satel_fields_without_writing(self, client):
        broken = _zip_bytes({
            "config.yaml": "satel: {}\ntuya:\n  devices: {}\nreguły_ruchu: []\nebus:\n  mqtt_host: x\n",
        })
        resp = client.post(
            "/api/config/import-zip",
            data={"file": (io.BytesIO(broken), "backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        assert "satel" in resp.get_json()["error"]

        # Konfiguracja MUSI zostać nietknięta - sprawdzone przez ponowny
        # eksport (wciąż zawiera oryginalny host, nie zniknął/nie zepsuł się).
        export_resp = client.get("/api/config/export-zip", headers=_basic_auth_header("test_haslo_123"))
        with zipfile.ZipFile(io.BytesIO(export_resp.data)) as zf:
            content = yaml.safe_load(zf.read("config.yaml"))
        assert content["satel"]["host"] == "127.0.0.1"

    def test_import_rejects_broken_yaml_syntax(self, client):
        broken = _zip_bytes({"config.yaml": "satel: [unclosed"})
        resp = client.post(
            "/api/config/import-zip",
            data={"file": (io.BytesIO(broken), "backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400


class TestAutomationContextEndpoint:
    """/automations i /api/config/automation-context teraz wymagają
    zalogowania (patrz TestSettingsRequireLogin) - reszta testów loguje
    się przez nagłówek Basic Auth, żeby sprawdzić właściwą treść."""

    def test_context_requires_auth(self, client):
        assert client.get("/api/config/automation-context").status_code == 401

    def test_context_returns_configured_device_and_virtual_input_names(self, client):
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["tuya_devices"] == ["gniazdko"]
        assert data["virtual_inputs"] == ["motion_hall"]

    def test_context_returns_satel_output_names_for_target_system_ui(self, client):
        """"Urządzenie docelowe" akcji reguły ruchu wybiera się dwuetapowo -
        system (Tuya/Satel), potem coś z tego systemu. Dla Satela to nazwy
        wyjść (satel.monitored_outputs), np. do sterowania bramą/syreną."""
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["satel_outputs"] == ["Syrena"]  # z CONFIG_TEMPLATE

    def test_context_returns_satel_zone_names_for_zone_number_ui(self, client):
        """"Wejście w Satelu" (zona wyzwalająca regułę) wybiera się z listy
        skonfigurowanych wejść (satel.monitored_zones), nie wpisuje gołego
        numeru na pamięć."""
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["satel_zones"] == [{"name": "Salon", "number": 12}]  # z CONFIG_TEMPLATE

    def test_context_returns_satel_partitions_for_scene_action_ui(self, client):
        """Zakładka /automations potrzebuje nazwanych stref, żeby akcje
        'Uzbrój strefę(y)'/'Rozbrój strefę(y)' w edytorze scen mogły dać
        wybrać KTÓRE strefy, zamiast zawsze dotykać tylko domyślnej."""
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["satel_partitions"] == {"Dom": 1, "Garaz": 2}  # z CONFIG_TEMPLATE

    def test_context_includes_cloud_only_devices(self, client):
        """Zgłoszony błąd: urządzenie sterowane WYŁĄCZNIE przez Tuya Cloud
        (zakładka ☁ Tuya Cloud, brak local_key/IP) nie dało się wybrać jako
        cel sceny/reguły ruchu, bo ten endpoint listował TYLKO tuya.devices
        (lokalne) - mimo że TuyaController (bridge/tuya_dispatch.py) i tak
        potrafi nim sterować."""
        client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "T & H Sensor", "id": "cloud1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert "gniazdko" in data["tuya_devices"]  # lokalne, nietknięte
        assert "T & H Sensor" in data["tuya_devices"]

    def test_context_includes_status_keys_for_condition_builder(self, client):
        """Edytor warunków (and/or/not/state) w /automations potrzebuje
        listy aktualnych kluczy statusu (patrz bridge/conditions.py: warunek
        "state"), żeby użytkownik mógł wybrać status z listy zamiast
        pamiętać dokładny klucz jak "satel_input:3"."""
        import dashboard.app as app_module
        app_module.history.set_status("satel_input:3", "satel_input", "naruszone", label="Salon")
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert {"key": "satel_input:3", "label": "Salon", "category": "satel_input"} in data["status_keys"]

    def test_context_includes_motion_rule_names_for_chaining_ui(self, client):
        client.post(
            "/api/config/automations",
            json={
                "scenes": [],
                "motion_rules": [
                    {"zone_number": "12", "name": "wejscie_do_salonu", "target_device": "gniazdko", "duration_seconds": "30"},
                ],
                "presence_simulation": {"enabled": False},
            },
            headers=_basic_auth_header("test_haslo_123"),
        )
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["motion_rule_names"] == ["wejscie_do_salonu"]

    def test_context_includes_dimmer_devices_for_brightness_field_gating(self, client):
        """Edytor pokazuje pole "Jasność %" TYLKO dla urządzeń ze
        ściemniaczem (type == "dimmer") - "gniazdko" z CONFIG_TEMPLATE jest
        zwykłym gniazdkiem (type domyślny), więc nie powinno się tu pojawić."""
        client.post(
            "/api/plugins/tuya/devices",
            json={"tuya_devices": [
                {"name": "lampa_scienna", "id": "dimmer1", "ip": "1.2.3.9", "local_key": "k9",
                 "type": "dimmer", "dimmer_dp": "3"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["dimmer_devices"] == ["lampa_scienna"]

    def test_context_includes_homewhiz_appliances_for_device_state_source(self, client):
        """Nazwy skonfigurowanych urządzeń HomeWhiz - do wyboru jako
        "system: HomeWhiz" WEJŚCIA reguły ruchu (source: homewhiz_state,
        patrz RulesEngine.handle_device_state)."""
        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["homewhiz_appliances"] == ["Zmywarka"]

    def test_context_homewhiz_appliances_empty_when_unconfigured(self, client):
        data = client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["homewhiz_appliances"] == []


class TestAutomationsEndpoint:
    def test_get_automations_requires_auth(self, client):
        resp = client.get("/api/config/automations", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"scenes": [], "motion_rules": [], "presence_simulation": {"enabled": False}}

    def test_post_automations_without_auth_returns_401(self, client):
        payload = {"scenes": [], "motion_rules": [], "presence_simulation": {}}
        resp = client.post("/api/config/automations", json=payload)
        assert resp.status_code == 401

    def test_post_automations_with_wrong_auth_returns_401(self, client):
        payload = {"scenes": [], "motion_rules": [], "presence_simulation": {}}
        resp = client.post("/api/config/automations", json=payload, headers=_basic_auth_header("złe_hasło"))
        assert resp.status_code == 401

    def test_post_automations_with_correct_auth_saves_scene(self, client):
        payload = {
            "scenes": [{"name": "test_scena", "actions": [{"type": "tuya_on", "device": "gniazdko"}]}],
            "motion_rules": [],
            "presence_simulation": {"enabled": False},
        }
        resp = client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["saved"] == {"scenes": 1, "motion_rules": 0}

    def test_post_automations_enqueues_bridge_reload_command(self, client):
        import dashboard.app as app_module

        payload = {"scenes": [], "motion_rules": [], "presence_simulation": {}}
        client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

    def test_post_automations_validates_scene_fields(self, client):
        payload = {"scenes": [{"name": "", "actions": []}], "motion_rules": [], "presence_simulation": {}}
        resp = client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400
        assert "details" in resp.get_json()

    def test_saved_automations_persist_and_are_readable(self, client):
        payload = {
            "scenes": [{"name": "scena1", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [{"zone_number": "5", "scene": "scena1"}],
            "presence_simulation": {
                "enabled": True, "devices": ["gniazdko"],
                "min_interval_minutes": "5", "max_interval_minutes": "10",
            },
        }
        client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        resp = client.get("/api/config/automations", headers=_basic_auth_header("test_haslo_123"))
        data = resp.get_json()
        assert data["scenes"][0]["name"] == "scena1"
        assert data["motion_rules"][0]["scene"] == "scena1"
        assert data["presence_simulation"]["enabled"] is True
        assert data["presence_simulation"]["devices"] == ["gniazdko"]

    def test_post_automations_writes_automations_document(self, client):
        import dashboard.app as app_module

        payload = {
            "scenes": [{"name": "s", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [], "presence_simulation": {},
        }
        client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert app_module.config_store.get("automations")["scenes"][0]["name"] == "s"

    def test_post_automations_does_not_touch_config_document(self, client):
        """Zapis z panelu MUSI iść do dokumentu "automations", nigdy do
        "config" - inaczej reszta ustawień (Satel/Tuya/hasło) zostałaby
        nadpisana pustymi wartościami z formularza automatyzacji (patrz
        uzasadnienie dla sensors.yaml/automations.yaml w bridge/config.py)."""
        import dashboard.app as app_module

        before = app_module.config_store.get("config")
        payload = {
            "scenes": [{"name": "s", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [], "presence_simulation": {},
        }
        client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        after = app_module.config_store.get("config")
        assert before == after


class TestLocationEndpoint:
    """Zakładka "Połączenia", sekcja "Lokalizacja" (współrzędne do
    wschodu/zachodu słońca w oknach czasowych "sunset"/"sunrise" reguł
    ruchu, patrz bridge/scheduling.py) - dashboard/setup.py:
    build_location_patch/validate_location. CONFIG_TEMPLATE nie ustawia
    "location" - startowo nieobecna."""

    def test_get_without_auth_returns_401(self, client):
        assert client.get("/api/config/location").status_code == 401

    def test_get_with_auth_returns_empty_when_unset(self, client):
        resp = client.get("/api/config/location", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_post_without_auth_returns_401(self, client):
        resp = client.post("/api/config/location", json={"latitude": "52.23", "longitude": "21.01"})
        assert resp.status_code == 401

    def test_post_sets_location(self, client):
        import dashboard.app as app_module

        payload = {"latitude": "52.23", "longitude": "21.01"}
        resp = client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["location"] == {"latitude": 52.23, "longitude": 21.01}

    def test_get_reflects_saved_location(self, client):
        payload = {"latitude": "52.23", "longitude": "21.01"}
        client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123"))
        resp = client.get("/api/config/location", headers=_basic_auth_header("test_haslo_123"))
        assert resp.get_json() == {"latitude": 52.23, "longitude": 21.01}

    def test_post_validates_out_of_range_latitude(self, client):
        payload = {"latitude": "195", "longitude": "21.01"}
        resp = client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_post_blank_clears_existing_location(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/config/location", json={"latitude": "52.23", "longitude": "21.01"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/config/location", json={"latitude": "", "longitude": ""},
            headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")
        assert "location" not in saved

    def test_post_preserves_other_config_sections(self, client):
        import dashboard.app as app_module

        payload = {"latitude": "52.23", "longitude": "21.01"}
        client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123"))
        saved = app_module.config_store.get("config")
        assert saved["satel"]["host"] == "127.0.0.1"
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"

    def test_post_enqueues_bridge_reload_not_restart(self, client):
        """Lokalizacja to czyste dane (brak żywego połączenia sieciowego
        do zerwania) - w odróżnieniu od /api/config/connections, zapis
        prosi bridge o LIVE reload_config(), nie wymaga restartu procesu."""
        import dashboard.app as app_module

        payload = {"latitude": "52.23", "longitude": "21.01"}
        resp = client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert "bez restartu" in resp.get_json()["message"].lower()
        commands = [c["command"] for c in app_module.history.get_pending_commands()]
        assert "reload_config" in commands


class TestPluginsEndpoint:
    """Zakładka 🧩 Wtyczki - spójny przegląd wszystkich integracji
    (patrz dashboard/setup.py: list_plugins) i przełącznik pola "enabled" -
    dla tuya_cloud/home_assistant/mqtt_publish/presence_simulation przez
    pełny formularz Połączeń/Automatyzacji (_CONNECTIONS_PLUGIN_IDS_RESTART/
    _AUTOMATIONS_PLUGIN_IDS), a dla satel/tuya_local/ebus/unifi_protect/
    notifications - wtyczek z już własną, dedykowaną stroną - bezpośrednim
    przełącznikiem "enabled" w ich sekcji configu (_TOGGLE_ONLY_PLUGIN_IDS w
    dashboard/app.py), o ile ta sekcja w ogóle istnieje (inaczej nie ma czego
    włączać - patrz test_post_enabling_unconfigured_unifi_protect_is_rejected).
    Jedyny wyjątek bez własnego przełącznika to tuya_zigbee (dzieli
    połączenie/runtime z tuya_local)."""

    def test_get_requires_auth(self, client):
        assert client.get("/api/plugins").status_code == 401

    def test_get_returns_satel_and_tuya_local_enabled_by_default(self, client):
        by_id = {p["id"]: p for p in client.get("/api/plugins", headers=_basic_auth_header("test_haslo_123")).get_json()}
        assert by_id["satel"]["enabled"] is True
        assert by_id["tuya_local"]["enabled"] is True
        assert by_id["home_assistant"]["enabled"] is False
        assert by_id["presence_simulation"]["enabled"] is False

    def test_get_never_leaks_secrets(self, client):
        """Nawet zalogowanemu - to endpoint listujący WŁĄCZONE/WYŁĄCZONE
        wtyczki (w odróżnieniu od /api/config/connections), więc absolutnie
        nie powinien zwracać integration_key/local_key/access_key."""
        text = client.get("/api/plugins", headers=_basic_auth_header("test_haslo_123")).get_data(as_text=True)
        assert '"x"' not in text  # integration_key z CONFIG_TEMPLATE
        assert '"k"' not in text  # local_key gniazdka z CONFIG_TEMPLATE

    def test_post_without_auth_returns_401(self, client):
        resp = client.post("/api/plugins", json={"home_assistant": {"enabled": True}})
        assert resp.status_code == 401

    def test_post_unknown_plugin_id_rejected(self, client):
        resp = client.post(
            "/api/plugins", json={"does_not_exist": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_post_enabling_unconfigured_unifi_protect_is_rejected(self, client):
        """UniFi Protect nie ma żadnej sekcji w CONFIG_TEMPLATE (brak hosta/
        loginu) - włączanie nieskonfigurowanej integracji musi być odrzucone
        z czytelnym błędem, nie zapisane w połowie."""
        resp = client.post(
            "/api/plugins", json={"unifi_protect": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_post_toggles_satel_enabled_without_touching_host(self, client):
        """satel ma już własną, dedykowaną stronę (/plugins/satel) - "enabled"
        jest tu obsługiwane bezpośrednio (bez przechodzenia przez formularz
        Połączeń), reszta pól (host/klucz/itd.) zostaje nietknięta."""
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins", json={"satel": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert "wymagają pełnego restartu" in resp.get_json()["message"]
        saved = app_module.config_store.get("config")
        assert saved["satel"]["enabled"] is False
        assert saved["satel"]["host"] == "127.0.0.1"  # nietknięte

    def test_post_toggles_ebus_enabled(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins", json={"ebus": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["ebus"]["enabled"] is False
        assert saved["ebus"]["mqtt_host"] == "127.0.0.1"  # nietknięte

    def test_post_updates_satel_poll_interval_live(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins", json={"satel": {"status_poll_interval_seconds": 25}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert "bez restartu" in resp.get_json()["message"]
        saved = app_module.config_store.get("config")
        assert saved["satel"]["status_poll_interval_seconds"] == 25
        assert saved["satel"]["host"] == "127.0.0.1"  # nietknięte

    def test_post_updates_tuya_local_poll_interval_without_restart_message(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins", json={"tuya_local": {"status_poll_interval_seconds": 45}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        message = resp.get_json()["message"]
        assert "bez restartu" in message
        assert "wymagają pełnego restartu" not in message
        saved = app_module.config_store.get("config")
        assert saved["tuya"]["status_poll_interval_seconds"] == 45
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"  # nietknięte

    def test_post_rejects_poll_interval_below_minimum(self, client):
        resp = client.post(
            "/api/plugins", json={"satel": {"status_poll_interval_seconds": 1}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_post_enables_home_assistant_without_touching_satel_host(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins", json={"home_assistant": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["home_assistant"]["enabled"] is True
        assert saved["satel"]["host"] == "127.0.0.1"  # nietknięte
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"  # nietknięte

    def test_post_disables_mqtt_publish(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins", json={"mqtt_publish": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")
        assert saved["mqtt_publish"]["enabled"] is False

    def test_post_enabling_tuya_cloud_without_credentials_is_rejected(self, client):
        """Włączenie stąd samym przełącznikiem nie ma sensu bez wcześniej
        wpisanych poświadczeń (Access ID) w zakładce 🔌 Połączenia -
        walidacja musi to odrzucić z czytelnym błędem, nie zapisać połowicznie."""
        resp = client.post(
            "/api/plugins", json={"tuya_cloud": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_post_toggles_presence_simulation_without_clobbering_scenes(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [{"name": "powitanie", "actions": [{"type": "satel_disarm"}]}],
            "reguły_ruchu": [],
            "presence_simulation": {"enabled": False, "devices": ["gniazdko"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = client.post(
            "/api/plugins", json={"presence_simulation": {"enabled": True}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("automations")
        assert saved["presence_simulation"]["enabled"] is True
        assert saved["presence_simulation"]["devices"] == ["gniazdko"]
        assert saved["scenes"][0]["name"] == "powitanie"  # nietknięte

    def test_post_enqueues_bridge_reload_only_for_automations_plugins(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [], "reguły_ruchu": [],
            "presence_simulation": {"enabled": False, "devices": ["gniazdko"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        client.post(
            "/api/plugins", json={"presence_simulation": {"enabled": True}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)


class TestDiscoveryEndpoints:
    """POST /api/discover/tuya - publiczny (tylko odczyt sieci), wykrywanie
    samo w sobie zamockowane (bridge/tuya_local.py ma własne testy
    jednostkowe z fake'ami - tutaj sprawdzamy tylko routing/obsługę błędów
    na poziomie endpointu). Odpowiednik dla eBUS
    (/api/plugins/ebus/discover*) ma własne testy w
    tests/test_plugin_routes.py::TestEbusPluginPage - dawne
    /api/discover/ebus* (blokujące i w tle) zostały usunięte jako
    zdublowane/martwe (config.html, jedyny ich odbiorca, też usunięty)."""

    def test_discover_tuya_no_auth_required(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.scan_tuya_network", lambda: [{"id": "d1", "ip": "1.2.3.4", "version": "3.4"}])
        resp = client.post("/api/discover/tuya", json={})
        assert resp.status_code == 200
        assert resp.get_json()["devices"] == [{"id": "d1", "ip": "1.2.3.4", "version": "3.4"}]

    def test_discover_tuya_error_returns_502(self, client, monkeypatch):
        def raise_error():
            raise RuntimeError("skan padł")

        monkeypatch.setattr("dashboard.app.scan_tuya_network", raise_error)
        resp = client.post("/api/discover/tuya", json={})
        assert resp.status_code == 502


class TestSatelConnectionTestEndpoint:
    def test_no_auth_required(self, client, monkeypatch):
        async def fake_test_connection(host, port, **_kwargs):
            return None

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        resp = client.post("/api/setup/test-satel-connection", json={"host": "192.168.1.10", "port": 7094})
        assert resp.status_code == 200

    def test_missing_host_returns_400(self, client):
        resp = client.post("/api/setup/test-satel-connection", json={})
        assert resp.status_code == 400

    def test_connection_error_returns_502_with_message(self, client, monkeypatch):
        async def fake_test_connection(host, port, **_kwargs):
            raise ConnectionError(f"Nie udało się połączyć z {host}:{port}")

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        resp = client.post("/api/setup/test-satel-connection", json={"host": "10.0.0.5", "port": 7094})
        assert resp.status_code == 502
        assert "10.0.0.5" in resp.get_json()["error"]

    def test_default_port_used_when_omitted(self, client, monkeypatch):
        captured = {}

        async def fake_test_connection(host, port, **_kwargs):
            captured["port"] = port

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        client.post("/api/setup/test-satel-connection", json={"host": "192.168.1.10"})
        assert captured["port"] == 7094


class TestSatelDiscoveryEndpoint:
    """/api/discover/satel - wykrywanie best-effort aktywnych stref/wyjść
    w kroku 1 kreatora (patrz bridge/satel_client.py:
    discover_active_zones_and_outputs)."""

    def test_no_auth_required(self, client, monkeypatch):
        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            return {"zones": [12], "outputs": [3]}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        resp = client.post("/api/discover/satel", json={"host": "192.168.1.10", "port": 7094})
        assert resp.status_code == 200
        assert resp.get_json() == {"zones": [12], "outputs": [3]}

    def test_missing_host_returns_400(self, client, monkeypatch):
        # Symuluje kreator PRZED zapisaniem config.yaml (config_raw puste) -
        # dopiero wtedy brak hosta w ciele żądania jest błędem; PO
        # skonfigurowaniu (patrz test_defaults_to_configured_host_and_port
        # niżej) host domyślnie bierze się z zapisanego config.yaml.
        monkeypatch.setattr("dashboard.app.config_raw", {})
        resp = client.post("/api/discover/satel", json={})
        assert resp.status_code == 400

    def test_defaults_to_configured_host_and_port_when_omitted(self, client, monkeypatch):
        """Zakładka ⚙ Ustawienia (w odróżnieniu od kreatora) NIE zna hosta
        Satela w JS - jest on w osobnej, zablokowanej hasłem zakładce
        "Połączenia" (patrz api_config_connections_get) - endpoint musi więc
        sam dociągnąć host/port z JUŻ ZAPISANEGO config.yaml, gdy żądanie
        ich nie poda (tak samo jak api_discover_ebus robi to dla mqtt_host)."""
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["host"] = host
            captured["port"] = port
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        resp = client.post("/api/discover/satel", json={})
        assert resp.status_code == 200
        assert captured == {"host": "127.0.0.1", "port": 7094}  # z CONFIG_TEMPLATE

    def test_encryption_key_not_used_when_encryption_disabled(self, client, monkeypatch):
        """Regresja: satel.encryption_enabled musi GATE'OWAĆ encryption_key
        - sam zapisany klucz bez włączonego przełącznika nie może trafić do
        biblioteki (patrz analogiczny komentarz w bridge/rules_engine.py)."""
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **kwargs):
            captured.update(kwargs)
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        monkeypatch.setattr("dashboard.app.config_raw", {
            "satel": {"host": "127.0.0.1", "port": 7094, "encryption_key": "AESKEY", "encryption_enabled": False},
        })
        resp = client.post("/api/discover/satel", json={})
        assert resp.status_code == 200
        assert captured["encryption_key"] is None

    def test_encryption_key_used_when_encryption_enabled(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **kwargs):
            captured.update(kwargs)
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        monkeypatch.setattr("dashboard.app.config_raw", {
            "satel": {"host": "127.0.0.1", "port": 7094, "encryption_key": "AESKEY", "encryption_enabled": True},
        })
        resp = client.post("/api/discover/satel", json={})
        assert resp.status_code == 200
        assert captured["encryption_key"] == "AESKEY"

    def test_explicit_host_in_body_overrides_configured(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["host"] = host
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        client.post("/api/discover/satel", json={"host": "10.0.0.9"})
        assert captured["host"] == "10.0.0.9"

    def test_connection_error_returns_502(self, client, monkeypatch):
        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            raise ConnectionError(f"Nie udało się połączyć z {host}:{port}")

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        resp = client.post("/api/discover/satel", json={"host": "10.0.0.5", "port": 7094})
        assert resp.status_code == 502
        assert "10.0.0.5" in resp.get_json()["error"]

    def test_timeout_seconds_is_capped(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["timeout_seconds"] = timeout_seconds
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        client.post("/api/discover/satel", json={"host": "192.168.1.10", "timeout_seconds": 99999})
        assert captured["timeout_seconds"] == 120

    def test_default_timeout_used_when_omitted(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["timeout_seconds"] = timeout_seconds
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        client.post("/api/discover/satel", json={"host": "192.168.1.10"})
        assert captured["timeout_seconds"] == 30.0


class TestTuyaCloudDiscoveryEndpoint:
    def test_no_auth_required(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", lambda region, aid, akey: [
            {"id": "d1", "name": "Gniazdko", "local_key": "k1", "ip": "1.2.3.4", "version": "3.4"},
        ])
        resp = client.post(
            "/api/discover/tuya-cloud",
            json={"api_region": "eu", "access_id": "aid", "access_key": "akey"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["devices"][0]["name"] == "Gniazdko"

    def test_missing_credentials_returns_400(self, client):
        resp = client.post("/api/discover/tuya-cloud", json={"api_region": "eu"})
        assert resp.status_code == 400

    def test_connection_error_returns_502(self, client, monkeypatch):
        def raise_error(region, aid, akey):
            raise ConnectionError("Błąd logowania do Tuya Cloud: zły klucz")

        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", raise_error)
        resp = client.post(
            "/api/discover/tuya-cloud",
            json={"api_region": "eu", "access_id": "aid", "access_key": "bad"},
        )
        assert resp.status_code == 502
        assert "zły klucz" in resp.get_json()["error"]

    def test_credentials_passed_through(self, client, monkeypatch):
        captured = {}

        def fake_fetch(region, aid, akey):
            captured.update(region=region, aid=aid, akey=akey)
            return []

        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", fake_fetch)
        client.post(
            "/api/discover/tuya-cloud",
            json={"api_region": "us", "access_id": "myid", "access_key": "mykey"},
        )
        assert captured == {"region": "us", "aid": "myid", "akey": "mykey"}
