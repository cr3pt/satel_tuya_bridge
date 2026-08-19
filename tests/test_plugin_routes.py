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
    def test_page_loads(self, client):
        assert client.get("/plugins/satel").status_code == 200

    def test_get_fields_no_auth_required(self, client):
        resp = client.get("/api/plugins/satel/fields")
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
        assert {"name": "Piwnica", "number": 9} in client.get("/api/plugins/satel/fields").get_json()["partitions"]
        assert app_module.config_store.get("satel_fields")["partitions"] == {"Piwnica": 9}

    def test_post_fields_enqueues_bridge_reload(self, client):
        import dashboard.app as app_module

        client.post("/api/plugins/satel/fields", json={}, headers=_basic_auth_header("test_haslo_123"))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

    def test_scan_no_auth_required_and_handles_connection_error(self, client):
        resp = client.post("/api/plugins/satel/scan", json={"host": "127.0.0.1", "timeout_seconds": 0.1})
        assert resp.status_code == 502


class TestTuyaLocalPluginPage:
    def test_page_loads(self, client):
        assert client.get("/plugins/tuya").status_code == 200

    def test_get_devices_no_auth_required(self, client):
        data = client.get("/api/plugins/tuya/devices").get_json()
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
        assert any(d["name"] == "trwale" for d in client.get("/api/plugins/tuya/devices").get_json()["devices"])
        assert "trwale" in app_module.config_store.get("tuya_devices")["devices"]

    def test_post_devices_rejects_incomplete_device(self, client):
        payload = {"tuya_devices": [{"name": "x", "id": ""}]}
        resp = client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_scan_no_auth_required(self, client, monkeypatch):
        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", lambda: [{"id": "d1", "ip": "1.2.3.4"}])
        resp = client.post("/api/plugins/tuya/scan")
        assert resp.status_code == 200
        assert resp.get_json()["devices"] == [{"id": "d1", "ip": "1.2.3.4"}]

    def test_cloud_import_missing_credentials_returns_400(self, client):
        resp = client.post("/api/plugins/tuya/cloud-import", json={"api_region": "eu"})
        assert resp.status_code == 400

    def test_cloud_import_no_auth_required(self, client, monkeypatch):
        monkeypatch.setattr(
            "plugins.tuya_local.routes.fetch_tuya_cloud_devices",
            lambda region, aid, akey: [{"id": "d9", "name": "Nowe", "local_key": "k9", "ip": "1.2.3.9"}],
        )
        resp = client.post(
            "/api/plugins/tuya/cloud-import",
            json={"api_region": "eu", "access_id": "aid", "access_key": "akey"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["devices"][0]["name"] == "Nowe"


class TestEbusPluginPage:
    def test_page_loads(self, client):
        assert client.get("/plugins/ebus").status_code == 200

    def test_get_sensors_no_auth_required(self, client):
        assert client.get("/api/plugins/ebus/sensors").get_json() == []

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
        names = [s["name"] for s in client.get("/api/plugins/ebus/sensors").get_json()]
        assert "kociol_temp" in names
        assert app_module.config_store.get("sensors")["sensors"][0]["name"] == "kociol_temp"

    def test_post_sensors_validates_required_fields(self, client):
        payload = [{"name": "", "mqtt_topic": "x", "targets": {}}]
        resp = client.post("/api/plugins/ebus/sensors", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_discover_no_auth_required(self, client, monkeypatch):
        monkeypatch.setattr(
            "plugins.ebus.routes.discover_ebus_topics", lambda host, port: ["ebusd/bai/FlowTemp"],
        )
        resp = client.post("/api/plugins/ebus/discover", json={})
        assert resp.status_code == 200
        assert resp.get_json()["topics"] == ["ebusd/bai/FlowTemp"]

    def test_discover_uses_configured_mqtt_host_by_default(self, client, monkeypatch):
        captured = {}

        def fake_discover(host, port):
            captured["host"] = host
            return []

        monkeypatch.setattr("plugins.ebus.routes.discover_ebus_topics", fake_discover)
        client.post("/api/plugins/ebus/discover", json={})
        assert captured["host"] == "127.0.0.1"

    def test_background_discovery_start_status_stop(self, client, monkeypatch):
        import plugins.ebus.routes as ebus_routes

        monkeypatch.setattr(ebus_routes._background_discovery, "start", lambda *a, **kw: None)
        monkeypatch.setattr(
            ebus_routes._background_discovery, "status",
            lambda: {"running": True, "topics": [], "elapsed_seconds": 0, "duration_seconds": 300},
        )
        resp = client.post("/api/plugins/ebus/discover/start", json={})
        assert resp.status_code == 200
        assert resp.get_json()["running"] is True

        resp = client.get("/api/plugins/ebus/discover/status")
        assert resp.status_code == 200

        monkeypatch.setattr(
            ebus_routes._background_discovery, "stop",
            lambda: {"running": False, "topics": [], "elapsed_seconds": 0, "duration_seconds": 300},
        )
        resp = client.post("/api/plugins/ebus/discover/stop")
        assert resp.status_code == 200
        assert resp.get_json()["running"] is False

    def test_background_discovery_start_missing_host_returns_400(self, client, monkeypatch):
        import dashboard.app as app_module

        raw = app_module.config_store.get("config")
        raw["ebus"] = {}
        app_module.config_store.set("config", raw)
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = client.post("/api/plugins/ebus/discover/start", json={})
        assert resp.status_code == 400
