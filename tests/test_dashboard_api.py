import asyncio
import base64
import io
import time
import zipfile

import pytest
import yaml
from werkzeug.datastructures import FileStorage
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
    url_template: "http://{{host}}:{{port}}/{{name}}"
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
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(tmp_path):
    from dashboard.app import app as quart_app
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
    app_module.config_path = config.db_path
    app_module.auth_guard = None

    create_app(config)
    quart_app.config["TESTING"] = True
    return quart_app.test_client()


class TestPublicEndpoints:

    async def test_healthz_ok_when_no_components_reported(self, client):
        resp = (await client.get("/healthz"))
        assert resp.status_code == 200
        assert (await resp.get_json())["ok"] is True


class TestSettingsRequireLogin:

    async def test_index_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_index_page_loads_when_authenticated_via_session(self, client):
        login = (await client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"}))
        assert login.status_code == 200
        assert (await client.get("/")).status_code == 200

    async def test_status_endpoint_requires_auth(self, client):
        assert (await client.get("/api/status")).status_code == 401
        resp = (await client.get("/api/status", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_live_dashboard_data_endpoints_require_auth(self, client):
        for path in ("/api/sensors", "/api/history?sensor=x", "/api/events",
                     "/api/alerts/active", "/api/alerts/history"):
            assert (await client.get(path)).status_code == 401, path
            resp = (await client.get(path, headers=_basic_auth_header("test_haslo_123")))
            assert resp.status_code == 200, path

    async def test_command_status_requires_auth(self, client):
        assert (await client.get("/api/commands/999")).status_code == 401
        resp = (await client.get("/api/commands/999", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 404

    async def test_satel_zone_types_requires_auth(self, client):
        assert (await client.get("/api/satel/zone-types")).status_code == 401
        resp = (await client.get("/api/satel/zone-types", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"zone_types": {}, "output_types": {}}

    async def test_plugins_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_automations_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/automations", follow_redirects=False))
        assert resp.status_code == 302

    async def test_system_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/system", follow_redirects=False))
        assert resp.status_code == 302

    async def test_config_sensors_requires_auth(self, client):
        assert (await client.get("/api/config/sensors")).status_code == 401
        resp = (await client.get("/api/config/sensors", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_satel_partitions_requires_auth(self, client):
        assert (await client.get("/api/satel/partitions")).status_code == 401
        resp = (await client.get("/api/satel/partitions", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"Dom": 1, "Garaz": 2}

    async def test_unknown_path_still_404s_not_redirected(self, client):
        resp = (await client.get("/nie-ma-takiej-strony", follow_redirects=False))
        assert resp.status_code == 404


class TestGlobalErrorHandlerNonApiRoutes:

    async def test_404_on_non_api_route_stays_404_not_500(self, client):
        resp = (await client.get("/favicon.ico"))
        assert resp.status_code == 404

    async def test_404_on_arbitrary_non_api_path_stays_404(self, client):
        resp = (await client.get("/this-page-does-not-exist"))
        assert resp.status_code == 404


class TestSessionAuthEndpoints:

    async def test_login_page_loads(self, client):
        assert (await client.get("/login")).status_code == 200

    async def test_auth_status_when_not_logged_in(self, client):
        resp = (await client.get("/api/auth/status"))
        assert resp.status_code == 200
        assert await resp.get_json() == {"logged_in": False, "configured": True, "username": None}

    async def test_login_with_correct_password_sets_session_cookie(self, client):
        resp = (await client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"}))
        assert resp.status_code == 200
        assert "stb_session" in resp.headers.get("Set-Cookie", "")

    async def test_login_with_wrong_password_returns_401(self, client):
        resp = (await client.post("/api/login", json={"username": "admin", "password": "złe hasło"}))
        assert resp.status_code == 401

    async def test_login_with_wrong_username_returns_401(self, client):
        resp = (await client.post("/api/login", json={"username": "ktoinny", "password": "test_haslo_123"}))
        assert resp.status_code == 401

    async def test_auth_status_true_after_login(self, client):
        (await client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"}))
        resp = (await client.get("/api/auth/status"))
        assert (await resp.get_json())["logged_in"] is True

    async def test_session_cookie_authorizes_protected_endpoint_without_basic_auth(self, client):
        (await client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"}))
        resp = (await client.post("/api/plugins/ebus/sensors", json=[]))
        assert resp.status_code == 200

    async def test_logout_clears_session(self, client):
        (await client.post("/api/login", json={"username": "admin", "password": "test_haslo_123"}))
        (await client.post("/api/logout"))
        resp = (await client.get("/api/auth/status"))
        assert (await resp.get_json())["logged_in"] is False

    async def test_logout_without_active_session_still_succeeds(self, client):
        resp = (await client.post("/api/logout"))
        assert resp.status_code == 200

    async def test_protected_endpoint_still_works_with_basic_auth_fallback(self, client):
        resp = (await client.post("/api/plugins/ebus/sensors", json=[], headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200


class TestProtectedArmDisarmEndpoints:
    async def test_arm_without_auth_returns_401(self, client):
        resp = (await client.post("/api/satel/arm", json={"partitions": [1]}))
        assert resp.status_code == 401

    async def test_disarm_without_auth_returns_401(self, client):
        resp = (await client.post("/api/satel/disarm", json={"partitions": [1]}))
        assert resp.status_code == 401

    async def test_arm_with_auth_enqueues_command(self, client):
        resp = (await client.post(
            "/api/satel/arm", json={"partitions": [1], "mode": 0}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "pending"
        assert "command_id" in body

    async def test_arm_command_visible_via_status_endpoint(self, client):
        resp = (await client.post(
            "/api/satel/arm", json={"partitions": [1]}, headers=_basic_auth_header("test_haslo_123"),
        ))
        command_id = (await resp.get_json())["command_id"]
        status_resp = (await client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123")))
        assert status_resp.status_code == 200
        assert (await status_resp.get_json())["status"] == "pending"
        assert (await status_resp.get_json())["command"] == "satel_arm"

    async def test_arm_defaults_to_all_configured_partitions(self, client):
        resp = (await client.post("/api/satel/arm", json={}, headers=_basic_auth_header("test_haslo_123")))
        command_id = (await resp.get_json())["command_id"]
        status_resp = (await client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123")))
        assert set((await status_resp.get_json())["params"]["partitions"]) == {1, 2}


class TestVirtualInputEndpoints:

    async def test_list_requires_auth(self, client):
        assert (await client.get("/api/satel/virtual-inputs")).status_code == 401

    async def test_list_returns_configured_names(self, client):
        resp = (await client.get("/api/satel/virtual-inputs", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"virtual_inputs": ["motion_hall"]}

    async def test_trigger_without_auth_returns_401(self, client):
        resp = (await client.post("/api/satel/virtual-inputs/motion_hall/trigger"))
        assert resp.status_code == 401

    async def test_trigger_with_auth_enqueues_command(self, client):
        resp = (await client.post(
            "/api/satel/virtual-inputs/motion_hall/trigger", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "pending"
        assert "command_id" in body

    async def test_trigger_command_visible_via_status_endpoint(self, client):
        resp = (await client.post(
            "/api/satel/virtual-inputs/motion_hall/trigger", headers=_basic_auth_header("test_haslo_123"),
        ))
        command_id = (await resp.get_json())["command_id"]
        status_resp = (await client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123")))
        assert status_resp.status_code == 200
        assert (await status_resp.get_json())["status"] == "pending"
        assert (await status_resp.get_json())["command"] == "satel_virtual_input"
        assert (await status_resp.get_json())["params"] == {"name": "motion_hall"}

    async def test_trigger_unknown_name_returns_404(self, client):
        resp = (await client.post(
            "/api/satel/virtual-inputs/nieistniejace/trigger", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 404


class TestTuyaControlEndpoints:

    async def test_on_without_auth_returns_401(self, client):
        resp = (await client.post("/api/tuya/gniazdko/on"))
        assert resp.status_code == 401

    async def test_off_without_auth_returns_401(self, client):
        resp = (await client.post("/api/tuya/gniazdko/off"))
        assert resp.status_code == 401

    async def test_on_with_auth_enqueues_command(self, client):
        resp = (await client.post("/api/tuya/gniazdko/on", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "pending"
        assert "command_id" in body

    async def test_on_command_visible_via_status_endpoint(self, client):
        resp = (await client.post("/api/tuya/gniazdko/on", headers=_basic_auth_header("test_haslo_123")))
        command_id = (await resp.get_json())["command_id"]
        status_resp = (await client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123")))
        assert status_resp.status_code == 200
        data = await status_resp.get_json()
        assert data["status"] == "pending"
        assert data["command"] == "tuya_on"
        assert data["params"] == {"name": "gniazdko"}

    async def test_off_command_has_correct_command_name(self, client):
        resp = (await client.post("/api/tuya/gniazdko/off", headers=_basic_auth_header("test_haslo_123")))
        command_id = (await resp.get_json())["command_id"]
        status_resp = (await client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123")))
        data = await status_resp.get_json()
        assert data["command"] == "tuya_off"
        assert data["params"] == {"name": "gniazdko"}

    async def test_unknown_device_returns_404(self, client):
        resp = (await client.post("/api/tuya/nieistniejace/on", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 404


class TestSatelTestOutputEndpoint:

    async def test_requires_auth(self, client):
        resp = (await client.post("/api/satel/test-output", json={"output_number": 1}))
        assert resp.status_code == 401

    async def test_library_mode_uses_freshly_saved_output_number(self, client, monkeypatch):
        captured = {}

        async def fake_test_output(host, port, integration_key, output_number, state, **_kwargs):
            captured["output_number"] = output_number

        monkeypatch.setattr("dashboard.app.test_satel_output", fake_test_output)
        resp = (await client.post(
            "/api/satel/test-output", json={"output_number": 15, "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured["output_number"] == 15

    async def test_http_mode_saved_after_client_created_is_picked_up_without_restart(self, client, monkeypatch):
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        app_module.config_store.set("config", current)

        captured = {}

        async def fake_test_http(host, port, url_template, name, **_kwargs):
            captured["name"] = name

        monkeypatch.setattr("dashboard.app.test_satel_virtual_input_http", fake_test_http)
        resp = (await client.post(
            "/api/satel/test-output", json={"name": "motion_hall", "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured["name"] == "motion_hall"

    async def test_http_mode_with_output_number_payload_does_not_leak_library_error(self, client):
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        app_module.config_store.set("config", current)

        resp = (await client.post(
            "/api/satel/test-output", json={"state": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400
        assert "wyjścia" not in (await resp.get_json())["error"]

    async def test_http_mode_finds_input_saved_via_fields_page_not_inline_config(self, client, monkeypatch):
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        current["satel"]["virtual_inputs"]["inputs"] = {}
        app_module.config_store.set("config", current)
        app_module.config_store.set("satel_fields", {"virtual_inputs": {"zmywarka": 5}})

        captured = {}

        async def fake_test_http(host, port, url_template, name, **_kwargs):
            captured["name"] = name

        monkeypatch.setattr("dashboard.app.test_satel_virtual_input_http", fake_test_http)
        resp = (await client.post(
            "/api/satel/test-output", json={"name": "zmywarka", "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured["name"] == "zmywarka"

    async def test_http_mode_ignores_stale_stored_url_template(self, client, monkeypatch):
        import dashboard.app as app_module
        current = app_module.config_store.get("config")
        current["satel"]["virtual_inputs"]["mode"] = "http"
        current["satel"]["virtual_inputs"]["url_template"] = (
            "http://{host}:{port}/input.cgi?key={key}&input={input_no}"
        )
        app_module.config_store.set("config", current)

        captured = {}

        async def fake_test_http(host, port, url_template, name, **_kwargs):
            captured["url_template"] = url_template

        monkeypatch.setattr("dashboard.app.test_satel_virtual_input_http", fake_test_http)
        resp = (await client.post(
            "/api/satel/test-output", json={"name": "motion_hall", "state": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured["url_template"] == "http://{host}:{port}/{name}"


class TestSystemLogsLoggersEndpoint:

    async def test_requires_auth(self, client):
        resp = (await client.get("/api/system/logs/loggers"))
        assert resp.status_code == 401

    async def test_returns_distinct_logger_names(self, client):
        import dashboard.app as app_module
        app_module.history.write_log("bridge", "bridge.satel", "INFO", "a")
        app_module.history.write_log("bridge", "bridge.satel", "WARNING", "b")
        app_module.history.write_log("bridge", "bridge.tuya_local", "DEBUG", "c")

        resp = (await client.get("/api/system/logs/loggers", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == ["bridge.satel", "bridge.tuya_local"]

    async def test_filters_by_component(self, client):
        import dashboard.app as app_module
        app_module.history.write_log("bridge", "bridge.satel", "INFO", "a")
        app_module.history.write_log("dashboard", "dashboard.app", "INFO", "b")

        resp = (await client.get(
            "/api/system/logs/loggers?component=dashboard", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert await resp.get_json() == ["dashboard.app"]


class TestSystemRestartEndpoint:

    def _patch_exit(self, monkeypatch):
        exit_calls = []
        monkeypatch.setattr("dashboard.app.os._exit", lambda code: exit_calls.append(code))

        started_timers = []

        class FakeTimer:
            def __init__(self, interval, fn):
                self.interval = interval
                self.fn = fn

            def start(self):
                started_timers.append(self)

        monkeypatch.setattr("dashboard.app.threading.Timer", FakeTimer)
        return exit_calls, started_timers

    async def test_requires_auth(self, client, monkeypatch):
        self._patch_exit(monkeypatch)
        resp = (await client.post("/api/system/restart"))
        assert resp.status_code == 401

    async def test_enqueues_restart_command(self, client, monkeypatch):
        self._patch_exit(monkeypatch)
        resp = (await client.post("/api/system/restart", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert "Restartuję" in (await resp.get_json())["message"]

        command_id = (await resp.get_json())["command_id"]
        status_resp = (await client.get(f"/api/commands/{command_id}", headers=_basic_auth_header("test_haslo_123")))
        assert (await status_resp.get_json())["command"] == "restart_process"

    async def test_schedules_delayed_self_exit_without_exiting_immediately(self, client, monkeypatch):
        exit_calls, started_timers = self._patch_exit(monkeypatch)
        (await client.post("/api/system/restart", headers=_basic_auth_header("test_haslo_123")))
        assert len(started_timers) == 1
        assert started_timers[0].interval == 0.5
        assert exit_calls == []


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestConfigBackupZipEndpoints:

    async def test_export_requires_auth(self, client):
        assert (await client.get("/api/config/export-zip")).status_code == 401

    async def test_export_returns_valid_zip_with_config_yaml(self, client):
        resp = (await client.get("/api/config/export-zip", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        with zipfile.ZipFile(io.BytesIO(await resp.get_data())) as zf:
            names = zf.namelist()
            assert "config.yaml" in names
            content = yaml.safe_load(zf.read("config.yaml"))
            assert content["satel"]["host"] == "127.0.0.1"

    async def test_import_requires_auth(self, client):
        resp = (await client.post(
            "/api/config/import-zip",
            files={"file": FileStorage(stream=io.BytesIO(_zip_bytes({"config.yaml": "a: 1"})), filename="backup.zip")},
        ))
        assert resp.status_code == 401

    async def test_import_without_file_is_error(self, client):
        resp = (await client.post("/api/config/import-zip", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400

    async def test_import_non_zip_file_is_error(self, client):
        resp = (await client.post(
            "/api/config/import-zip",
            files={"file": FileStorage(stream=io.BytesIO(b"nie jestem zipem"), filename="backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400
        assert "zip" in (await resp.get_json())["error"].lower()

    async def test_import_valid_roundtrip_from_export(self, client):
        export_resp = (await client.get("/api/config/export-zip", headers=_basic_auth_header("test_haslo_123")))
        import_resp = (await client.post(
            "/api/config/import-zip",
            files={"file": FileStorage(stream=io.BytesIO(await export_resp.get_data()), filename="backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert import_resp.status_code == 200
        assert "config" in (await import_resp.get_json())["imported"]

    async def test_import_rejects_config_missing_satel_fields_without_writing(self, client):
        broken = _zip_bytes({
            "config.yaml": "satel: {}\ntuya:\n  devices: {}\nreguły_ruchu: []\nebus:\n  mqtt_host: x\n",
        })
        resp = (await client.post(
            "/api/config/import-zip",
            files={"file": FileStorage(stream=io.BytesIO(broken), filename="backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400
        assert "satel" in (await resp.get_json())["error"]

        export_resp = (await client.get("/api/config/export-zip", headers=_basic_auth_header("test_haslo_123")))
        with zipfile.ZipFile(io.BytesIO(await export_resp.get_data())) as zf:
            content = yaml.safe_load(zf.read("config.yaml"))
        assert content["satel"]["host"] == "127.0.0.1"

    async def test_import_rejects_broken_yaml_syntax(self, client):
        broken = _zip_bytes({"config.yaml": "satel: [unclosed"})
        resp = (await client.post(
            "/api/config/import-zip",
            files={"file": FileStorage(stream=io.BytesIO(broken), filename="backup.zip")},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400


class TestAutomationContextEndpoint:

    async def test_context_requires_auth(self, client):
        assert (await client.get("/api/config/automation-context")).status_code == 401

    async def test_context_returns_configured_device_and_virtual_input_names(self, client):
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["tuya_devices"] == ["gniazdko"]
        assert data["virtual_inputs"] == ["motion_hall"]

    async def test_context_returns_satel_output_names_for_target_system_ui(self, client):
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["satel_outputs"] == ["Syrena"]

    async def test_context_returns_satel_zone_names_for_zone_number_ui(self, client):
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["satel_zones"] == [{"name": "Salon", "number": 12}]

    async def test_context_returns_satel_partitions_for_scene_action_ui(self, client):
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["satel_partitions"] == {"Dom": 1, "Garaz": 2}

    async def test_context_includes_cloud_only_devices(self, client):
        (await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "T & H Sensor", "id": "cloud1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert "gniazdko" in data["tuya_devices"]
        assert "T & H Sensor" in data["tuya_devices"]

    async def test_context_includes_status_keys_for_condition_builder(self, client):
        import dashboard.app as app_module
        app_module.history.set_status("satel_input:3", "satel_input", "naruszone", label="Salon")
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert {"key": "satel_input:3", "label": "Salon", "category": "satel_input"} in data["status_keys"]

    async def test_context_includes_motion_rule_names_for_chaining_ui(self, client):
        (await client.post(
            "/api/config/automations",
            json={
                "scenes": [],
                "motion_rules": [
                    {"zone_number": "12", "name": "wejscie_do_salonu", "target_device": "gniazdko", "duration_seconds": "30"},
                ],
                "presence_simulation": {"enabled": False},
            },
            headers=_basic_auth_header("test_haslo_123"),
        ))
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["motion_rule_names"] == ["wejscie_do_salonu"]

    async def test_context_includes_dimmer_devices_for_brightness_field_gating(self, client):
        (await client.post(
            "/api/plugins/tuya/devices",
            json={"tuya_devices": [
                {"name": "lampa_scienna", "id": "dimmer1", "ip": "1.2.3.9", "local_key": "k9",
                 "type": "dimmer", "dimmer_dp": "3"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["dimmer_devices"] == ["lampa_scienna"]

    async def test_context_includes_homewhiz_appliances_for_device_state_source(self, client):
        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["homewhiz_appliances"] == ["Zmywarka"]

    async def test_context_homewhiz_appliances_empty_when_unconfigured(self, client):
        data = await (await client.get(
            "/api/config/automation-context", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["homewhiz_appliances"] == []


class TestAutomationsEndpoint:
    async def test_get_automations_requires_auth(self, client):
        resp = (await client.get("/api/config/automations", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"scenes": [], "motion_rules": [], "presence_simulation": {"enabled": False}}

    async def test_post_automations_without_auth_returns_401(self, client):
        payload = {"scenes": [], "motion_rules": [], "presence_simulation": {}}
        resp = (await client.post("/api/config/automations", json=payload))
        assert resp.status_code == 401

    async def test_post_automations_with_wrong_auth_returns_401(self, client):
        payload = {"scenes": [], "motion_rules": [], "presence_simulation": {}}
        resp = (await client.post("/api/config/automations", json=payload, headers=_basic_auth_header("złe_hasło")))
        assert resp.status_code == 401

    async def test_post_automations_with_correct_auth_saves_scene(self, client):
        payload = {
            "scenes": [{"name": "test_scena", "actions": [{"type": "tuya_on", "device": "gniazdko"}]}],
            "motion_rules": [],
            "presence_simulation": {"enabled": False},
        }
        resp = (await client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["saved"] == {"scenes": 1, "motion_rules": 0}

    async def test_post_automations_enqueues_bridge_reload_command(self, client):
        import dashboard.app as app_module

        payload = {"scenes": [], "motion_rules": [], "presence_simulation": {}}
        (await client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123")))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

    async def test_post_automations_validates_scene_fields(self, client):
        payload = {"scenes": [{"name": "", "actions": []}], "motion_rules": [], "presence_simulation": {}}
        resp = (await client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400
        assert "details" in await resp.get_json()

    async def test_saved_automations_persist_and_are_readable(self, client):
        payload = {
            "scenes": [{"name": "scena1", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [{"zone_number": "5", "scene": "scena1"}],
            "presence_simulation": {
                "enabled": True, "devices": ["gniazdko"],
                "min_interval_minutes": "5", "max_interval_minutes": "10",
            },
        }
        (await client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123")))
        resp = (await client.get("/api/config/automations", headers=_basic_auth_header("test_haslo_123")))
        data = await resp.get_json()
        assert data["scenes"][0]["name"] == "scena1"
        assert data["motion_rules"][0]["scene"] == "scena1"
        assert data["presence_simulation"]["enabled"] is True
        assert data["presence_simulation"]["devices"] == ["gniazdko"]

    async def test_post_automations_writes_automations_document(self, client):
        import dashboard.app as app_module

        payload = {
            "scenes": [{"name": "s", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [], "presence_simulation": {},
        }
        (await client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert app_module.config_store.get("automations")["scenes"][0]["name"] == "s"

    async def test_post_automations_does_not_touch_config_document(self, client):
        import dashboard.app as app_module

        before = app_module.config_store.get("config")
        payload = {
            "scenes": [{"name": "s", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [], "presence_simulation": {},
        }
        (await client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123")))
        after = app_module.config_store.get("config")
        assert before == after


class TestLocationEndpoint:

    async def test_get_without_auth_returns_401(self, client):
        assert (await client.get("/api/config/location")).status_code == 401

    async def test_get_with_auth_returns_empty_when_unset(self, client):
        resp = (await client.get("/api/config/location", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {}

    async def test_post_without_auth_returns_401(self, client):
        resp = (await client.post("/api/config/location", json={"latitude": "52.23", "longitude": "21.01"}))
        assert resp.status_code == 401

    async def test_post_sets_location(self, client):
        import dashboard.app as app_module

        payload = {"latitude": "52.23", "longitude": "21.01"}
        resp = (await client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["location"] == {"latitude": 52.23, "longitude": 21.01}

    async def test_get_reflects_saved_location(self, client):
        payload = {"latitude": "52.23", "longitude": "21.01"}
        (await client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123")))
        resp = (await client.get("/api/config/location", headers=_basic_auth_header("test_haslo_123")))
        assert await resp.get_json() == {"latitude": 52.23, "longitude": 21.01}

    async def test_post_validates_out_of_range_latitude(self, client):
        payload = {"latitude": "195", "longitude": "21.01"}
        resp = (await client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400

    async def test_post_blank_clears_existing_location(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/config/location", json={"latitude": "52.23", "longitude": "21.01"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/config/location", json={"latitude": "", "longitude": ""},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        saved = app_module.config_store.get("config")
        assert "location" not in saved

    async def test_post_preserves_other_config_sections(self, client):
        import dashboard.app as app_module

        payload = {"latitude": "52.23", "longitude": "21.01"}
        (await client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123")))
        saved = app_module.config_store.get("config")
        assert saved["satel"]["host"] == "127.0.0.1"
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"

    async def test_post_enqueues_bridge_reload_not_restart(self, client):
        import dashboard.app as app_module

        payload = {"latitude": "52.23", "longitude": "21.01"}
        resp = (await client.post("/api/config/location", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert "bez restartu" in (await resp.get_json())["message"].lower()
        commands = [c["command"] for c in app_module.history.get_pending_commands()]
        assert "reload_config" in commands


class TestPluginsEndpoint:

    async def test_get_requires_auth(self, client):
        assert (await client.get("/api/plugins")).status_code == 401

    async def test_get_returns_satel_and_tuya_local_enabled_by_default(self, client):
        by_id = {p["id"]: p for p in await (await client.get("/api/plugins", headers=_basic_auth_header("test_haslo_123"))).get_json()}
        assert by_id["satel"]["enabled"] is True
        assert by_id["tuya_local"]["enabled"] is True
        assert by_id["home_assistant"]["enabled"] is False
        assert by_id["presence_simulation"]["enabled"] is False

    async def test_get_never_leaks_secrets(self, client):
        text = await (await client.get("/api/plugins", headers=_basic_auth_header("test_haslo_123"))).get_data(as_text=True)
        assert '"x"' not in text
        assert '"k"' not in text

    async def test_get_includes_config_url_per_plugin(self, client):
        by_id = {p["id"]: p for p in await (await client.get("/api/plugins", headers=_basic_auth_header("test_haslo_123"))).get_json()}
        assert by_id["satel"]["config_url"] == "/plugins/satel"
        assert by_id["homekit"]["config_url"] == "/plugins/homekit"
        assert by_id["tuya_zigbee"]["config_url"] == "/plugins/tuya"

    async def test_load_errors_endpoint_requires_auth(self, client):
        assert (await client.get("/api/plugins/load-errors")).status_code == 401

    async def test_load_errors_endpoint_empty_for_healthy_install(self, client):
        resp = (await client.get("/api/plugins/load-errors", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == []

    async def test_post_without_auth_returns_401(self, client):
        resp = (await client.post("/api/plugins", json={"home_assistant": {"enabled": True}}))
        assert resp.status_code == 401

    async def test_post_unknown_plugin_id_rejected(self, client):
        resp = (await client.post(
            "/api/plugins", json={"does_not_exist": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_post_enabling_unconfigured_unifi_protect_is_rejected(self, client):
        resp = (await client.post(
            "/api/plugins", json={"unifi_protect": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_post_enables_mcp_server_without_prior_config(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins", json={"mcp_server": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["mcp_server"]["enabled"] is True

    async def test_post_enables_homekit_without_prior_config(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins", json={"homekit": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["homekit"]["enabled"] is True

    async def test_post_toggles_satel_enabled_without_touching_host(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins", json={"satel": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert "wymagają pełnego restartu" in (await resp.get_json())["message"]
        saved = app_module.config_store.get("config")
        assert saved["satel"]["enabled"] is False
        assert saved["satel"]["host"] == "127.0.0.1"

    async def test_post_toggles_ebus_enabled(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins", json={"ebus": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["ebus"]["enabled"] is False
        assert saved["ebus"]["mqtt_host"] == "127.0.0.1"

    async def test_post_updates_satel_poll_interval_live(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins", json={"satel": {"status_poll_interval_seconds": 25}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert "bez restartu" in (await resp.get_json())["message"]
        saved = app_module.config_store.get("config")
        assert saved["satel"]["status_poll_interval_seconds"] == 25
        assert saved["satel"]["host"] == "127.0.0.1"

    async def test_post_updates_tuya_local_poll_interval_without_restart_message(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins", json={"tuya_local": {"status_poll_interval_seconds": 45}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        message = (await resp.get_json())["message"]
        assert "bez restartu" in message
        assert "wymagają pełnego restartu" not in message
        saved = app_module.config_store.get("config")
        assert saved["tuya"]["status_poll_interval_seconds"] == 45
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"

    async def test_post_rejects_poll_interval_below_minimum(self, client):
        resp = (await client.post(
            "/api/plugins", json={"satel": {"status_poll_interval_seconds": 1}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_post_enables_home_assistant_without_touching_satel_host(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins", json={"home_assistant": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["home_assistant"]["enabled"] is True
        assert saved["satel"]["host"] == "127.0.0.1"
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"

    async def test_post_disables_mqtt_publish(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins", json={"mqtt_publish": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        saved = app_module.config_store.get("config")
        assert saved["mqtt_publish"]["enabled"] is False

    async def test_post_enabling_tuya_cloud_without_credentials_is_rejected(self, client):
        resp = (await client.post(
            "/api/plugins", json={"tuya_cloud": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_post_toggles_presence_simulation_without_clobbering_scenes(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [{"name": "powitanie", "actions": [{"type": "satel_disarm"}]}],
            "reguły_ruchu": [],
            "presence_simulation": {"enabled": False, "devices": ["gniazdko"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.post(
            "/api/plugins", json={"presence_simulation": {"enabled": True}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("automations")
        assert saved["presence_simulation"]["enabled"] is True
        assert saved["presence_simulation"]["devices"] == ["gniazdko"]
        assert saved["scenes"][0]["name"] == "powitanie"

    async def test_post_enqueues_bridge_reload_only_for_automations_plugins(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [], "reguły_ruchu": [],
            "presence_simulation": {"enabled": False, "devices": ["gniazdko"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        (await client.post(
            "/api/plugins", json={"presence_simulation": {"enabled": True}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)


class TestDiscoveryEndpoints:

    async def test_discover_tuya_requires_auth_once_configured(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.scan_tuya_network", lambda: [{"id": "d1", "ip": "1.2.3.4", "version": "3.4"}])
        assert (await client.post("/api/discover/tuya", json={})).status_code == 401
        resp = (await client.post("/api/discover/tuya", json={}, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["devices"] == [{"id": "d1", "ip": "1.2.3.4", "version": "3.4"}]

    async def test_discover_tuya_error_returns_502(self, client, monkeypatch):
        def raise_error():
            raise RuntimeError("skan padł")

        monkeypatch.setattr("dashboard.app.scan_tuya_network", raise_error)
        resp = (await client.post("/api/discover/tuya", json={}, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 502

    async def test_discover_tuya_does_not_block_event_loop(self, client, monkeypatch):
        def slow_scan():
            time.sleep(0.3)
            return [{"id": "d1", "ip": "1.2.3.4", "version": "3.4"}]

        monkeypatch.setattr("dashboard.app.scan_tuya_network", slow_scan)

        ticks: list[float] = []
        scan_finished_at: list[float] = []

        async def do_scan():
            resp = await client.post("/api/discover/tuya", json={}, headers=_basic_auth_header("test_haslo_123"))
            scan_finished_at.append(time.monotonic())
            return resp

        async def ticker():
            for _ in range(10):
                await asyncio.sleep(0.03)
                ticks.append(time.monotonic())

        resp, _ = await asyncio.gather(do_scan(), ticker())

        assert resp.status_code == 200
        ticks_during_scan = [t for t in ticks if t < scan_finished_at[0]]
        assert len(ticks_during_scan) >= 3, (
            "Pętla zdarzeń była zablokowana podczas skanu Tuya - "
            "żaden tick nie zdążył wykonać się równolegle ze skanem"
        )


class TestSatelConnectionTestEndpoint:
    async def test_requires_auth_once_configured(self, client, monkeypatch):
        async def fake_test_connection(host, port, **_kwargs):
            return None

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        payload = {"host": "192.168.1.10", "port": 7094}
        assert (await client.post("/api/setup/test-satel-connection", json=payload)).status_code == 401
        resp = (await client.post(
            "/api/setup/test-satel-connection", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200

    async def test_missing_host_returns_400(self, client):
        resp = (await client.post(
            "/api/setup/test-satel-connection", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_connection_error_returns_502_with_message(self, client, monkeypatch):
        async def fake_test_connection(host, port, **_kwargs):
            raise ConnectionError(f"Nie udało się połączyć z {host}:{port}")

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        resp = (await client.post(
            "/api/setup/test-satel-connection", json={"host": "10.0.0.5", "port": 7094},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 502
        assert "10.0.0.5" in (await resp.get_json())["error"]

    async def test_default_port_used_when_omitted(self, client, monkeypatch):
        captured = {}

        async def fake_test_connection(host, port, **_kwargs):
            captured["port"] = port

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        (await client.post(
            "/api/setup/test-satel-connection", json={"host": "192.168.1.10"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert captured["port"] == 7094


class TestSatelDiscoveryEndpoint:

    async def test_requires_auth_once_configured(self, client, monkeypatch):
        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            return {"zones": [12], "outputs": [3]}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        payload = {"host": "192.168.1.10", "port": 7094}
        assert (await client.post("/api/discover/satel", json=payload)).status_code == 401
        resp = (await client.post(
            "/api/discover/satel", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert await resp.get_json() == {"zones": [12], "outputs": [3]}

    async def test_missing_host_returns_400(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.config_raw", {})
        resp = (await client.post(
            "/api/discover/satel", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_defaults_to_configured_host_and_port_when_omitted(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["host"] = host
            captured["port"] = port
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        resp = (await client.post(
            "/api/discover/satel", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured == {"host": "127.0.0.1", "port": 7094}

    async def test_encryption_key_not_used_when_encryption_disabled(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **kwargs):
            captured.update(kwargs)
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        monkeypatch.setattr("dashboard.app.config_raw", {
            "satel": {"host": "127.0.0.1", "port": 7094, "encryption_key": "AESKEY", "encryption_enabled": False},
        })
        resp = (await client.post(
            "/api/discover/satel", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured["encryption_key"] is None

    async def test_encryption_key_used_when_encryption_enabled(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **kwargs):
            captured.update(kwargs)
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        monkeypatch.setattr("dashboard.app.config_raw", {
            "satel": {"host": "127.0.0.1", "port": 7094, "encryption_key": "AESKEY", "encryption_enabled": True},
        })
        resp = (await client.post(
            "/api/discover/satel", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured["encryption_key"] == "AESKEY"

    async def test_explicit_host_in_body_overrides_configured(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["host"] = host
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        (await client.post(
            "/api/discover/satel", json={"host": "10.0.0.9"}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert captured["host"] == "10.0.0.9"

    async def test_connection_error_returns_502(self, client, monkeypatch):
        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            raise ConnectionError(f"Nie udało się połączyć z {host}:{port}")

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        resp = (await client.post(
            "/api/discover/satel", json={"host": "10.0.0.5", "port": 7094},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 502
        assert "10.0.0.5" in (await resp.get_json())["error"]

    async def test_timeout_seconds_is_capped(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["timeout_seconds"] = timeout_seconds
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        (await client.post(
            "/api/discover/satel", json={"host": "192.168.1.10", "timeout_seconds": 99999},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert captured["timeout_seconds"] == 120

    async def test_default_timeout_used_when_omitted(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0, **_kwargs):
            captured["timeout_seconds"] = timeout_seconds
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        (await client.post(
            "/api/discover/satel", json={"host": "192.168.1.10"}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert captured["timeout_seconds"] == 30.0


class TestTuyaCloudDiscoveryEndpoint:
    async def test_requires_auth_once_configured(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", lambda region, aid, akey: [
            {"id": "d1", "name": "Gniazdko", "local_key": "k1", "ip": "1.2.3.4", "version": "3.4"},
        ])
        payload = {"api_region": "eu", "access_id": "aid", "access_key": "akey"}
        assert (await client.post("/api/discover/tuya-cloud", json=payload)).status_code == 401
        resp = (await client.post(
            "/api/discover/tuya-cloud", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["devices"][0]["name"] == "Gniazdko"

    async def test_missing_credentials_returns_400(self, client):
        resp = (await client.post(
            "/api/discover/tuya-cloud", json={"api_region": "eu"}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_connection_error_returns_502(self, client, monkeypatch):
        def raise_error(region, aid, akey):
            raise ConnectionError("Błąd logowania do Tuya Cloud: zły klucz")

        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", raise_error)
        resp = (await client.post(
            "/api/discover/tuya-cloud",
            json={"api_region": "eu", "access_id": "aid", "access_key": "bad"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 502
        assert "zły klucz" in (await resp.get_json())["error"]

    async def test_credentials_passed_through(self, client, monkeypatch):
        captured = {}

        def fake_fetch(region, aid, akey):
            captured.update(region=region, aid=aid, akey=akey)
            return []

        monkeypatch.setattr("dashboard.app.fetch_tuya_cloud_devices", fake_fetch)
        (await client.post(
            "/api/discover/tuya-cloud",
            json={"api_region": "us", "access_id": "myid", "access_key": "mykey"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert captured == {"region": "us", "aid": "myid", "akey": "mykey"}


class TestFlowsImportFromMotionRules:
    async def test_requires_auth(self, client):
        resp = (await client.post("/api/flows/import-from-motion-rules"))
        assert resp.status_code == 401

    async def test_no_rules_returns_empty(self, client):
        resp = (await client.post(
            "/api/flows/import-from-motion-rules", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert await resp.get_json() == {"flows": [], "warnings": [], "imported_rule_indices": []}

    async def test_convertible_rule_is_returned_as_flow(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.config_raw", {
            "reguły_ruchu": [
                {"name": "swiatlo_na_taras", "source": "satel_zone", "zone_number": 5,
                 "target_device": "lampa_taras", "duration_seconds": 60},
            ],
        })
        resp = (await client.post(
            "/api/flows/import-from-motion-rules", headers=_basic_auth_header("test_haslo_123"),
        ))
        data = await resp.get_json()
        assert resp.status_code == 200
        assert data["warnings"] == []
        assert len(data["flows"]) == 1
        assert data["flows"][0]["name"] == "Import: swiatlo_na_taras"
        assert data["imported_rule_indices"] == [0]

    async def test_unsupported_rule_is_skipped_with_warning(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.config_raw", {
            "reguły_ruchu": [
                {"name": "wielo", "sources": [{"zone_number": 1}, {"zone_number": 2}], "target_device": "x"},
            ],
        })
        resp = (await client.post(
            "/api/flows/import-from-motion-rules", headers=_basic_auth_header("test_haslo_123"),
        ))
        data = await resp.get_json()
        assert resp.status_code == 200
        assert data["flows"] == []
        assert any("sources/actions" in w for w in data["warnings"])

    async def test_virtual_input_rule_converts_to_device_node(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.config_raw", {
            "reguły_ruchu": [
                {"name": "wejscie_ip", "source": "satel_zone", "zone_number": 1, "virtual_input": "kamera",
                 "duration_seconds": 30},
            ],
        })
        resp = (await client.post(
            "/api/flows/import-from-motion-rules", headers=_basic_auth_header("test_haslo_123"),
        ))
        data = await resp.get_json()
        assert resp.status_code == 200
        assert data["warnings"] == []
        assert len(data["flows"]) == 1
        device_nodes = [n for n in data["flows"][0]["nodes"] if n["type"] == "device"]
        assert device_nodes[0]["config"]["target_system"] == "satel_virtual_input"

    async def test_does_not_persist_anything(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.config_raw", {
            "reguły_ruchu": [
                {"name": "swiatlo_na_taras", "source": "satel_zone", "zone_number": 5,
                 "target_device": "lampa_taras", "duration_seconds": 60},
            ],
        })
        (await client.post(
            "/api/flows/import-from-motion-rules", headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.get("/api/config/flows", headers=_basic_auth_header("test_haslo_123")))
        assert (await resp.get_json())["flows"] == []


class TestFlowsMarkMotionRulesImported:
    async def test_requires_auth(self, client):
        resp = (await client.post("/api/flows/mark-motion-rules-imported", json={"rule_indices": [0]}))
        assert resp.status_code == 401

    async def test_marks_rule_disabled_and_imported(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [],
            "reguły_ruchu": [
                {"name": "swiatlo_na_taras", "source": "satel_zone", "zone_number": 5,
                 "target_device": "lampa_taras", "duration_seconds": 60},
                {"name": "inna", "source": "satel_zone", "zone_number": 6, "target_device": "lampa2"},
            ],
            "presence_simulation": {"enabled": False},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.post(
            "/api/flows/mark-motion-rules-imported", json={"rule_indices": [0]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["marked"] == 1

        saved_rules = app_module.config_store.get("automations")["reguły_ruchu"]
        assert saved_rules[0]["enabled"] is False
        assert saved_rules[0]["imported_to_flows"] is True
        assert "imported_to_flows" not in saved_rules[1]

    async def test_out_of_range_index_is_ignored(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [],
            "reguły_ruchu": [
                {"name": "swiatlo_na_taras", "source": "satel_zone", "zone_number": 5,
                 "target_device": "lampa_taras", "duration_seconds": 60},
            ],
            "presence_simulation": {"enabled": False},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.post(
            "/api/flows/mark-motion-rules-imported", json={"rule_indices": [0, 5]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["marked"] == 1

    async def test_invalid_payload_is_rejected(self, client):
        resp = (await client.post(
            "/api/flows/mark-motion-rules-imported", json={"rule_indices": "not-a-list"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400


class TestSetupOnlyPathsPublicOnlyBeforeConfiguration:
    def test_setup_paths_public_when_not_configured(self, monkeypatch):
        import dashboard.app as app_module

        monkeypatch.setattr(app_module, "is_configured", lambda: False)
        for path in app_module._SETUP_ONLY_PUBLIC_PATHS:
            assert app_module._is_public_path(path) is True

    def test_setup_paths_require_auth_when_configured(self, monkeypatch):
        import dashboard.app as app_module

        monkeypatch.setattr(app_module, "is_configured", lambda: True)
        for path in app_module._SETUP_ONLY_PUBLIC_PATHS:
            assert app_module._is_public_path(path) is False
