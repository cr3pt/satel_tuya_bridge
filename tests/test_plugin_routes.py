"""Testy dla plugins/*/routes.py - własne strony konfiguracji wtyczek
(Satel/Tuya lokalne/eBUS), patrz plugins/__init__.py po opis wzorca."""
import base64

import pytest
from werkzeug.security import generate_password_hash

from bridge.config import BridgeConfig

CONFIG_TEMPLATE = """
satel:
  host: "127.0.0.1"
  port: 7094
  integration_key: "x"
  partitions: {{Dom: 1}}
  monitored_zones: {{Salon: 12}}
  virtual_inputs:
    port: 8888
    url_template: "http://{{host}}:{{port}}/x"
    inputs: {{}}
tuya:
  devices:
    gniazdko:
      id: "d1"
      ip: "1.2.3.4"
      local_key: "k"
      type: socket
reguły_ruchu: []
ebus:
  mqtt_host: "127.0.0.1"
  sensors_file: "sensors.yaml"
dashboard:
  db_path: "{db_path}"
  auth:
    password_hash: "{password_hash}"
"""


def _basic_auth_header(password: str, username: str = "admin") -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(tmp_path):
    from dashboard.app import app as flask_app
    from dashboard.app import create_app

    (tmp_path / "sensors.yaml").write_text("sensors: []\n", encoding="utf-8")
    password_hash = generate_password_hash("test_haslo_123")
    config_text = CONFIG_TEMPLATE.format(db_path=str(tmp_path / "history.db"), password_hash=password_hash)
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")

    config = BridgeConfig.load(tmp_path / "config.yaml")

    import dashboard.app as app_module
    app_module.config_path = config.db_path
    # create_app() od teraz PONOWNIE UŻYWA istniejącego auth_guard (żeby
    # zapis configu z panelu nie wylogowywał wszystkich - patrz
    # AuthGuard.update_config) - dla izolacji testów między sobą trzeba
    # więc jawnie wymusić świeży obiekt na starcie każdego testu.
    app_module.auth_guard = None

    create_app(config)
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


class TestSatelPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        """Strony wtyczek (/plugins/<id>) TERAZ wymagają zalogowania -
        patrz dashboard/app.py: _require_login_for_settings_and_control -
        wcześniej były publiczne, chronione było tylko zapisywanie."""
        resp = client.get("/plugins/satel", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/satel", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_get_fields_requires_auth(self, client):
        assert client.get("/api/plugins/satel/fields").status_code == 401
        resp = client.get("/api/plugins/satel/fields", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        data = resp.get_json()
        assert {"name": "Dom", "number": 1} in data["partitions"]
        assert {"name": "Salon", "number": 12, "type": ""} in data["monitored_zones"]

    def test_post_fields_without_auth_returns_401(self, client):
        resp = client.post("/api/plugins/satel/fields", json={})
        assert resp.status_code == 401

    def test_post_fields_persists_and_is_readable(self, client):
        import dashboard.app as app_module

        payload = {"partitions": [{"name": "Piwnica", "number": "9"}]}
        resp = client.post("/api/plugins/satel/fields", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        fields = client.get("/api/plugins/satel/fields", headers=_basic_auth_header("test_haslo_123")).get_json()
        assert {"name": "Piwnica", "number": 9} in fields["partitions"]
        assert app_module.config_store.get("satel_fields")["partitions"] == {"Piwnica": 9}

    def test_post_fields_enqueues_bridge_reload(self, client):
        import dashboard.app as app_module

        client.post("/api/plugins/satel/fields", json={}, headers=_basic_auth_header("test_haslo_123"))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

    def test_scan_requires_auth_and_handles_connection_error(self, client):
        assert client.post("/api/plugins/satel/scan", json={"host": "127.0.0.1", "timeout_seconds": 0.1}).status_code == 401
        resp = client.post(
            "/api/plugins/satel/scan", json={"host": "127.0.0.1", "timeout_seconds": 0.1},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 502

    def test_settings_get_requires_auth(self, client):
        assert client.get("/api/plugins/satel/settings").status_code == 401
        resp = client.get("/api/plugins/satel/settings", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"enabled": True, "log_level": ""}

    def test_settings_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/satel/settings", json={"log_level": "DEBUG"}).status_code == 401

    def test_settings_post_sets_log_level_without_touching_host(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins/satel/settings", json={"log_level": "debug"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert "bez restartu" in resp.get_json()["message"]
        saved = app_module.config_store.get("config")["satel"]
        assert saved["log_level"] == "DEBUG"
        assert saved["host"] == "127.0.0.1"  # nietknięte

    def test_settings_post_rejects_invalid_log_level(self, client):
        resp = client.post(
            "/api/plugins/satel/settings", json={"log_level": "LOUD"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_settings_post_toggles_enabled_and_reports_restart_required(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins/satel/settings", json={"enabled": False},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert "restartu" in resp.get_json()["message"]
        assert app_module.config_store.get("config")["satel"]["enabled"] is False

    def test_settings_post_does_not_clobber_fields_saved_via_satel_fields_doc(self, client):
        """Po zapisie przez /api/plugins/satel/fields, monitored_zones/
        partitions żyją w OSOBNYM dokumencie satel_fields (nadpisują to,
        co w configu - patrz BridgeConfig.load) - zapis /settings (log_level/
        enabled) nie może cofnąć tej zmiany, bo bierze/zapisuje sekcję
        "satel" wprost z dokumentu "config", nie ze scalonego widoku."""
        import dashboard.app as app_module

        client.post(
            "/api/plugins/satel/fields", json={"partitions": [{"name": "Piwnica", "number": "9"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/satel/settings", json={"log_level": "INFO"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert app_module.config_store.get("config")["satel"]["log_level"] == "INFO"
        # scalony widok wciąż pokazuje partycję zapisaną przez /fields,
        # NIE tę wciąż leżącą (nieaktualną) w samym dokumencie "config"
        fields = client.get("/api/plugins/satel/fields", headers=_basic_auth_header("test_haslo_123")).get_json()
        assert {"name": "Piwnica", "number": 9} in fields["partitions"]


class TestTuyaLocalPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/tuya", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/tuya", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_get_devices_requires_auth(self, client):
        assert client.get("/api/plugins/tuya/devices").status_code == 401
        data = client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123")).get_json()
        assert any(d["name"] == "gniazdko" and d["ip"] == "1.2.3.4" for d in data["devices"])

    def test_post_devices_without_auth_returns_401(self, client):
        resp = client.post("/api/plugins/tuya/devices", json={"tuya_devices": []})
        assert resp.status_code == 401

    def test_post_devices_persists_and_is_readable(self, client):
        import dashboard.app as app_module

        payload = {"tuya_devices": [
            {"name": "trwale", "id": "d3", "ip": "1.2.3.6", "local_key": "k3", "type": "socket"},
        ]}
        resp = client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["saved"] == 1
        devices = client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123")).get_json()
        assert any(d["name"] == "trwale" for d in devices["devices"])
        assert "trwale" in app_module.config_store.get("tuya_devices")["devices"]

    def test_post_devices_rejects_incomplete_device(self, client):
        payload = {"tuya_devices": [{"name": "x", "id": ""}]}
        resp = client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_post_devices_preserves_cloud_devices(self, client):
        """Odwrotny kierunek do
        TestTuyaCloudPluginPage.test_devices_post_preserves_local_devices_and_gateways -
        zapis stąd (urządzenia lokalne) nie może skasować urządzeń
        chmurowych zapisanych z zakładki Tuya Cloud (współdzielony
        dokument ConfigStore "tuya_devices")."""
        client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        payload = {"tuya_devices": [
            {"name": "trwale", "id": "d3", "ip": "1.2.3.6", "local_key": "k3", "type": "socket"},
        ]}
        resp = client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

        cloud_after = client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert any(d["name"] == "lampa_taras" for d in cloud_after["devices"])

    def test_scan_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", lambda: [{"id": "d1", "ip": "1.2.3.4"}])
        assert client.post("/api/plugins/tuya/scan").status_code == 401
        resp = client.post("/api/plugins/tuya/scan", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        # "known_name" - dopasowanie po ID do już skonfigurowanego urządzenia
        # (patrz CONFIG_TEMPLATE tego pliku: tuya.devices ma "gniazdko" -> id "d1").
        assert resp.get_json()["devices"] == [{"id": "d1", "ip": "1.2.3.4", "known_name": "gniazdko"}]

    def test_scan_known_name_empty_when_no_id_match(self, client, monkeypatch):
        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", lambda: [{"id": "nieznany", "ip": "1.2.3.9"}])
        resp = client.post("/api/plugins/tuya/scan", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["devices"] == [{"id": "nieznany", "ip": "1.2.3.9", "known_name": ""}]

    def test_cloud_import_missing_credentials_returns_400(self, client):
        resp = client.post(
            "/api/plugins/tuya/cloud-import", json={"api_region": "eu"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_cloud_import_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr(
            "plugins.tuya_local.routes.fetch_tuya_cloud_devices",
            lambda region, aid, akey: [{"id": "d9", "name": "Nowe", "local_key": "k9", "ip": "1.2.3.9"}],
        )
        payload = {"api_region": "eu", "access_id": "aid", "access_key": "akey"}
        assert client.post("/api/plugins/tuya/cloud-import", json=payload).status_code == 401
        resp = client.post(
            "/api/plugins/tuya/cloud-import", json=payload, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert resp.get_json()["devices"][0]["name"] == "Nowe"

    def test_settings_get_requires_auth(self, client):
        assert client.get("/api/plugins/tuya_local/settings").status_code == 401
        resp = client.get("/api/plugins/tuya_local/settings", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"enabled": True, "log_level": ""}

    def test_settings_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/tuya_local/settings", json={"log_level": "DEBUG"}).status_code == 401

    def test_settings_post_does_not_clobber_devices_saved_via_tuya_devices_doc(self, client):
        """Tak jak dla satel - po zapisie przez /api/plugins/tuya/devices,
        zapis /settings (enabled/log_level) nie może cofnąć zmiany
        urządzeń, bo bierze/zapisuje sekcję "tuya" wprost z dokumentu
        "config", nie ze scalonego widoku (gdzie devices nadpisane są z
        osobnego dokumentu tuya_devices)."""
        import dashboard.app as app_module

        client.post(
            "/api/plugins/tuya/devices",
            json={"tuya_devices": [{"name": "trwale", "id": "d3", "ip": "1.2.3.6", "local_key": "k3", "type": "socket"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/tuya_local/settings", json={"enabled": False, "log_level": "WARNING"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        stored_tuya = app_module.config_store.get("config")["tuya"]
        assert stored_tuya["enabled"] is False
        assert stored_tuya["log_level"] == "WARNING"
        devices = client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123")).get_json()
        assert any(d["name"] == "trwale" for d in devices["devices"])


class TestEbusPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/ebus", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/ebus", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_get_sensors_requires_auth(self, client):
        assert client.get("/api/plugins/ebus/sensors").status_code == 401
        resp = client.get("/api/plugins/ebus/sensors", headers=_basic_auth_header("test_haslo_123"))
        assert resp.get_json() == []

    def test_post_sensors_without_auth_returns_401(self, client):
        resp = client.post("/api/plugins/ebus/sensors", json=[])
        assert resp.status_code == 401

    def test_post_sensors_persists_and_is_readable(self, client):
        import dashboard.app as app_module

        payload = [{
            "name": "kociol_temp", "label": "Temp", "mqtt_topic": "ebusd/x/y",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False, "tuya_dp": ""},
        }]
        resp = client.post("/api/plugins/ebus/sensors", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["saved"] == 1
        sensors = client.get("/api/plugins/ebus/sensors", headers=_basic_auth_header("test_haslo_123")).get_json()
        names = [s["name"] for s in sensors]
        assert "kociol_temp" in names
        assert app_module.config_store.get("sensors")["sensors"][0]["name"] == "kociol_temp"

    def test_post_sensors_validates_required_fields(self, client):
        payload = [{"name": "", "mqtt_topic": "x", "targets": {}}]
        resp = client.post("/api/plugins/ebus/sensors", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_discover_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr(
            "plugins.ebus.routes.discover_ebus_topics", lambda host, port: ["ebusd/bai/FlowTemp"],
        )
        assert client.post("/api/plugins/ebus/discover", json={}).status_code == 401
        resp = client.post("/api/plugins/ebus/discover", json={}, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["topics"] == ["ebusd/bai/FlowTemp"]

    def test_discover_uses_configured_mqtt_host_by_default(self, client, monkeypatch):
        captured = {}

        def fake_discover(host, port):
            captured["host"] = host
            return []

        monkeypatch.setattr("plugins.ebus.routes.discover_ebus_topics", fake_discover)
        client.post("/api/plugins/ebus/discover", json={}, headers=_basic_auth_header("test_haslo_123"))
        assert captured["host"] == "127.0.0.1"

    def test_background_discovery_start_status_stop(self, client, monkeypatch):
        import plugins.ebus.routes as ebus_routes

        monkeypatch.setattr(ebus_routes._background_discovery, "start", lambda *a, **kw: None)
        monkeypatch.setattr(
            ebus_routes._background_discovery, "status",
            lambda: {"running": True, "topics": [], "elapsed_seconds": 0, "duration_seconds": 300},
        )
        resp = client.post("/api/plugins/ebus/discover/start", json={}, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["running"] is True

        resp = client.get("/api/plugins/ebus/discover/status", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

        monkeypatch.setattr(
            ebus_routes._background_discovery, "stop",
            lambda: {"running": False, "topics": [], "elapsed_seconds": 0, "duration_seconds": 300},
        )
        resp = client.post("/api/plugins/ebus/discover/stop", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["running"] is False

    def test_background_discovery_start_missing_host_returns_400(self, client, monkeypatch):
        import dashboard.app as app_module

        raw = app_module.config_store.get("config")
        raw["ebus"] = {}
        app_module.config_store.set("config", raw)
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = client.post(
            "/api/plugins/ebus/discover/start", json={}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_settings_get_requires_auth(self, client):
        assert client.get("/api/plugins/ebus/settings").status_code == 401
        resp = client.get("/api/plugins/ebus/settings", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"enabled": True, "log_level": ""}

    def test_settings_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/ebus/settings", json={"log_level": "DEBUG"}).status_code == 401

    def test_settings_post_does_not_duplicate_sensors_into_config_doc(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins/ebus/settings", json={"log_level": "ERROR"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        stored_ebus = app_module.config_store.get("config")["ebus"]
        assert stored_ebus["log_level"] == "ERROR"
        assert "sensors" not in stored_ebus
        assert stored_ebus["mqtt_host"] == "127.0.0.1"


class TestTuyaCloudPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/tuya_cloud", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/tuya_cloud", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_credentials_get_requires_auth_and_defaults(self, client):
        assert client.get("/api/plugins/tuya_cloud/credentials").status_code == 401
        resp = client.get("/api/plugins/tuya_cloud/credentials", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["access_id"] == ""
        assert data["access_key_set"] is False
        assert data["api_region"] == "eu"
        assert data["api_endpoint"] == "https://openapi.tuyaeu.com"

    def test_credentials_post_rejects_unknown_region(self, client):
        resp = client.post(
            "/api/plugins/tuya_cloud/credentials",
            json={"access_id": "aid1", "api_region": "atlantyda"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_credentials_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/tuya_cloud/credentials", json={"access_id": "x"}).status_code == 401

    def test_credentials_post_requires_access_id(self, client):
        resp = client.post(
            "/api/plugins/tuya_cloud/credentials", json={}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_credentials_post_persists_and_never_leaks_secret_back(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins/tuya_cloud/credentials",
            json={
                "access_id": "aid1", "access_key": "secret1", "api_region": "us",
                "api_endpoint": "", "mq_endpoint": "",
            },
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["tuya"]["cloud"]
        assert saved["access_id"] == "aid1"
        assert saved["access_key"] == "secret1"
        assert saved["api_region"] == "us"

        data = client.get(
            "/api/plugins/tuya_cloud/credentials", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["access_id"] == "aid1"
        assert data["access_key_set"] is True
        assert data["api_region"] == "us"
        text = client.get(
            "/api/plugins/tuya_cloud/credentials", headers=_basic_auth_header("test_haslo_123"),
        ).get_data(as_text=True)
        assert "secret1" not in text

    def test_credentials_post_blank_secret_keeps_existing(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/tuya_cloud/credentials", json={"access_id": "aid1", "access_key": "secret1"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/tuya_cloud/credentials", json={"access_id": "aid1", "access_key": ""},
            headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")["tuya"]["cloud"]
        assert saved["access_key"] == "secret1"  # nietknięte

    def test_settings_get_requires_auth_and_defaults(self, client):
        assert client.get("/api/plugins/tuya_cloud/settings").status_code == 401
        resp = client.get("/api/plugins/tuya_cloud/settings", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"enabled": True, "log_level": ""}

    def test_settings_post_does_not_clobber_credentials(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/tuya_cloud/credentials", json={"access_id": "aid1", "access_key": "secret1"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/tuya_cloud/settings", json={"log_level": "DEBUG"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["tuya"]["cloud"]
        assert saved["log_level"] == "DEBUG"
        assert saved["access_id"] == "aid1"  # nietknięte

    def test_devices_get_requires_auth_and_empty_by_default(self, client):
        assert client.get("/api/plugins/tuya_cloud/devices").status_code == 401
        resp = client.get("/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"devices": []}

    def test_devices_post_without_auth_returns_401(self, client):
        payload = {"cloud_devices": [{"name": "lampa", "id": "c1"}]}
        assert client.post("/api/plugins/tuya_cloud/devices", json=payload).status_code == 401

    def test_devices_post_rejects_missing_id(self, client):
        payload = {"cloud_devices": [{"name": "lampa", "id": ""}]}
        resp = client.post(
            "/api/plugins/tuya_cloud/devices", json=payload, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_devices_post_saves_and_roundtrips(self, client):
        payload = {"cloud_devices": [{"name": "lampa_taras", "id": "c1", "category": "kg", "switch_code": "switch_1"}]}
        resp = client.post(
            "/api/plugins/tuya_cloud/devices", json=payload, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert resp.get_json()["saved"] == 1

        data = client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["devices"] == [{"name": "lampa_taras", "id": "c1", "category": "kg", "switch_code": "switch_1"}]

    def test_devices_post_preserves_local_devices_and_gateways(self, client):
        """Ta strona edytuje TYLKO cloud_devices - zapis stąd nie może
        skasować lokalnych urządzeń/bramek zapisanych z zakładki Tuya
        Local (współdzielony dokument ConfigStore "tuya_devices").
        "gniazdko" (id "d1") jest już skonfigurowane przez CONFIG_TEMPLATE
        tego pliku (patrz tuya.devices w fixture `client`) - widoczne przez
        /api/plugins/tuya/devices, tak jak faktycznie widziałby to panel."""
        before = client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123")).get_json()
        assert any(d["name"] == "gniazdko" for d in before["devices"])

        payload = {"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]}
        resp = client.post(
            "/api/plugins/tuya_cloud/devices", json=payload, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200

        after = client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123")).get_json()
        assert any(d["name"] == "gniazdko" for d in after["devices"])
        cloud_after = client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert any(d["name"] == "lampa_taras" for d in cloud_after["devices"])

    def test_cloud_import_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/tuya_cloud/cloud-import").status_code == 401

    def test_cloud_import_requires_saved_credentials(self, client):
        resp = client.post(
            "/api/plugins/tuya_cloud/cloud-import", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        assert "poświadczenia" in resp.get_json()["error"].lower()

    def test_cloud_import_uses_stored_credentials(self, client, monkeypatch):
        captured = {}

        def _fake_fetch(api_region, access_id, access_key):
            captured.update(api_region=api_region, access_id=access_id, access_key=access_key)
            return [{"id": "c1", "name": "Lampa", "local_key": "", "ip": "", "category": "kg"}]

        monkeypatch.setattr("plugins.tuya_cloud.routes.fetch_tuya_cloud_devices", _fake_fetch)
        client.post(
            "/api/plugins/tuya_cloud/credentials",
            json={"access_id": "aid1", "access_key": "secret1", "api_region": "us"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/tuya_cloud/cloud-import", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert captured == {"api_region": "us", "access_id": "aid1", "access_key": "secret1"}
        assert resp.get_json()["devices"][0]["name"] == "Lampa"

    def test_cloud_import_connection_error_returns_502(self, client, monkeypatch):
        def _fake_fetch(api_region, access_id, access_key):
            raise ConnectionError("błąd logowania")

        monkeypatch.setattr("plugins.tuya_cloud.routes.fetch_tuya_cloud_devices", _fake_fetch)
        client.post(
            "/api/plugins/tuya_cloud/credentials",
            json={"access_id": "aid1", "access_key": "secret1"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/tuya_cloud/cloud-import", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 502


class TestHomeAssistantPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/home_assistant", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/home_assistant", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_broker_get_requires_auth_and_falls_back_to_ebus_host(self, client):
        assert client.get("/api/plugins/home_assistant/broker").status_code == 401
        resp = client.get("/api/plugins/home_assistant/broker", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mqtt_host"] == ""
        assert data["ebus_mqtt_host"] == "127.0.0.1"

    def test_broker_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/home_assistant/broker", json={}).status_code == 401

    def test_broker_post_falls_back_to_ebus_host_when_blank(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins/home_assistant/broker", json={"mqtt_host": ""},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["home_assistant"]
        assert saved["mqtt_host"] == "127.0.0.1"

    def test_broker_post_rejects_when_no_host_available(self, client):
        import dashboard.app as app_module

        raw = app_module.config_store.get("config")
        raw["ebus"] = {}
        app_module.config_store.set("config", raw)
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = client.post(
            "/api/plugins/home_assistant/broker", json={"mqtt_host": ""},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_settings_post_does_not_clobber_broker_fields(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/home_assistant/broker", json={"mqtt_host": "10.0.0.5", "device_name": "Custom"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/home_assistant/settings", json={"enabled": True, "log_level": "INFO"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")["home_assistant"]
        assert saved["mqtt_host"] == "10.0.0.5"
        assert saved["device_name"] == "Custom"
        assert saved["enabled"] is True
        assert saved["log_level"] == "INFO"


class TestMqttPublishPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/mqtt_publish", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/mqtt_publish", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_topics_get_requires_auth_and_defaults(self, client):
        assert client.get("/api/plugins/mqtt_publish/topics").status_code == 401
        resp = client.get("/api/plugins/mqtt_publish/topics", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"satel_topic": "satel", "tuya_topic": "tuya"}

    def test_topics_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/mqtt_publish/topics", json={}).status_code == 401

    def test_topics_post_persists_and_settings_do_not_clobber_them(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/mqtt_publish/topics", json={"satel_topic": "moj_satel", "tuya_topic": "moje_tuya"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/mqtt_publish/settings", json={"log_level": "WARNING"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")["mqtt_publish"]
        assert saved["topics"] == {"satel": "moj_satel", "tuya": "moje_tuya"}
        assert saved["log_level"] == "WARNING"


class TestUnifiProtectPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/unifi_protect", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/unifi_protect", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_credentials_get_requires_auth_and_defaults_when_unconfigured(self, client):
        assert client.get("/api/plugins/unifi_protect/credentials").status_code == 401
        resp = client.get("/api/plugins/unifi_protect/credentials", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"configured": False, "host": "", "username": "", "password_set": False, "verify_ssl": False}

    def test_credentials_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/unifi_protect/credentials", json={"host": "x"}).status_code == 401

    def test_credentials_post_requires_host(self, client):
        resp = client.post(
            "/api/plugins/unifi_protect/credentials", json={}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_credentials_post_persists_and_never_leaks_password_back(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins/unifi_protect/credentials",
            json={"host": "192.168.1.1", "username": "admin", "password": "s3cr3t", "verify_ssl": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["unifi_protect"]
        assert saved["host"] == "192.168.1.1"
        assert saved["password"] == "s3cr3t"
        text = client.get(
            "/api/plugins/unifi_protect/credentials", headers=_basic_auth_header("test_haslo_123"),
        ).get_data(as_text=True)
        assert "s3cr3t" not in text

    def test_settings_post_rejects_enabling_when_not_configured(self, client):
        resp = client.post(
            "/api/plugins/unifi_protect/settings", json={"enabled": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_settings_post_succeeds_once_configured(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/unifi_protect/credentials", json={"host": "192.168.1.1"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/unifi_protect/settings", json={"enabled": True, "log_level": "ERROR"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["unifi_protect"]
        assert saved["enabled"] is True
        assert saved["log_level"] == "ERROR"
        assert saved["host"] == "192.168.1.1"  # nietknięte


class TestHomeWhizPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/homewhiz", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/homewhiz", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_credentials_get_requires_auth_and_defaults_when_unconfigured(self, client):
        assert client.get("/api/plugins/homewhiz/credentials").status_code == 401
        resp = client.get("/api/plugins/homewhiz/credentials", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"username": "", "password_set": False, "language": "en"}

    def test_credentials_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/homewhiz/credentials", json={"username": "x"}).status_code == 401

    def test_credentials_post_requires_username(self, client):
        resp = client.post(
            "/api/plugins/homewhiz/credentials", json={}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_credentials_post_persists_and_never_leaks_password_back(self, client):
        import dashboard.app as app_module

        resp = client.post(
            "/api/plugins/homewhiz/credentials",
            json={"username": "user@example.com", "password": "s3cr3t", "language": "pl"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["username"] == "user@example.com"
        assert saved["password"] == "s3cr3t"
        assert saved["language"] == "pl"
        text = client.get(
            "/api/plugins/homewhiz/credentials", headers=_basic_auth_header("test_haslo_123"),
        ).get_data(as_text=True)
        assert "s3cr3t" not in text

    def test_credentials_post_blank_password_keeps_existing(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "user@example.com", "password": "s3cr3t"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "user@example.com", "password": ""},
            headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["password"] == "s3cr3t"

    def test_appliances_get_defaults_to_empty(self, client):
        resp = client.get("/api/plugins/homewhiz/appliances", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"appliances": []}

    def test_appliances_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/homewhiz/appliances", json={"appliances": []}).status_code == 401

    def test_appliances_post_requires_credentials_first(self, client):
        resp = client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_appliances_post_rejects_missing_appliance_id(self, client):
        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/homewhiz/appliances", json={"appliances": [{"name": "Zmywarka", "appliance_id": ""}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_appliances_post_rejects_duplicate_names(self, client):
        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [
                {"name": "Zmywarka", "appliance_id": "a1"}, {"name": "Zmywarka", "appliance_id": "a2"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_appliances_post_persists(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["appliances"] == {"Zmywarka": {"appliance_id": "abc123"}}

    def test_settings_post_rejects_enabling_when_not_configured(self, client):
        resp = client.post(
            "/api/plugins/homewhiz/settings", json={"enabled": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_settings_post_succeeds_once_configured(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/plugins/homewhiz/settings", json={"enabled": True, "log_level": "ERROR"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["enabled"] is True
        assert saved["log_level"] == "ERROR"
        assert saved["username"] == "u"  # nietknięte

    def test_discover_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/homewhiz/discover").status_code == 401

    def test_discover_requires_credentials_first(self, client):
        resp = client.post("/api/plugins/homewhiz/discover", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_discover_returns_appliances_from_client(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizSession

        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )

        async def fake_login(username, password):
            return HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")

        async def fake_list_appliances(session):
            return [
                {"applianceId": "a1", "name": "Zmywarka", "brand": "Beko", "model": "X1", "applianceType": 3},
                {"applianceId": "a2", "name": "Pralka", "brand": "Beko", "model": "Y1", "applianceType": 1},
            ]

        monkeypatch.setattr(homewhiz_routes_module, "login", fake_login)
        monkeypatch.setattr(homewhiz_routes_module, "list_appliances", fake_list_appliances)

        resp = client.post("/api/plugins/homewhiz/discover", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        appliances = resp.get_json()["appliances"]
        assert {a["appliance_id"] for a in appliances} == {"a1", "a2"}
        assert next(a for a in appliances if a["appliance_id"] == "a1")["is_dishwasher"] is True
        assert next(a for a in appliances if a["appliance_id"] == "a2")["is_dishwasher"] is False

    def test_discover_reports_homewhiz_error(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizError

        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )

        async def fake_login(username, password):
            raise HomeWhizError("zły login lub hasło")

        monkeypatch.setattr(homewhiz_routes_module, "login", fake_login)

        resp = client.post("/api/plugins/homewhiz/discover", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 502
        assert "login" in resp.get_json()["error"]

    def test_appliance_states_without_auth_returns_401(self, client):
        assert client.get("/api/plugins/homewhiz/appliances/Zmywarka/states").status_code == 401

    def test_appliance_states_requires_credentials_first(self, client):
        resp = client.get(
            "/api/plugins/homewhiz/appliances/Zmywarka/states", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_appliance_states_unknown_appliance_returns_404(self, client):
        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.get(
            "/api/plugins/homewhiz/appliances/Nieznana/states", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 404

    def test_appliance_states_returns_list_from_config_descriptor(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizSession

        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        async def fake_login(username, password):
            return HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")

        async def fake_fetch_config_descriptor(session, appliance_id, language="en"):
            assert appliance_id == "abc123"
            return {"deviceStates": {"states": [
                {"strKey": "Dishwasher_State_Idle", "wifiArrayValue": 0},
                {"strKey": "Dishwasher_State_Finished", "wifiArrayValue": 1},
            ]}}

        monkeypatch.setattr(homewhiz_routes_module, "login", fake_login)
        monkeypatch.setattr(homewhiz_routes_module, "fetch_config_descriptor", fake_fetch_config_descriptor)

        resp = client.get(
            "/api/plugins/homewhiz/appliances/Zmywarka/states", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert resp.get_json()["states"] == ["idle", "finished"]

    def test_appliance_states_reports_homewhiz_error(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizError

        client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        async def fake_login(username, password):
            raise HomeWhizError("zły login lub hasło")

        monkeypatch.setattr(homewhiz_routes_module, "login", fake_login)

        resp = client.get(
            "/api/plugins/homewhiz/appliances/Zmywarka/states", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 502


class TestNotificationsPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/notifications", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/notifications", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_topic_get_requires_auth_and_defaults(self, client):
        assert client.get("/api/plugins/notifications/topic").status_code == 401
        resp = client.get("/api/plugins/notifications/topic", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"ntfy_topic_url": "", "grace_seconds": 120}

    def test_topic_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/notifications/topic", json={}).status_code == 401

    def test_topic_post_rejects_negative_grace_seconds(self, client):
        resp = client.post(
            "/api/plugins/notifications/topic", json={"grace_seconds": -5},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_topic_post_persists_and_settings_do_not_clobber_it(self, client):
        import dashboard.app as app_module

        client.post(
            "/api/plugins/notifications/topic",
            json={"ntfy_topic_url": "https://ntfy.sh/xyz", "grace_seconds": 60},
            headers=_basic_auth_header("test_haslo_123"),
        )
        client.post(
            "/api/plugins/notifications/settings", json={"log_level": "CRITICAL"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")["notifications"]
        assert saved["ntfy_topic_url"] == "https://ntfy.sh/xyz"
        assert saved["grace_seconds"] == 60
        assert saved["log_level"] == "CRITICAL"


class TestPresenceSimulationPluginPage:
    def test_page_redirects_to_login_without_auth(self, client):
        resp = client.get("/plugins/presence_simulation", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_page_loads_when_authenticated(self, client):
        resp = client.get("/plugins/presence_simulation", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

    def test_summary_get_requires_auth_and_defaults_empty(self, client):
        assert client.get("/api/plugins/presence_simulation/summary").status_code == 401
        resp = client.get("/api/plugins/presence_simulation/summary", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"devices": []}

    def test_settings_get_requires_auth_and_defaults(self, client):
        assert client.get("/api/plugins/presence_simulation/settings").status_code == 401
        resp = client.get("/api/plugins/presence_simulation/settings", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json() == {"enabled": True, "log_level": ""}

    def test_settings_post_without_auth_returns_401(self, client):
        assert client.post("/api/plugins/presence_simulation/settings", json={"log_level": "DEBUG"}).status_code == 401

    def test_settings_post_persists_to_automations_doc_without_clobbering_scenes(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [{"name": "powitanie", "actions": [{"type": "satel_disarm"}]}],
            "reguły_ruchu": [],
            "presence_simulation": {"enabled": False, "devices": ["gniazdko"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = client.post(
            "/api/plugins/presence_simulation/settings", json={"enabled": True, "log_level": "DEBUG"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("automations")
        assert saved["presence_simulation"]["enabled"] is True
        assert saved["presence_simulation"]["log_level"] == "DEBUG"
        assert saved["presence_simulation"]["devices"] == ["gniazdko"]  # nietknięte
        assert saved["scenes"][0]["name"] == "powitanie"  # nietknięte

    def test_summary_reflects_devices_from_automations_doc(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [], "reguły_ruchu": [],
            "presence_simulation": {"enabled": True, "devices": ["gniazdko", "lampka"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        data = client.get(
            "/api/plugins/presence_simulation/summary", headers=_basic_auth_header("test_haslo_123"),
        ).get_json()
        assert data["devices"] == ["gniazdko", "lampka"]

    def test_automations_page_save_does_not_clobber_log_level(self, client):
        """Zakładka /automations nie zna pola log_level (żyje tylko na
        /plugins/presence_simulation) - zapis stamtąd (nawet samych scen)
        nie może po cichu skasować wcześniej ustawionego poziomu logowania
        (patrz komentarz w dashboard/app.py: api_config_automations_post)."""
        import dashboard.app as app_module

        client.post(
            "/api/plugins/presence_simulation/settings", json={"log_level": "WARNING"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = client.post(
            "/api/config/automations",
            json={
                "scenes": [],
                "motion_rules": [],
                "presence_simulation": {"enabled": False, "devices": []},
            },
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = app_module.config_store.get("automations")["presence_simulation"]
        assert saved["log_level"] == "WARNING"
