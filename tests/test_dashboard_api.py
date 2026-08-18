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
  monitored_zones: {{Salon: 12}}
  monitored_outputs: {{Syrena: 1}}
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

    def test_plugins_page_loads(self, client):
        assert client.get("/plugins").status_code == 200

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


class TestSessionAuthEndpoints:
    """/login, /api/login, /api/logout, /api/auth/status - logowanie
    sesyjne, które zastąpiło prompt()-owy Basic Auth w panelu webowym."""

    def test_login_page_loads(self, client):
        assert client.get("/login").status_code == 200

    def test_auth_status_when_not_logged_in(self, client):
        resp = client.get("/api/auth/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"logged_in": False, "configured": True, "username": "admin"}

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
        resp = client.post("/api/config/sensors", json=[])
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
        resp = client.post("/api/config/sensors", json=[], headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200


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

    def test_post_sensors_enqueues_bridge_reload_command(self, client):
        """Bridge (osobny proces) musi dowiedzieć się o zmianie BEZ ręcznego
        restartu - panel dokłada komendę 'reload_config' do tej samej
        kolejki co uzbrojenie/rozbrojenie (patrz dashboard/app.py:
        _enqueue_bridge_reload, bridge/rules_engine.py: reload_config)."""
        import dashboard.app as app_module

        client.post("/api/config/sensors", json=[], headers=_basic_auth_header("test_haslo_123"))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

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
        status_resp = client.get(f"/api/commands/{command_id}")
        assert status_resp.get_json()["command"] == "restart_process"

    def test_schedules_delayed_self_exit_without_exiting_immediately(self, client, monkeypatch):
        exit_calls, started_timers = self._patch_exit(monkeypatch)
        client.post("/api/system/restart", headers=_basic_auth_header("test_haslo_123"))
        assert len(started_timers) == 1
        assert started_timers[0].interval == 0.5
        assert exit_calls == []  # timer.start() spatchowany - fn() nie wywołane, proces żyje


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


class TestSatelFieldsEndpoint:
    def test_get_satel_fields_no_auth_required(self, client):
        assert client.get("/api/config/satel-fields").status_code == 200

    def test_get_satel_fields_reads_inline_values_when_no_fields_file(self, client):
        """CONFIG_TEMPLATE ma strefy/wyjścia/wejścia wpisane wprost - bez
        satel_fields.yaml na dysku - i to musi działać (wsteczna zgodność,
        patrz bridge/config.py)."""
        data = client.get("/api/config/satel-fields").get_json()
        assert {"name": "Dom", "number": 1} in data["partitions"]
        assert {"name": "Salon", "number": 12, "type": ""} in data["monitored_zones"]
        assert {"name": "Syrena", "number": 1, "type": ""} in data["monitored_outputs"]
        assert {"name": "motion_hall", "number": 1, "key": "k"} in data["virtual_inputs"]
        assert data["zone_types"] == {}
        assert data["output_types"] == {}

    def test_post_satel_fields_without_auth_returns_401(self, client):
        resp = client.post("/api/config/satel-fields", json={})
        assert resp.status_code == 401

    def test_post_satel_fields_with_correct_auth_saves(self, client):
        payload = {
            "partitions": [{"name": "Dom", "number": "1"}],
            "monitored_zones": [{"name": "Kuchnia", "number": "20"}],
            "monitored_outputs": [],
            "virtual_inputs": [{"name": "vi1", "number": "3", "key": "K"}],
        }
        resp = client.post("/api/config/satel-fields", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["saved"]["monitored_zones"] == 1

    def test_post_satel_fields_enqueues_bridge_reload_command(self, client):
        import dashboard.app as app_module

        client.post("/api/config/satel-fields", json={}, headers=_basic_auth_header("test_haslo_123"))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

    def test_post_satel_fields_validates_incomplete_virtual_input(self, client):
        payload = {"virtual_inputs": [{"name": "x", "number": "", "key": ""}]}
        resp = client.post("/api/config/satel-fields", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_saved_satel_fields_persist_and_are_readable(self, client, tmp_path):
        payload = {
            "partitions": [{"name": "Piwnica", "number": "9"}],
            "monitored_zones": [], "monitored_outputs": [], "virtual_inputs": [],
        }
        client.post("/api/config/satel-fields", json=payload, headers=_basic_auth_header("test_haslo_123"))
        resp = client.get("/api/config/satel-fields")
        assert {"name": "Piwnica", "number": 9} in resp.get_json()["partitions"]

    def test_saved_zone_and_output_types_round_trip(self, client):
        """Rodzaj czujki/wyjścia (patrz SATEL_ZONE_TYPES/SATEL_OUTPUT_TYPES w
        setup.py) - kosmetyczny atrybut do ikon w panelu głównym - musi
        przetrwać zapis/odczyt tak samo jak nazwa/numer."""
        payload = {
            "monitored_zones": [{"name": "Kuchnia", "number": "20", "type": "dym"}],
            "monitored_outputs": [{"name": "Syrena zew.", "number": "2", "type": "syrena"}],
        }
        client.post("/api/config/satel-fields", json=payload, headers=_basic_auth_header("test_haslo_123"))
        data = client.get("/api/config/satel-fields").get_json()
        assert {"name": "Kuchnia", "number": 20, "type": "dym"} in data["monitored_zones"]
        assert {"name": "Syrena zew.", "number": 2, "type": "syrena"} in data["monitored_outputs"]
        assert data["zone_types"] == {"Kuchnia": "dym"}
        assert data["output_types"] == {"Syrena zew.": "syrena"}

    def test_post_satel_fields_does_not_touch_config_yaml(self, client, tmp_path):
        before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        client.post(
            "/api/config/satel-fields",
            json={"partitions": [{"name": "X", "number": "1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        after = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert before == after


class TestTuyaDevicesEndpoint:
    def test_get_tuya_devices_no_auth_required(self, client):
        assert client.get("/api/config/tuya-devices").status_code == 200

    def test_get_tuya_devices_reads_inline_values_when_no_devices_file(self, client):
        data = client.get("/api/config/tuya-devices").get_json()
        assert any(d["name"] == "gniazdko" and d["ip"] == "1.2.3.4" for d in data["devices"])

    def test_post_tuya_devices_without_auth_returns_401(self, client):
        resp = client.post("/api/config/tuya-devices", json={"tuya_devices": []})
        assert resp.status_code == 401

    def test_post_tuya_devices_with_correct_auth_saves(self, client):
        payload = {"tuya_devices": [
            {"name": "nowe", "id": "d2", "ip": "1.2.3.5", "local_key": "k2", "type": "socket"},
        ]}
        resp = client.post("/api/config/tuya-devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert resp.get_json()["saved"] == 1

    def test_post_tuya_devices_enqueues_bridge_reload_command(self, client):
        import dashboard.app as app_module

        client.post("/api/config/tuya-devices", json={"tuya_devices": []}, headers=_basic_auth_header("test_haslo_123"))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

    def test_post_tuya_devices_validates_incomplete_device(self, client):
        payload = {"tuya_devices": [{"name": "x", "id": "", "ip": "1.2.3.4", "local_key": "k"}]}
        resp = client.post("/api/config/tuya-devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_saved_tuya_devices_persist_and_are_readable(self, client, tmp_path):
        payload = {"tuya_devices": [
            {"name": "trwale", "id": "d3", "ip": "1.2.3.6", "local_key": "k3", "type": "socket"},
        ]}
        client.post("/api/config/tuya-devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        resp = client.get("/api/config/tuya-devices")
        assert any(d["name"] == "trwale" for d in resp.get_json()["devices"])
        assert (tmp_path / "tuya_devices.yaml").exists()

    def test_saved_zigbee_gateway_and_sub_device_round_trip(self, client):
        """Bramka Zigbee + podurządzenie wskazujące na nią (pole "gateway")
        - patrz TuyaLocalController w bridge/tuya_local.py. Podurządzenie
        NIE ma własnego ip/local_key, dostaje za to gateway/cid."""
        payload = {
            "tuya_devices": [
                {"name": "gniazdko", "id": "d1", "ip": "1.2.3.4", "local_key": "k", "type": "socket"},
                {"name": "lampka_zigbee", "id": "sub1", "gateway": "brama_salon", "cid": "node1", "type": "socket"},
            ],
            "zigbee_gateways": [
                {"name": "brama_salon", "id": "gw1", "ip": "1.2.3.9", "local_key": "gwkey", "version": "3.4"},
            ],
        }
        resp = client.post("/api/config/tuya-devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200

        data = client.get("/api/config/tuya-devices").get_json()
        assert {
            "name": "lampka_zigbee", "id": "sub1", "gateway": "brama_salon", "cid": "node1",
            "type": "socket", "ip": "", "local_key": "", "dimmer_dp": "",
        } in data["devices"]
        assert {"name": "brama_salon", "id": "gw1", "ip": "1.2.3.9", "local_key": "gwkey", "version": 3.4} \
            in data["zigbee_gateways"]

    def test_post_tuya_devices_rejects_sub_device_with_unknown_gateway(self, client):
        payload = {"tuya_devices": [{"name": "lampka", "id": "sub1", "gateway": "brama_nieznana"}]}
        resp = client.post("/api/config/tuya-devices", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_post_tuya_devices_does_not_touch_config_yaml(self, client, tmp_path):
        before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        client.post(
            "/api/config/tuya-devices",
            json={"tuya_devices": [{"name": "x", "id": "i", "ip": "1.2.3.4", "local_key": "k"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        after = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert before == after


class TestConnectionsEndpoint:
    """Zakładka "Połączenia" - jedyne miejsce do zmiany host/port/kluczy/
    tematów MQTT PO pierwszej konfiguracji (patrz dashboard/setup.py:
    build_connections_patch/connections_to_form). W ODRÓŻNIENIU od reszty
    /api/config/* - odczyt TEŻ wymaga hasła (zwraca informacje o tym, czy
    sekrety Satela/Tuya Cloud są ustawione)."""

    def test_get_without_auth_returns_401(self, client):
        assert client.get("/api/config/connections").status_code == 401

    def test_get_with_auth_returns_current_values(self, client):
        resp = client.get("/api/config/connections", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["satel"]["host"] == "127.0.0.1"
        assert data["satel"]["integration_key_set"] is True
        assert data["ebus"]["mqtt_host"] == "127.0.0.1"

    def test_get_never_leaks_integration_key(self, client):
        resp = client.get("/api/config/connections", headers=_basic_auth_header("test_haslo_123"))
        assert '"x"' not in resp.get_data(as_text=True)  # "x" to integration_key w CONFIG_TEMPLATE

    def test_post_without_auth_returns_401(self, client):
        resp = client.post("/api/config/connections", json={})
        assert resp.status_code == 401

    def test_post_updates_satel_host(self, client, tmp_path):
        payload = {"satel": {"host": "10.0.0.9", "port": "7094"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        resp = client.post("/api/config/connections", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["satel"]["host"] == "10.0.0.9"

    def test_post_blank_integration_key_preserves_existing_secret(self, client, tmp_path):
        payload = {
            "satel": {"host": "127.0.0.1", "port": "7094", "integration_key": ""},
            "ebus": {"mqtt_host": "127.0.0.1"},
        }
        client.post("/api/config/connections", json=payload, headers=_basic_auth_header("test_haslo_123"))
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["satel"]["integration_key"] == "x"  # niezmieniony, patrz CONFIG_TEMPLATE

    def test_post_validates_missing_satel_host(self, client):
        payload = {"satel": {"host": ""}, "ebus": {"mqtt_host": "127.0.0.1"}}
        resp = client.post("/api/config/connections", json=payload, headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 400

    def test_post_preserves_other_config_sections(self, client, tmp_path):
        """reguły_ruchu i inne sekcje spoza formularza połączeń nie mogą
        zniknąć po zapisie (patrz build_connections_patch)."""
        payload = {"satel": {"host": "10.0.0.9"}, "ebus": {"mqtt_host": "127.0.0.1"}}
        client.post("/api/config/connections", json=payload, headers=_basic_auth_header("test_haslo_123"))
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"
        assert saved["dashboard"]["auth"]["password_hash"]

    def test_post_sets_custom_mqtt_publish_topics(self, client, tmp_path):
        payload = {
            "satel": {"host": "127.0.0.1"}, "ebus": {"mqtt_host": "127.0.0.1"},
            "mqtt_publish": {"enabled": True, "satel_topic": "alarm", "tuya_topic": "smart_home"},
        }
        client.post("/api/config/connections", json=payload, headers=_basic_auth_header("test_haslo_123"))
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["mqtt_publish"]["topics"] == {"satel": "alarm", "tuya": "smart_home"}


class TestPluginsEndpoint:
    """Zakładka 🧩 Wtyczki - spójny przegląd wszystkich integracji
    (patrz dashboard/setup.py: list_plugins) i przełącznik TYLKO dla pól
    "enabled" (tuya_cloud/home_assistant/mqtt_publish/presence_simulation) -
    reszta integracji (Satel, Tuya lokalnie/Zigbee, eBUS, UniFi Protect,
    powiadomienia) jest włączona/wyłączona przez samą obecność konfiguracji
    i wciąż wymaga przejścia do właściwej zakładki."""

    def test_get_no_auth_required(self, client):
        assert client.get("/api/plugins").status_code == 200

    def test_get_returns_satel_and_tuya_local_enabled_by_default(self, client):
        by_id = {p["id"]: p for p in client.get("/api/plugins").get_json()}
        assert by_id["satel"]["enabled"] is True
        assert by_id["tuya_local"]["enabled"] is True
        assert by_id["home_assistant"]["enabled"] is False
        assert by_id["presence_simulation"]["enabled"] is False

    def test_get_never_leaks_secrets(self, client):
        """W odróżnieniu od /api/config/connections - ten endpoint jest
        publiczny (bez hasła), więc absolutnie nie może zwrócić
        integration_key/local_key/access_key."""
        text = client.get("/api/plugins").get_data(as_text=True)
        assert '"x"' not in text  # integration_key z CONFIG_TEMPLATE
        assert '"k"' not in text  # local_key gniazdka z CONFIG_TEMPLATE

    def test_post_without_auth_returns_401(self, client):
        resp = client.post("/api/plugins", json={"home_assistant": {"enabled": True}})
        assert resp.status_code == 401

    def test_post_unknown_plugin_id_rejected(self, client):
        resp = client.post(
            "/api/plugins", json={"unifi_protect": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_post_satel_enabled_field_is_ignored_not_toggleable(self, client, tmp_path):
        """satel jest rdzeniem (zawsze aktywny) - ma tylko regulowalny
        interwał odpytywania, "enabled" nie jest dla niego obsługiwane i
        zostaje po cichu zignorowane (nie jest to błąd - satel to
        prawidłowe id, tylko bez pola enabled)."""
        resp = client.post(
            "/api/plugins", json={"satel": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["satel"]["host"] == "127.0.0.1"  # nietknięte, wciąż działa

    def test_post_updates_satel_poll_interval_live(self, client, tmp_path):
        resp = client.post(
            "/api/plugins", json={"satel": {"status_poll_interval_seconds": 25}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert "bez restartu" in resp.get_json()["message"]
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["satel"]["status_poll_interval_seconds"] == 25
        assert saved["satel"]["host"] == "127.0.0.1"  # nietknięte

    def test_post_updates_tuya_local_poll_interval_without_restart_message(self, client, tmp_path):
        resp = client.post(
            "/api/plugins", json={"tuya_local": {"status_poll_interval_seconds": 45}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        message = resp.get_json()["message"]
        assert "bez restartu" in message
        assert "wymagają pełnego restartu" not in message
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["tuya"]["status_poll_interval_seconds"] == 45
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"  # nietknięte

    def test_post_rejects_poll_interval_below_minimum(self, client):
        resp = client.post(
            "/api/plugins", json={"satel": {"status_poll_interval_seconds": 1}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_post_enables_home_assistant_without_touching_satel_host(self, client, tmp_path):
        resp = client.post(
            "/api/plugins", json={"home_assistant": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["home_assistant"]["enabled"] is True
        assert saved["satel"]["host"] == "127.0.0.1"  # nietknięte
        assert saved["tuya"]["devices"]["gniazdko"]["ip"] == "1.2.3.4"  # nietknięte

    def test_post_disables_mqtt_publish(self, client, tmp_path):
        client.post(
            "/api/plugins", json={"mqtt_publish": {"enabled": False}}, headers=_basic_auth_header("test_haslo_123"),
        )
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["mqtt_publish"]["enabled"] is False

    def test_post_enabling_tuya_cloud_without_credentials_is_rejected(self, client):
        """Włączenie stąd samym przełącznikiem nie ma sensu bez wcześniej
        wpisanych poświadczeń (Access ID) w zakładce 🔌 Połączenia -
        walidacja musi to odrzucić z czytelnym błędem, nie zapisać połowicznie."""
        resp = client.post(
            "/api/plugins", json={"tuya_cloud": {"enabled": True}}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    def test_post_toggles_presence_simulation_without_clobbering_scenes(self, client, tmp_path):
        (tmp_path / "automations.yaml").write_text(
            "scenes:\n  - name: powitanie\n    actions: [{type: satel_disarm}]\n"
            "reguły_ruchu: []\n"
            "presence_simulation:\n  enabled: false\n  devices: [gniazdko]\n",
            encoding="utf-8",
        )
        import dashboard.app as app_module
        config = BridgeConfig.load(tmp_path / "config.yaml")
        app_module.create_app(config)

        resp = client.post(
            "/api/plugins", json={"presence_simulation": {"enabled": True}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        saved = yaml.safe_load((tmp_path / "automations.yaml").read_text(encoding="utf-8"))
        assert saved["presence_simulation"]["enabled"] is True
        assert saved["presence_simulation"]["devices"] == ["gniazdko"]
        assert saved["scenes"][0]["name"] == "powitanie"  # nietknięte

    def test_post_enqueues_bridge_reload_only_for_automations_plugins(self, client, tmp_path):
        (tmp_path / "automations.yaml").write_text(
            "scenes: []\nreguły_ruchu: []\npresence_simulation:\n  enabled: false\n  devices: [gniazdko]\n",
            encoding="utf-8",
        )
        import dashboard.app as app_module
        config = BridgeConfig.load(tmp_path / "config.yaml")
        app_module.create_app(config)

        client.post(
            "/api/plugins", json={"presence_simulation": {"enabled": True}},
            headers=_basic_auth_header("test_haslo_123"),
        )
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)


class TestDiscoveryEndpoints:
    """POST /api/discover/ebus i /api/discover/tuya - publiczne (tylko
    odczyt sieci), wykrywanie samo w sobie zamockowane (bridge/ebus_client.py
    i bridge/tuya_local.py mają własne testy jednostkowe z fake'ami -
    tutaj sprawdzamy tylko routing/obsługę błędów na poziomie endpointu)."""

    def test_discover_ebus_no_auth_required(self, client, monkeypatch):
        monkeypatch.setattr("dashboard.app.discover_ebus_topics", lambda host, port: ["ebusd/bai/FlowTemp"])
        resp = client.post("/api/discover/ebus", json={})
        assert resp.status_code == 200
        assert resp.get_json()["topics"] == ["ebusd/bai/FlowTemp"]

    def test_discover_ebus_uses_configured_mqtt_host_by_default(self, client, monkeypatch):
        captured = {}

        def fake_discover(host, port):
            captured["host"] = host
            captured["port"] = port
            return []

        monkeypatch.setattr("dashboard.app.discover_ebus_topics", fake_discover)
        client.post("/api/discover/ebus", json={})
        assert captured["host"] == "127.0.0.1"

    def test_discover_ebus_connection_error_returns_502(self, client, monkeypatch):
        def raise_connection_error(host, port):
            raise ConnectionError("nie udało się połączyć")

        monkeypatch.setattr("dashboard.app.discover_ebus_topics", raise_connection_error)
        resp = client.post("/api/discover/ebus", json={})
        assert resp.status_code == 502

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


class TestBackgroundEbusDiscoveryEndpoints:
    """/api/discover/ebus/start|status|stop - nasłuch MQTT ebusd W TLE (do
    kilku minut), w odróżnieniu od /api/discover/ebus (blokujące ~4s).
    Logika samego nasłuchu ma własne testy w test_ebus_client.py - tutaj
    tylko routing/obsługa błędów na poziomie endpointu (podmieniamy metody
    singletona _ebus_background_discovery na fake'i)."""

    def test_start_no_auth_required(self, client, monkeypatch):
        import dashboard.app as app_module

        monkeypatch.setattr(app_module._ebus_background_discovery, "start", lambda *a, **kw: None)
        monkeypatch.setattr(
            app_module._ebus_background_discovery, "status",
            lambda: {"running": True, "topics": [], "elapsed_seconds": 0, "duration_seconds": 300},
        )
        resp = client.post("/api/discover/ebus/start", json={})
        assert resp.status_code == 200
        assert resp.get_json()["running"] is True

    def test_start_uses_configured_mqtt_host_by_default(self, client, monkeypatch):
        import dashboard.app as app_module

        captured = {}
        monkeypatch.setattr(
            app_module._ebus_background_discovery, "start",
            lambda host, port, duration_seconds=300.0: captured.update(host=host, port=port),
        )
        monkeypatch.setattr(app_module._ebus_background_discovery, "status", lambda: {})
        client.post("/api/discover/ebus/start", json={})
        assert captured["host"] == "127.0.0.1"

    def test_start_connection_error_returns_502(self, client, monkeypatch):
        import dashboard.app as app_module

        def raise_connection_error(*a, **kw):
            raise ConnectionError("nie udało się połączyć")

        monkeypatch.setattr(app_module._ebus_background_discovery, "start", raise_connection_error)
        resp = client.post("/api/discover/ebus/start", json={})
        assert resp.status_code == 502

    def test_start_while_already_running_returns_409(self, client, monkeypatch):
        import dashboard.app as app_module

        def raise_running(*a, **kw):
            raise RuntimeError("Nasłuch już trwa")

        monkeypatch.setattr(app_module._ebus_background_discovery, "start", raise_running)
        resp = client.post("/api/discover/ebus/start", json={})
        assert resp.status_code == 409

    def test_duration_seconds_is_capped(self, client, monkeypatch):
        import dashboard.app as app_module

        captured = {}
        monkeypatch.setattr(
            app_module._ebus_background_discovery, "start",
            lambda host, port, duration_seconds=300.0: captured.update(duration_seconds=duration_seconds),
        )
        monkeypatch.setattr(app_module._ebus_background_discovery, "status", lambda: {})
        client.post("/api/discover/ebus/start", json={"duration_seconds": 99999})
        assert captured["duration_seconds"] == 600

    def test_status_no_auth_required(self, client, monkeypatch):
        import dashboard.app as app_module

        monkeypatch.setattr(
            app_module._ebus_background_discovery, "status",
            lambda: {"running": True, "topics": ["ebusd/bai/FlowTemp"], "elapsed_seconds": 5, "duration_seconds": 300},
        )
        resp = client.get("/api/discover/ebus/status")
        assert resp.status_code == 200
        assert resp.get_json()["topics"] == ["ebusd/bai/FlowTemp"]

    def test_stop_no_auth_required(self, client, monkeypatch):
        import dashboard.app as app_module

        monkeypatch.setattr(
            app_module._ebus_background_discovery, "stop",
            lambda: {"running": False, "topics": [], "elapsed_seconds": 5, "duration_seconds": 300},
        )
        resp = client.post("/api/discover/ebus/stop")
        assert resp.status_code == 200
        assert resp.get_json()["running"] is False


class TestSatelConnectionTestEndpoint:
    def test_no_auth_required(self, client, monkeypatch):
        async def fake_test_connection(host, port):
            return None

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        resp = client.post("/api/setup/test-satel-connection", json={"host": "192.168.1.10", "port": 7094})
        assert resp.status_code == 200

    def test_missing_host_returns_400(self, client):
        resp = client.post("/api/setup/test-satel-connection", json={})
        assert resp.status_code == 400

    def test_connection_error_returns_502_with_message(self, client, monkeypatch):
        async def fake_test_connection(host, port):
            raise ConnectionError(f"Nie udało się połączyć z {host}:{port}")

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        resp = client.post("/api/setup/test-satel-connection", json={"host": "10.0.0.5", "port": 7094})
        assert resp.status_code == 502
        assert "10.0.0.5" in resp.get_json()["error"]

    def test_default_port_used_when_omitted(self, client, monkeypatch):
        captured = {}

        async def fake_test_connection(host, port):
            captured["port"] = port

        monkeypatch.setattr("dashboard.app.test_satel_connection", fake_test_connection)
        client.post("/api/setup/test-satel-connection", json={"host": "192.168.1.10"})
        assert captured["port"] == 7094


class TestSatelDiscoveryEndpoint:
    """/api/discover/satel - wykrywanie best-effort aktywnych stref/wyjść
    w kroku 1 kreatora (patrz bridge/satel_client.py:
    discover_active_zones_and_outputs)."""

    def test_no_auth_required(self, client, monkeypatch):
        async def fake_discover(host, port, timeout_seconds=30.0):
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

        async def fake_discover(host, port, timeout_seconds=30.0):
            captured["host"] = host
            captured["port"] = port
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        resp = client.post("/api/discover/satel", json={})
        assert resp.status_code == 200
        assert captured == {"host": "127.0.0.1", "port": 7094}  # z CONFIG_TEMPLATE

    def test_explicit_host_in_body_overrides_configured(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0):
            captured["host"] = host
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        client.post("/api/discover/satel", json={"host": "10.0.0.9"})
        assert captured["host"] == "10.0.0.9"

    def test_connection_error_returns_502(self, client, monkeypatch):
        async def fake_discover(host, port, timeout_seconds=30.0):
            raise ConnectionError(f"Nie udało się połączyć z {host}:{port}")

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        resp = client.post("/api/discover/satel", json={"host": "10.0.0.5", "port": 7094})
        assert resp.status_code == 502
        assert "10.0.0.5" in resp.get_json()["error"]

    def test_timeout_seconds_is_capped(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0):
            captured["timeout_seconds"] = timeout_seconds
            return {"zones": [], "outputs": []}

        monkeypatch.setattr("dashboard.app.discover_active_zones_and_outputs", fake_discover)
        client.post("/api/discover/satel", json={"host": "192.168.1.10", "timeout_seconds": 99999})
        assert captured["timeout_seconds"] == 120

    def test_default_timeout_used_when_omitted(self, client, monkeypatch):
        captured = {}

        async def fake_discover(host, port, timeout_seconds=30.0):
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
