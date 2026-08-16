"""Testy dla dashboard/app.py - endpointy API, w tym egzekwowanie autoryzacji."""
import base64

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
  virtual_inputs:
    port: 8888
    url_template: "http://{{host}}:{{port}}/x"
    inputs:
      motion_hall:
        number: 1
        key: "k"
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


def _basic_auth_header(password: str) -> dict:
    token = base64.b64encode(f":{password}".encode()).decode()
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

    import dashboard.app as app_module
    app_module.config_path = tmp_path / "config.yaml"  # inaczej before_request przekieruje wszystko na /setup

    config = BridgeConfig.load(tmp_path / "config.yaml")
    create_app(config)
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


class TestPublicEndpoints:
    def test_index_page_loads(self, client):
        assert client.get("/").status_code == 200

    def test_config_page_loads(self, client):
        assert client.get("/config").status_code == 200

    def test_get_sensors_no_auth_required(self, client):
        assert client.get("/api/config/sensors").status_code == 200

    def test_status_endpoint(self, client):
        assert client.get("/api/status").status_code == 200

    def test_healthz_ok_when_no_components_reported(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_satel_partitions_public(self, client):
        resp = client.get("/api/satel/partitions")
        assert resp.status_code == 200
        assert resp.get_json() == {"Dom": 1, "Garaz": 2}

    def test_command_status_public(self, client):
        resp = client.get("/api/commands/999")
        assert resp.status_code == 404  # nie istnieje, ale endpoint jest dostępny bez auth


class TestProtectedConfigEndpoint:
    def test_post_sensors_without_auth_returns_401(self, client):
        resp = client.post("/api/config/sensors", json=[])
        assert resp.status_code == 401

    def test_post_sensors_with_correct_auth_succeeds(self, client):
        payload = [{
            "name": "test_sensor", "label": "Test", "mqtt_topic": "ebusd/x/y",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False, "tuya_dp": ""},
        }]
        resp = client.post("/api/config/sensors", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["saved"] == 1

    def test_post_sensors_with_wrong_auth_returns_401(self, client):
        resp = client.post("/api/config/sensors", json=[], headers=_basic_auth_header("złe_hasło"))
        assert resp.status_code == 401

    def test_post_sensors_validates_required_fields(self, client):
        payload = [{"name": "", "mqtt_topic": "x", "targets": {}}]
        resp = client.post("/api/config/sensors", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_post_sensors_rejects_duplicate_names(self, client):
        payload = [
            {"name": "a", "mqtt_topic": "x", "targets": {}},
            {"name": "a", "mqtt_topic": "y", "targets": {}},
        ]
        resp = client.post("/api/config/sensors", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_saved_sensors_persist_and_are_readable(self, client):
        payload = [{
            "name": "trwaly_czujnik", "label": "Trwały", "mqtt_topic": "ebusd/a/b",
            "targets": {"history": True, "mqtt_publish": True, "tuya_virtual_device": False, "tuya_dp": ""},
        }]
        client.post("/api/config/sensors", json=payload, headers=_basic_auth_header("test_haslo_123"))
        resp = client.get("/api/config/sensors")
        names = [s["name"] for s in resp.get_json()]
        assert "trwaly_czujnik" in names


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
        status_resp = client.get(f"/api/commands/{command_id}")
        assert status_resp.status_code == 200
        assert status_resp.get_json()["status"] == "pending"
        assert status_resp.get_json()["command"] == "satel_arm"

    def test_arm_defaults_to_all_configured_partitions(self, client):
        resp = client.post("/api/satel/arm", json={}, headers=_basic_auth_header("test_haslo_123"))
        command_id = resp.get_json()["command_id"]
        status_resp = client.get(f"/api/commands/{command_id}")
        assert set(status_resp.get_json()["params"]["partitions"]) == {1, 2}


class TestAutomationContextEndpoint:
    def test_context_public_no_auth_required(self, client):
        assert client.get("/api/config/automation-context").status_code == 200

    def test_context_returns_configured_device_and_virtual_input_names(self, client):
        data = client.get("/api/config/automation-context").get_json()
        assert data["tuya_devices"] == ["gniazdko"]
        assert data["virtual_inputs"] == ["motion_hall"]


class TestAutomationsEndpoint:
    def test_automations_page_loads(self, client):
        assert client.get("/automations").status_code == 200

    def test_get_automations_no_auth_required(self, client):
        resp = client.get("/api/config/automations")
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
        resp = client.get("/api/config/automations")
        data = resp.get_json()
        assert data["scenes"][0]["name"] == "scena1"
        assert data["motion_rules"][0]["scene"] == "scena1"
        assert data["presence_simulation"]["enabled"] is True
        assert data["presence_simulation"]["devices"] == ["gniazdko"]

    def test_post_automations_writes_automations_yaml_file(self, client, tmp_path):
        payload = {
            "scenes": [{"name": "s", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [], "presence_simulation": {},
        }
        client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert (tmp_path / "automations.yaml").exists()

    def test_post_automations_does_not_touch_config_yaml(self, client, tmp_path):
        """Zapis z panelu MUSI iść do automations.yaml, nigdy do config.yaml -
        inaczej ręczne komentarze/zmiany w config.yaml zostałyby skasowane
        (patrz uzasadnienie dla sensors.yaml w bridge/config.py)."""
        before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        payload = {
            "scenes": [{"name": "s", "actions": [{"type": "satel_disarm"}]}],
            "motion_rules": [], "presence_simulation": {},
        }
        client.post("/api/config/automations", json=payload, headers=_basic_auth_header("test_haslo_123"))
        after = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert before == after
