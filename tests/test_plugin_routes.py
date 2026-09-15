import asyncio
import base64
import time

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


async def _save_cloud_account(
    client, access_id: str = "aid", access_key: str = "akey", api_region: str = "eu", name: str = "domyślne",
):
    return await client.post(
        "/api/plugins/tuya_cloud/accounts",
        json={"accounts": [{"name": name, "access_id": access_id, "access_key": access_key, "api_region": api_region}]},
        headers=_basic_auth_header("test_haslo_123"),
    )


@pytest.fixture
def client(tmp_path):
    from dashboard.app import app as quart_app
    from dashboard.app import create_app

    (tmp_path / "sensors.yaml").write_text("sensors: []\n", encoding="utf-8")
    password_hash = generate_password_hash("test_haslo_123")
    config_text = CONFIG_TEMPLATE.format(db_path=str(tmp_path / "history.db"), password_hash=password_hash)
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")

    config = BridgeConfig.load(tmp_path / "config.yaml")

    import dashboard.app as app_module
    app_module.config_path = config.db_path
    app_module.auth_guard = None

    create_app(config)
    quart_app.config["TESTING"] = True
    return quart_app.test_client()


class TestSatelPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/satel", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/satel", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_get_fields_requires_auth(self, client):
        assert (await client.get("/api/plugins/satel/fields")).status_code == 401
        resp = (await client.get("/api/plugins/satel/fields", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        data = await resp.get_json()
        assert {"name": "Dom", "number": 1} in data["partitions"]
        assert {"name": "Salon", "number": 12, "type": ""} in data["monitored_zones"]

    async def test_post_fields_without_auth_returns_401(self, client):
        resp = (await client.post("/api/plugins/satel/fields", json={}))
        assert resp.status_code == 401

    async def test_post_fields_persists_and_is_readable(self, client):
        import dashboard.app as app_module

        payload = {"partitions": [{"name": "Piwnica", "number": "9"}]}
        resp = (await client.post("/api/plugins/satel/fields", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        fields = await (await client.get("/api/plugins/satel/fields", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert {"name": "Piwnica", "number": 9} in fields["partitions"]
        assert app_module.config_store.get("satel_fields")["partitions"] == {"Piwnica": 9}

    async def test_post_fields_enqueues_bridge_reload(self, client):
        import dashboard.app as app_module

        (await client.post("/api/plugins/satel/fields", json={}, headers=_basic_auth_header("test_haslo_123")))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)

    async def test_scan_start_requires_auth(self, client):
        assert (await client.post("/api/plugins/satel/scan/start", json={"host": "127.0.0.1"})).status_code == 401

    async def test_scan_status_requires_auth(self, client):
        assert (await client.get("/api/plugins/satel/scan/status")).status_code == 401

    async def test_scan_start_missing_host_returns_400(self, client, monkeypatch):
        import dashboard.app as app_module

        raw = app_module.config_store.get("config")
        raw["satel"] = {}
        app_module.config_store.set("config", raw)
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.post(
            "/api/plugins/satel/scan/start", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_scan_start_status_runs_in_background(self, client, monkeypatch):
        import plugins.satel.routes as satel_routes

        monkeypatch.setattr(satel_routes._background_scan, "start", lambda *a, **kw: None)
        monkeypatch.setattr(
            satel_routes._background_scan, "status",
            lambda: {"running": True, "elapsed_seconds": 0, "duration_seconds": 30, "result": None, "error": None},
        )
        resp = (await client.post(
            "/api/plugins/satel/scan/start", json={"host": "127.0.0.1", "timeout_seconds": 30},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["running"] is True

        resp = (await client.get("/api/plugins/satel/scan/status", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["running"] is True

    async def test_scan_start_already_running_returns_409(self, client, monkeypatch):
        import plugins.satel.routes as satel_routes

        def _raise(*a, **kw):
            raise RuntimeError("Nasłuch już trwa - poczekaj na koniec i spróbuj ponownie")

        monkeypatch.setattr(satel_routes._background_scan, "start", _raise)
        resp = (await client.post(
            "/api/plugins/satel/scan/start", json={"host": "127.0.0.1"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 409

    async def test_settings_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/satel/settings")).status_code == 401
        resp = (await client.get("/api/plugins/satel/settings", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"enabled": True, "log_level": ""}

    async def test_settings_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/satel/settings", json={"log_level": "DEBUG"})).status_code == 401

    async def test_settings_post_sets_log_level_without_touching_host(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/satel/settings", json={"log_level": "debug"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert "bez restartu" in (await resp.get_json())["message"]
        saved = app_module.config_store.get("config")["satel"]
        assert saved["log_level"] == "DEBUG"
        assert saved["host"] == "127.0.0.1"

    async def test_settings_post_rejects_invalid_log_level(self, client):
        resp = (await client.post(
            "/api/plugins/satel/settings", json={"log_level": "LOUD"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_settings_post_toggles_enabled_and_reports_restart_required(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/satel/settings", json={"enabled": False},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert "restartu" in (await resp.get_json())["message"]
        assert app_module.config_store.get("config")["satel"]["enabled"] is False

    async def test_settings_post_does_not_clobber_fields_saved_via_satel_fields_doc(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/satel/fields", json={"partitions": [{"name": "Piwnica", "number": "9"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/satel/settings", json={"log_level": "INFO"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert app_module.config_store.get("config")["satel"]["log_level"] == "INFO"
        fields = await (await client.get("/api/plugins/satel/fields", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert {"name": "Piwnica", "number": 9} in fields["partitions"]


class TestSatelConnectionEndpoint:

    async def test_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/satel/connection")).status_code == 401
        resp = (await client.get("/api/plugins/satel/connection", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_get_returns_current_values_without_leaking_secret(self, client):
        resp = (await client.get("/api/plugins/satel/connection", headers=_basic_auth_header("test_haslo_123")))
        data = await resp.get_json()
        assert data["host"] == "127.0.0.1"
        assert data["port"] == 7094
        assert data["integration_key_set"] is True
        assert data["encryption_key_set"] is False
        assert data["encryption_enabled"] is False
        assert data["virtual_inputs_mode"] == "library"
        assert '"x"' not in await resp.get_data(as_text=True)

    async def test_post_without_auth_returns_401(self, client):
        resp = (await client.post("/api/plugins/satel/connection", json={}))
        assert resp.status_code == 401

    async def test_post_updates_host(self, client):
        import dashboard.app as app_module

        payload = {"host": "10.0.0.9", "port": "7094"}
        resp = (await client.post("/api/plugins/satel/connection", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert app_module.config_store.get("config")["satel"]["host"] == "10.0.0.9"

    async def test_post_validates_missing_host(self, client):
        resp = (await client.post(
            "/api/plugins/satel/connection", json={"host": ""}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_post_blank_integration_key_preserves_existing_secret(self, client):
        import dashboard.app as app_module

        payload = {"host": "127.0.0.1", "port": "7094", "integration_key": ""}
        (await client.post("/api/plugins/satel/connection", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert app_module.config_store.get("config")["satel"]["integration_key"] == "x"

    async def test_post_does_not_touch_zones_or_partitions(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/satel/connection", json={"host": "10.0.0.9"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        fields = await (await client.get("/api/plugins/satel/fields", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert {"name": "Dom", "number": 1} in fields["partitions"]
        assert {"name": "Salon", "number": 12, "type": ""} in fields["monitored_zones"]

    async def test_post_does_not_clobber_log_level_set_via_settings(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/satel/settings", json={"log_level": "DEBUG"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/satel/connection", json={"host": "10.0.0.9"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert app_module.config_store.get("config")["satel"]["log_level"] == "DEBUG"


class TestTuyaLocalPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/tuya", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/tuya", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_get_devices_requires_auth(self, client):
        assert (await client.get("/api/plugins/tuya/devices")).status_code == 401
        data = await (await client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert any(d["name"] == "gniazdko" and d["ip"] == "1.2.3.4" for d in data["devices"])

    async def test_post_devices_without_auth_returns_401(self, client):
        resp = (await client.post("/api/plugins/tuya/devices", json={"tuya_devices": []}))
        assert resp.status_code == 401

    async def test_post_devices_persists_and_is_readable(self, client):
        import dashboard.app as app_module

        payload = {"tuya_devices": [
            {"name": "trwale", "id": "d3", "ip": "1.2.3.6", "local_key": "k3", "type": "socket"},
        ]}
        resp = (await client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["saved"] == 1
        devices = await (await client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert any(d["name"] == "trwale" for d in devices["devices"])
        assert "trwale" in app_module.config_store.get("tuya_devices")["devices"]

    async def test_post_devices_rejects_incomplete_device(self, client):
        payload = {"tuya_devices": [{"name": "x", "id": ""}]}
        resp = (await client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400

    async def test_saved_zigbee_gateway_and_sub_device_round_trip(self, client):
        payload = {
            "tuya_devices": [
                {"name": "gniazdko", "id": "d1", "ip": "1.2.3.4", "local_key": "k", "type": "socket"},
                {"name": "lampka_zigbee", "id": "sub1", "gateway": "brama_salon", "cid": "node1", "type": "socket"},
            ],
            "zigbee_gateways": [
                {"name": "brama_salon", "id": "gw1", "ip": "1.2.3.9", "local_key": "gwkey", "version": "3.4"},
            ],
        }
        resp = (await client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

        data = await (await client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert {
            "name": "lampka_zigbee", "id": "sub1", "gateway": "brama_salon", "cid": "node1",
            "type": "socket", "ip": "", "local_key": "", "dimmer_dp": "", "version": "", "dp_schema": {},
        } in data["devices"]
        assert {"name": "brama_salon", "id": "gw1", "ip": "1.2.3.9", "local_key": "gwkey", "version": 3.4} \
            in data["zigbee_gateways"]

    async def test_post_devices_rejects_sub_device_with_unknown_gateway(self, client):
        payload = {"tuya_devices": [{"name": "lampka", "id": "sub1", "gateway": "brama_nieznana"}]}
        resp = (await client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400

    async def test_post_devices_preserves_cloud_devices(self, client):
        (await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        payload = {"tuya_devices": [
            {"name": "trwale", "id": "d3", "ip": "1.2.3.6", "local_key": "k3", "type": "socket"},
        ]}
        resp = (await client.post("/api/plugins/tuya/devices", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

        cloud_after = await (await client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert any(d["name"] == "lampa_taras" for d in cloud_after["devices"])

    async def test_scan_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", lambda **kw: [{"id": "d1", "ip": "1.2.3.4"}])
        assert (await client.post("/api/plugins/tuya/scan")).status_code == 401
        resp = (await client.post("/api/plugins/tuya/scan", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["devices"] == [
            {"id": "d1", "ip": "1.2.3.4", "known_name": "gniazdko", "known_gateway_name": ""},
        ]

    async def test_scan_known_name_empty_when_no_id_match(self, client, monkeypatch):
        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", lambda **kw: [{"id": "nieznany", "ip": "1.2.3.9"}])
        resp = (await client.post("/api/plugins/tuya/scan", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["devices"] == [
            {"id": "nieznany", "ip": "1.2.3.9", "known_name": "", "known_gateway_name": ""},
        ]

    async def test_scan_known_gateway_name_set_for_configured_zigbee_gateway(self, client, monkeypatch):
        await client.post(
            "/api/plugins/tuya/devices",
            json={"tuya_devices": [], "zigbee_gateways": [
                {"name": "brama", "id": "gw1", "ip": "", "local_key": "gwkey"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", lambda **kw: [{"id": "gw1", "ip": "1.2.3.5"}])
        resp = await client.post("/api/plugins/tuya/scan", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        devices = (await resp.get_json())["devices"]
        assert devices[0]["known_gateway_name"] == "brama"

    async def test_scan_passes_zigbee_gateway_key_as_known_tuyadevice(self, client, monkeypatch):
        await client.post(
            "/api/plugins/tuya/devices",
            json={"tuya_devices": [], "zigbee_gateways": [
                {"name": "brama", "id": "gw1", "ip": "", "local_key": "gwkey"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        captured = {}

        def fake_scan(**kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", fake_scan)
        resp = await client.post("/api/plugins/tuya/scan", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert {"id": "gw1", "key": "gwkey", "name": "brama"} in captured["tuyadevices"]

    async def test_scan_passes_configured_networks_and_known_local_devices_as_tuyadevices(self, client, monkeypatch):
        await client.post(
            "/api/plugins/tuya/scan-networks", json={"scan_networks": ["192.168.1.0/24"]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        captured = {}

        def fake_scan(**kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr("plugins.tuya_local.routes.scan_tuya_network", fake_scan)
        resp = await client.post("/api/plugins/tuya/scan", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert captured["networks"] == ["192.168.1.0/24"]
        assert {"id": "d1", "key": "k", "name": "gniazdko"} in captured["tuyadevices"]

    async def test_scan_networks_requires_auth_to_save(self, client):
        assert (await client.post("/api/plugins/tuya/scan-networks", json={"scan_networks": []})).status_code == 401

    async def test_scan_networks_get_defaults_to_empty_list(self, client):
        resp = await client.get("/api/plugins/tuya/scan-networks", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        assert (await resp.get_json())["scan_networks"] == []

    async def test_scan_networks_rejects_invalid_entry(self, client):
        resp = await client.post(
            "/api/plugins/tuya/scan-networks", json={"scan_networks": ["not-an-ip"]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    async def test_scan_networks_accepts_cidr_and_plain_ip_and_round_trips(self, client):
        resp = await client.post(
            "/api/plugins/tuya/scan-networks",
            json={"scan_networks": ["192.168.1.0/24", "192.168.1.50", ""]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert (await resp.get_json())["scan_networks"] == ["192.168.1.0/24", "192.168.1.50"]

        get_resp = await client.get("/api/plugins/tuya/scan-networks", headers=_basic_auth_header("test_haslo_123"))
        assert (await get_resp.get_json())["scan_networks"] == ["192.168.1.0/24", "192.168.1.50"]

    async def test_refresh_dp_schema_requires_auth(self, client):
        resp = await client.post("/api/plugins/tuya/devices/refresh-dp-schema")
        assert resp.status_code == 401

    async def test_refresh_dp_schema_without_cloud_credentials_returns_400(self, client):
        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        assert "Tuya Cloud" in (await resp.get_json())["error"]

    async def test_refresh_dp_schema_saves_fetched_schema(self, client, monkeypatch):
        await _save_cloud_account(client)

        def fake_fetch(api_region, access_id, access_key, device_ids):
            assert device_ids == ["d1"]
            return {"d1": {"1": {"code": "switch_1", "type": "Boolean", "values": "{}"}}}, {}

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_dp_schemas_for_devices", fake_fetch)

        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["updated"] == ["gniazdko"]
        assert data["failed"] == []

        devices = await (await client.get(
            "/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        gniazdko = next(d for d in devices["devices"] if d["name"] == "gniazdko")
        assert gniazdko["dp_schema"] == {"1": {"code": "switch_1", "type": "Boolean", "values": "{}"}}

    async def test_refresh_dp_schema_reports_per_device_failures(self, client, monkeypatch):
        await _save_cloud_account(client)

        def fake_fetch(api_region, access_id, access_key, device_ids):
            return {}, {"d1": "nie znaleziono urządzenia"}

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_dp_schemas_for_devices", fake_fetch)

        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["updated"] == []
        assert data["failed"] == ["gniazdko"]

    async def test_refresh_dp_schema_connection_error_returns_502(self, client, monkeypatch):
        await _save_cloud_account(client)

        def fake_fetch(api_region, access_id, access_key, device_ids):
            raise ConnectionError("Błąd logowania do Tuya Cloud: bad credentials")

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_dp_schemas_for_devices", fake_fetch)

        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 502

    async def test_settings_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/tuya_local/settings")).status_code == 401
        resp = (await client.get("/api/plugins/tuya_local/settings", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"enabled": True, "log_level": ""}

    async def test_settings_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/tuya_local/settings", json={"log_level": "DEBUG"})).status_code == 401

    async def test_settings_post_does_not_clobber_devices_saved_via_tuya_devices_doc(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/tuya/devices",
            json={"tuya_devices": [{"name": "trwale", "id": "d3", "ip": "1.2.3.6", "local_key": "k3", "type": "socket"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/tuya_local/settings", json={"enabled": False, "log_level": "WARNING"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        stored_tuya = app_module.config_store.get("config")["tuya"]
        assert stored_tuya["enabled"] is False
        assert stored_tuya["log_level"] == "WARNING"
        devices = await (await client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert any(d["name"] == "trwale" for d in devices["devices"])


class TestEbusPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/ebus", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/ebus", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_get_sensors_requires_auth(self, client):
        assert (await client.get("/api/plugins/ebus/sensors")).status_code == 401
        resp = (await client.get("/api/plugins/ebus/sensors", headers=_basic_auth_header("test_haslo_123")))
        assert await resp.get_json() == []

    async def test_post_sensors_without_auth_returns_401(self, client):
        resp = (await client.post("/api/plugins/ebus/sensors", json=[]))
        assert resp.status_code == 401

    async def test_post_sensors_persists_and_is_readable(self, client):
        import dashboard.app as app_module

        payload = [{
            "name": "kociol_temp", "label": "Temp", "mqtt_topic": "ebusd/x/y",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False, "tuya_dp": ""},
        }]
        resp = (await client.post("/api/plugins/ebus/sensors", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["saved"] == 1
        sensors = await (await client.get("/api/plugins/ebus/sensors", headers=_basic_auth_header("test_haslo_123"))).get_json()
        names = [s["name"] for s in sensors]
        assert "kociol_temp" in names
        assert app_module.config_store.get("sensors")["sensors"][0]["name"] == "kociol_temp"

    async def test_post_sensors_validates_required_fields(self, client):
        payload = [{"name": "", "mqtt_topic": "x", "targets": {}}]
        resp = (await client.post("/api/plugins/ebus/sensors", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400

    async def test_mqtt_field_round_trips_for_composite_json_topics(self, client):
        payload = [{
            "name": "temp_podlogowa", "label": "Temp. podłogowa", "mqtt_topic": "ebusd/hc1",
            "mqtt_field": "TempFloor",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False, "tuya_dp": ""},
        }]
        (await client.post("/api/plugins/ebus/sensors", json=payload, headers=_basic_auth_header("test_haslo_123")))
        resp = (await client.get("/api/plugins/ebus/sensors", headers=_basic_auth_header("test_haslo_123")))
        saved = next(s for s in await resp.get_json() if s["name"] == "temp_podlogowa")
        assert saved["mqtt_field"] == "TempFloor"

    async def test_mqtt_field_defaults_to_empty_string(self, client):
        payload = [{
            "name": "zwykly_czujnik", "mqtt_topic": "ebusd/bai/FlowTemp",
            "targets": {"history": True, "mqtt_publish": False, "tuya_virtual_device": False, "tuya_dp": ""},
        }]
        (await client.post("/api/plugins/ebus/sensors", json=payload, headers=_basic_auth_header("test_haslo_123")))
        resp = (await client.get("/api/plugins/ebus/sensors", headers=_basic_auth_header("test_haslo_123")))
        saved = next(s for s in await resp.get_json() if s["name"] == "zwykly_czujnik")
        assert saved["mqtt_field"] == ""

    async def test_discover_requires_auth(self, client, monkeypatch):
        monkeypatch.setattr(
            "plugins.ebus.routes.discover_ebus_topics", lambda host, port: ["ebusd/bai/FlowTemp"],
        )
        assert (await client.post("/api/plugins/ebus/discover", json={})).status_code == 401
        resp = (await client.post("/api/plugins/ebus/discover", json={}, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["topics"] == ["ebusd/bai/FlowTemp"]

    async def test_discover_uses_configured_mqtt_host_by_default(self, client, monkeypatch):
        captured = {}

        def fake_discover(host, port):
            captured["host"] = host
            return []

        monkeypatch.setattr("plugins.ebus.routes.discover_ebus_topics", fake_discover)
        (await client.post("/api/plugins/ebus/discover", json={}, headers=_basic_auth_header("test_haslo_123")))
        assert captured["host"] == "127.0.0.1"

    async def test_discover_does_not_block_event_loop(self, client, monkeypatch):
        def slow_discover(host, port):
            time.sleep(0.3)
            return ["ebusd/bai/FlowTemp"]

        monkeypatch.setattr("plugins.ebus.routes.discover_ebus_topics", slow_discover)

        ticks: list[float] = []
        finished_at: list[float] = []

        async def do_discover():
            resp = await client.post(
                "/api/plugins/ebus/discover", json={}, headers=_basic_auth_header("test_haslo_123"),
            )
            finished_at.append(time.monotonic())
            return resp

        async def ticker():
            for _ in range(10):
                await asyncio.sleep(0.03)
                ticks.append(time.monotonic())

        resp, _ = await asyncio.gather(do_discover(), ticker())

        assert resp.status_code == 200
        ticks_during = [t for t in ticks if t < finished_at[0]]
        assert len(ticks_during) >= 3, (
            "Pętla zdarzeń była zablokowana podczas wykrywania tematów eBUS - "
            "żaden tick nie zdążył wykonać się równolegle"
        )

    async def test_background_discovery_start_status_stop(self, client, monkeypatch):
        import plugins.ebus.routes as ebus_routes

        monkeypatch.setattr(ebus_routes._background_discovery, "start", lambda *a, **kw: None)
        monkeypatch.setattr(
            ebus_routes._background_discovery, "status",
            lambda: {"running": True, "topics": [], "elapsed_seconds": 0, "duration_seconds": 300},
        )
        resp = (await client.post("/api/plugins/ebus/discover/start", json={}, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["running"] is True

        resp = (await client.get("/api/plugins/ebus/discover/status", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

        monkeypatch.setattr(
            ebus_routes._background_discovery, "stop",
            lambda: {"running": False, "topics": [], "elapsed_seconds": 0, "duration_seconds": 300},
        )
        resp = (await client.post("/api/plugins/ebus/discover/stop", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["running"] is False

    async def test_background_discovery_start_missing_host_returns_400(self, client, monkeypatch):
        import dashboard.app as app_module

        raw = app_module.config_store.get("config")
        raw["ebus"] = {}
        app_module.config_store.set("config", raw)
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.post(
            "/api/plugins/ebus/discover/start", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_settings_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/ebus/settings")).status_code == 401
        resp = (await client.get("/api/plugins/ebus/settings", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"enabled": True, "log_level": ""}

    async def test_settings_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/ebus/settings", json={"log_level": "DEBUG"})).status_code == 401

    async def test_settings_post_does_not_duplicate_sensors_into_config_doc(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/ebus/settings", json={"log_level": "ERROR"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        stored_ebus = app_module.config_store.get("config")["ebus"]
        assert stored_ebus["log_level"] == "ERROR"
        assert "sensors" not in stored_ebus
        assert stored_ebus["mqtt_host"] == "127.0.0.1"


class TestEbusConnectionEndpoint:

    async def test_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/ebus/connection")).status_code == 401
        resp = (await client.get("/api/plugins/ebus/connection", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_get_returns_current_values(self, client):
        resp = (await client.get("/api/plugins/ebus/connection", headers=_basic_auth_header("test_haslo_123")))
        data = await resp.get_json()
        assert data["mqtt_host"] == "127.0.0.1"
        assert data["mqtt_port"] == 1883
        assert data["topic_prefix"] == "home/boiler"

    async def test_post_without_auth_returns_401(self, client):
        resp = (await client.post("/api/plugins/ebus/connection", json={"mqtt_host": "127.0.0.1"}))
        assert resp.status_code == 401

    async def test_post_updates_mqtt_host_and_topic_prefix(self, client):
        import dashboard.app as app_module

        payload = {"mqtt_host": "10.0.0.5", "mqtt_port": "1884", "topic_prefix": "dom/kociol"}
        resp = (await client.post("/api/plugins/ebus/connection", json=payload, headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")
        assert saved["ebus"]["mqtt_host"] == "10.0.0.5"
        assert saved["ebus"]["mqtt_port"] == 1884
        assert saved["boiler_output"]["mqtt"]["host"] == "10.0.0.5"
        assert saved["boiler_output"]["mqtt"]["port"] == 1884
        assert saved["boiler_output"]["mqtt"]["topic_prefix"] == "dom/kociol"

    async def test_post_validates_missing_mqtt_host(self, client):
        resp = (await client.post(
            "/api/plugins/ebus/connection", json={"mqtt_host": ""}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_post_does_not_touch_satel_section(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/ebus/connection", json={"mqtt_host": "10.0.0.5"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert app_module.config_store.get("config")["satel"]["host"] == "127.0.0.1"


class TestTuyaCloudPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/tuya_cloud", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_redirects_to_merged_tuya_page(self, client):
        resp = (await client.get(
            "/plugins/tuya_cloud", headers=_basic_auth_header("test_haslo_123"), follow_redirects=False,
        ))
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/plugins/tuya"

    async def test_accounts_get_requires_auth_and_defaults_to_empty(self, client):
        assert (await client.get("/api/plugins/tuya_cloud/accounts")).status_code == 401
        resp = (await client.get("/api/plugins/tuya_cloud/accounts", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"accounts": []}

    async def test_accounts_get_sees_legacy_single_account_migrated_by_bridge_config(self, client):
        """Regres: ten endpoint kiedyś czytał konta wprost z ConfigStore, z
        pominięciem migracji BridgeConfig.load() - dla instalacji sprzed
        wprowadzenia wielu kont (samo stare "tuya.cloud", bez "cloud_accounts")
        pokazywał 0 kont, mimo że urządzenia faktycznie działały przez to
        "domyślne" konto w prawdziwym procesie bridge'a."""
        import dashboard.app as app_module

        current = app_module.config_store.get("config")
        current.setdefault("tuya", {})["cloud"] = {
            "access_id": "legacy_id", "access_key": "legacy_secret", "api_region": "eu",
        }
        app_module.config_store.set("config", current)
        app_module._reload_bridge_config_from_disk()

        resp = await client.get("/api/plugins/tuya_cloud/accounts", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["accounts"] == [{
            "name": "domyślne", "access_id": "legacy_id", "access_key_set": True,
            "api_region": "eu", "api_endpoint": "", "mq_endpoint": "",
        }]

    async def test_saving_empty_accounts_list_does_not_permanently_disable_legacy_account(self, client):
        """Regres: kliknięcie 'Zapisz konta' bez dodania żadnego wiersza (np. gdy
        strona - z powodu powyższego błędu - pokazywała 0 kont mimo działającego
        starego konta) zapisywało do configu PUSTY słownik cloud_accounts. Bez
        złagodzenia migracji w bridge/config.py to trwale gasiło konto "domyślne"
        - każda kolejna komenda do Tuya Cloud kończyła się błędem "nieistniejące
        konto", mimo że stare poświadczenia nigdy nie zniknęły z tuya.cloud."""
        import dashboard.app as app_module

        current = app_module.config_store.get("config")
        current.setdefault("tuya", {})["cloud"] = {
            "access_id": "legacy_id", "access_key": "legacy_secret", "api_region": "eu",
        }
        app_module.config_store.set("config", current)
        app_module._reload_bridge_config_from_disk()

        resp = await client.post(
            "/api/plugins/tuya_cloud/accounts", json={"accounts": []},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200

        data = await (await client.get(
            "/api/plugins/tuya_cloud/accounts", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["accounts"] == [{
            "name": "domyślne", "access_id": "legacy_id", "access_key_set": True,
            "api_region": "eu", "api_endpoint": "", "mq_endpoint": "",
        }]

    async def test_accounts_post_rejects_unknown_region(self, client):
        resp = (await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [{"name": "domyślne", "access_id": "aid1", "api_region": "atlantyda"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_accounts_post_without_auth_returns_401(self, client):
        payload = {"accounts": [{"name": "domyślne", "access_id": "x"}]}
        assert (await client.post("/api/plugins/tuya_cloud/accounts", json=payload)).status_code == 401

    async def test_accounts_post_requires_name_and_access_id(self, client):
        resp = (await client.post(
            "/api/plugins/tuya_cloud/accounts", json={"accounts": [{}]}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_accounts_post_rejects_duplicate_names(self, client):
        resp = (await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [
                {"name": "dom", "access_id": "aid1"}, {"name": "dom", "access_id": "aid2"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_accounts_post_persists_and_never_leaks_secret_back(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [{
                "name": "domyślne", "access_id": "aid1", "access_key": "secret1", "api_region": "us",
            }]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["tuya"]["cloud_accounts"]["domyślne"]
        assert saved["access_id"] == "aid1"
        assert saved["access_key"] == "secret1"
        assert saved["api_region"] == "us"

        data = await (await client.get(
            "/api/plugins/tuya_cloud/accounts", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["accounts"] == [{
            "name": "domyślne", "access_id": "aid1", "access_key_set": True,
            "api_region": "us", "api_endpoint": "", "mq_endpoint": "",
        }]
        text = await (await client.get(
            "/api/plugins/tuya_cloud/accounts", headers=_basic_auth_header("test_haslo_123"),
        )).get_data(as_text=True)
        assert "secret1" not in text

    async def test_accounts_post_blank_secret_keeps_existing(self, client):
        import dashboard.app as app_module

        await _save_cloud_account(client, access_id="aid1", access_key="secret1")
        await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [{"name": "domyślne", "access_id": "aid1", "access_key": ""}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        saved = app_module.config_store.get("config")["tuya"]["cloud_accounts"]["domyślne"]
        assert saved["access_key"] == "secret1"

    async def test_accounts_post_supports_multiple_named_accounts(self, client):
        import dashboard.app as app_module

        resp = await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [
                {"name": "dom", "access_id": "aid1", "access_key": "key1", "api_region": "eu"},
                {"name": "garaz", "access_id": "aid2", "access_key": "key2", "api_region": "us"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        assert (await resp.get_json())["saved"] == 2
        saved = app_module.config_store.get("config")["tuya"]["cloud_accounts"]
        assert set(saved) == {"dom", "garaz"}
        assert saved["garaz"]["api_region"] == "us"

    async def test_settings_endpoint_no_longer_exists_separately(self, client):
        # Tuya Cloud nie ma już własnego przełącznika włącz/wyłącz + poziom logowania -
        # wtyczki zostały scalone w jedną ("tuya_local"), z jednym wspólnym ustawieniem.
        resp = (await client.get("/api/plugins/tuya_cloud/settings", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 404

    async def test_devices_get_requires_auth_and_empty_by_default(self, client):
        assert (await client.get("/api/plugins/tuya_cloud/devices")).status_code == 401
        resp = (await client.get("/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"devices": []}

    async def test_devices_post_without_auth_returns_401(self, client):
        payload = {"cloud_devices": [{"name": "lampa", "id": "c1"}]}
        assert (await client.post("/api/plugins/tuya_cloud/devices", json=payload)).status_code == 401

    async def test_devices_post_rejects_missing_id(self, client):
        payload = {"cloud_devices": [{"name": "lampa", "id": ""}]}
        resp = (await client.post(
            "/api/plugins/tuya_cloud/devices", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_devices_post_saves_and_roundtrips(self, client):
        payload = {"cloud_devices": [{"name": "lampa_taras", "id": "c1", "category": "kg", "switch_code": "switch_1"}]}
        resp = (await client.post(
            "/api/plugins/tuya_cloud/devices", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["saved"] == 1

        data = await (await client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["devices"] == [
            {
                "name": "lampa_taras", "id": "c1", "category": "kg", "switch_code": "switch_1",
                "local_key": "", "ip": "", "version": "", "dp_schema": {}, "cloud_account": "",
            },
        ]

    async def test_devices_post_roundtrips_cloud_account(self, client):
        payload = {"cloud_devices": [{"name": "lampa_taras", "id": "c1", "cloud_account": "garaz"}]}
        resp = (await client.post(
            "/api/plugins/tuya_cloud/devices", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200

        data = await (await client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["devices"][0]["cloud_account"] == "garaz"

    async def test_devices_post_preserves_local_devices_and_gateways(self, client):
        before = await (await client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert any(d["name"] == "gniazdko" for d in before["devices"])

        payload = {"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]}
        resp = (await client.post(
            "/api/plugins/tuya_cloud/devices", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200

        after = await (await client.get("/api/plugins/tuya/devices", headers=_basic_auth_header("test_haslo_123"))).get_json()
        assert any(d["name"] == "gniazdko" for d in after["devices"])
        cloud_after = await (await client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert any(d["name"] == "lampa_taras" for d in cloud_after["devices"])

    async def test_cloud_import_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/tuya_cloud/cloud-import")).status_code == 401

    async def test_cloud_import_requires_configured_account(self, client):
        resp = (await client.post(
            "/api/plugins/tuya_cloud/cloud-import", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400
        assert "konto" in (await resp.get_json())["error"].lower()

    async def test_cloud_import_uses_stored_account_credentials(self, client, monkeypatch):
        captured = {}

        def _fake_fetch(api_region, access_id, access_key, networks):
            captured.update(api_region=api_region, access_id=access_id, access_key=access_key)
            return [{"id": "c1", "name": "Lampa", "local_key": "", "ip": "", "category": "kg"}]

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        await _save_cloud_account(client, access_id="aid1", access_key="secret1", api_region="us")
        resp = (await client.post(
            "/api/plugins/tuya_cloud/cloud-import", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured == {"api_region": "us", "access_id": "aid1", "access_key": "secret1"}
        data = await resp.get_json()
        assert data["devices"][0]["name"] == "Lampa"
        assert data["devices"][0]["cloud_account"] == "domyślne"

    async def test_cloud_import_picks_explicitly_requested_account(self, client, monkeypatch):
        captured = {}

        def _fake_fetch(api_region, access_id, access_key, networks):
            captured.update(api_region=api_region, access_id=access_id, access_key=access_key)
            return [{"id": "c2", "name": "Wentylator", "local_key": "", "ip": "", "category": "fs"}]

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [
                {"name": "dom", "access_id": "aid1", "access_key": "key1", "api_region": "eu"},
                {"name": "garaz", "access_id": "aid2", "access_key": "key2", "api_region": "us"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        resp = (await client.post(
            "/api/plugins/tuya_cloud/cloud-import", json={"account": "garaz"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert captured == {"api_region": "us", "access_id": "aid2", "access_key": "key2"}
        data = await resp.get_json()
        assert data["account"] == "garaz"
        assert data["devices"][0]["cloud_account"] == "garaz"

    async def test_cloud_import_connection_error_returns_502(self, client, monkeypatch):
        def _fake_fetch(api_region, access_id, access_key, networks):
            raise ConnectionError("błąd logowania")

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        await _save_cloud_account(client, access_id="aid1", access_key="secret1")
        resp = (await client.post(
            "/api/plugins/tuya_cloud/cloud-import", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 502

    async def test_refresh_dp_schema_saves_fetched_schema(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        def fake_fetch(api_region, access_id, access_key, device_ids):
            if device_ids == ["c1"]:
                return {"c1": {"1": {"code": "manual_delay", "type": "Value", "values": "{}"}}}, {}
            return {}, {dev_id: "nie znaleziono urządzenia" for dev_id in device_ids}

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_dp_schemas_for_devices", fake_fetch)

        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["updated"] == ["lampa_taras"]
        assert "gniazdko" in data["failed"]

        devices = await (await client.get(
            "/api/plugins/tuya_cloud/devices", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        lampa = next(d for d in devices["devices"] if d["name"] == "lampa_taras")
        assert lampa["dp_schema"] == {"1": {"code": "manual_delay", "type": "Value", "values": "{}"}}

    async def test_refresh_dp_schema_reports_per_device_failures(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        def fake_fetch(api_region, access_id, access_key, device_ids):
            return {}, {"c1": "nie znaleziono urządzenia"}

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_dp_schemas_for_devices", fake_fetch)

        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "lampa_taras" in data["failed"]

    async def test_refresh_dp_schema_connection_error_returns_502(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        def fake_fetch(api_region, access_id, access_key, device_ids):
            raise ConnectionError("Błąd logowania do Tuya Cloud: bad credentials")

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_dp_schemas_for_devices", fake_fetch)

        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 502

    async def test_refresh_dp_schema_queries_each_account_separately(self, client, monkeypatch):
        await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [
                {"name": "dom", "access_id": "aid1", "access_key": "key1", "api_region": "eu"},
                {"name": "garaz", "access_id": "aid2", "access_key": "key2", "api_region": "us"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [
                {"name": "lampa_dom", "id": "c1", "cloud_account": "dom"},
                {"name": "lampa_garaz", "id": "c2", "cloud_account": "garaz"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        seen_regions = []

        def fake_fetch(api_region, access_id, access_key, device_ids):
            seen_regions.append(api_region)
            return {dev_id: {"1": {"code": "switch_1", "type": "Boolean", "values": "{}"}} for dev_id in device_ids}, {}

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_dp_schemas_for_devices", fake_fetch)

        resp = await client.post(
            "/api/plugins/tuya/devices/refresh-dp-schema", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert set(data["updated"]) >= {"lampa_dom", "lampa_garaz"}
        assert set(seen_regions) == {"eu", "us"}

    async def test_rescan_requires_auth(self, client):
        assert (await client.post("/api/plugins/tuya_cloud/devices/rescan")).status_code == 401

    async def test_rescan_without_cloud_accounts_returns_400(self, client):
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        assert "konto" in (await resp.get_json())["error"].lower()

    async def test_rescan_without_devices_returns_400(self, client):
        await _save_cloud_account(client)
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    async def test_rescan_reports_devices_now_reachable_on_lan(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [
                {"name": "lampa_taras", "id": "c1"}, {"name": "kamera", "id": "c2"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        def _fake_fetch(api_region, access_id, access_key, networks):
            return [
                {"id": "c1", "name": "Lampa", "local_key": "key1", "ip": "192.168.1.20", "version": "3.3", "dp_schema": {}},
                {"id": "c2", "name": "Kamera", "local_key": "", "ip": "", "version": "", "dp_schema": {}},
                {"id": "c3", "name": "Nieznane", "local_key": "keyX", "ip": "192.168.1.99", "version": "3.3", "dp_schema": {}},
            ]

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        by_name = {d["name"]: d for d in data["devices"]}
        assert set(by_name) == {"lampa_taras", "kamera"}
        assert by_name["lampa_taras"]["can_move_to_local"] is True
        assert by_name["lampa_taras"]["ip"] == "192.168.1.20"
        assert by_name["kamera"]["can_move_to_local"] is False

    async def test_rescan_reports_devices_movable_to_configured_gateway(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya/devices",
            json={"zigbee_gateways": [{"name": "brama_salon", "id": "gw1", "ip": "192.168.1.5", "local_key": "gwkey"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [
                {"name": "czujnik_ruchu", "id": "c1"}, {"name": "kamera", "id": "c2"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        def _fake_fetch(api_region, access_id, access_key, networks):
            return [
                {"id": "c1", "name": "Czujnik", "local_key": "", "ip": "", "version": "",
                 "gateway_id": "gw1", "node_id": "0x01", "dp_schema": {}},
                {"id": "c2", "name": "Kamera", "local_key": "", "ip": "", "version": "",
                 "gateway_id": "", "node_id": "", "dp_schema": {}},
            ]

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        by_name = {d["name"]: d for d in data["devices"]}
        assert by_name["czujnik_ruchu"]["can_move_to_gateway"] is True
        assert by_name["czujnik_ruchu"]["gateway_name"] == "brama_salon"
        assert by_name["czujnik_ruchu"]["cid"] == "0x01"
        assert by_name["kamera"]["can_move_to_gateway"] is False

    async def test_rescan_flags_mismatched_cid_on_already_configured_zigbee_device(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya/devices",
            json={
                "zigbee_gateways": [{"name": "brama_salon", "id": "gw1", "ip": "192.168.1.5", "local_key": "gwkey"}],
                "tuya_devices": [
                    {"name": "czujnik_ruchu", "id": "sub1", "gateway": "brama_salon", "cid": "0xSTALE"},
                    {"name": "lampka", "id": "sub2", "gateway": "brama_salon", "cid": "0x02"},
                ],
            },
            headers=_basic_auth_header("test_haslo_123"),
        )

        def _fake_fetch(api_region, access_id, access_key, networks):
            return [
                {"id": "sub1", "name": "Czujnik", "local_key": "", "ip": "", "version": "",
                 "gateway_id": "gw1", "node_id": "0xFRESH", "dp_schema": {}},
                {"id": "sub2", "name": "Lampka", "local_key": "", "ip": "", "version": "",
                 "gateway_id": "gw1", "node_id": "0x02", "dp_schema": {}},
            ]

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        by_name = {c["name"]: c for c in data["zigbee_cid_checks"]}
        assert by_name["czujnik_ruchu"]["mismatch"] is True
        assert by_name["czujnik_ruchu"]["current_cid"] == "0xSTALE"
        assert by_name["czujnik_ruchu"]["cloud_cid"] == "0xFRESH"
        assert by_name["lampka"]["mismatch"] is False
        assert "nieaktualny cid" in data["message"]

    async def test_rescan_works_with_only_zigbee_devices_and_no_cloud_devices(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya/devices",
            json={
                "zigbee_gateways": [{"name": "brama_salon", "id": "gw1", "ip": "192.168.1.5", "local_key": "gwkey"}],
                "tuya_devices": [{"name": "czujnik_ruchu", "id": "sub1", "gateway": "brama_salon", "cid": "0x01"}],
            },
            headers=_basic_auth_header("test_haslo_123"),
        )

        def _fake_fetch(api_region, access_id, access_key, networks):
            return [
                {"id": "sub1", "name": "Czujnik", "local_key": "", "ip": "", "version": "",
                 "gateway_id": "gw1", "node_id": "0x01", "dp_schema": {}},
            ]

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["devices"] == []
        assert data["zigbee_cid_checks"][0]["mismatch"] is False

    async def test_rescan_connection_error_returns_502(self, client, monkeypatch):
        await _save_cloud_account(client)
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [{"name": "lampa_taras", "id": "c1"}]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        def _fake_fetch(api_region, access_id, access_key, networks):
            raise ConnectionError("Błąd logowania do Tuya Cloud: bad credentials")

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 502

    async def test_rescan_matches_devices_to_their_own_account_only(self, client, monkeypatch):
        await client.post(
            "/api/plugins/tuya_cloud/accounts",
            json={"accounts": [
                {"name": "dom", "access_id": "aid1", "access_key": "key1", "api_region": "eu"},
                {"name": "garaz", "access_id": "aid2", "access_key": "key2", "api_region": "us"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )
        await client.post(
            "/api/plugins/tuya_cloud/devices",
            json={"cloud_devices": [
                {"name": "lampa_dom", "id": "c1", "cloud_account": "dom"},
                {"name": "lampa_garaz", "id": "c2", "cloud_account": "garaz"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        )

        def _fake_fetch(api_region, access_id, access_key, networks):
            if api_region == "eu":
                return [{"id": "c1", "name": "Lampa", "local_key": "k1", "ip": "192.168.1.20", "dp_schema": {}}]
            return [{"id": "c2", "name": "Lampa2", "local_key": "k2", "ip": "192.168.1.21", "dp_schema": {}}]

        monkeypatch.setattr("plugins.tuya_local.routes.fetch_tuya_cloud_devices", _fake_fetch)
        resp = await client.post(
            "/api/plugins/tuya_cloud/devices/rescan", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        by_name = {d["name"]: d for d in data["devices"]}
        assert set(by_name) == {"lampa_dom", "lampa_garaz"}
        assert by_name["lampa_dom"]["cloud_account"] == "dom"
        assert by_name["lampa_garaz"]["cloud_account"] == "garaz"


class TestHomeAssistantPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/home_assistant", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/home_assistant", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_broker_get_requires_auth_and_falls_back_to_ebus_host(self, client):
        assert (await client.get("/api/plugins/home_assistant/broker")).status_code == 401
        resp = (await client.get("/api/plugins/home_assistant/broker", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["mqtt_host"] == ""
        assert data["ebus_mqtt_host"] == "127.0.0.1"

    async def test_broker_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/home_assistant/broker", json={})).status_code == 401

    async def test_broker_post_falls_back_to_ebus_host_when_blank(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/home_assistant/broker", json={"mqtt_host": ""},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["home_assistant"]
        assert saved["mqtt_host"] == "127.0.0.1"

    async def test_broker_post_rejects_when_no_host_available(self, client):
        import dashboard.app as app_module

        raw = app_module.config_store.get("config")
        raw["ebus"] = {}
        app_module.config_store.set("config", raw)
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.post(
            "/api/plugins/home_assistant/broker", json={"mqtt_host": ""},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_settings_post_does_not_clobber_broker_fields(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/home_assistant/broker", json={"mqtt_host": "10.0.0.5", "device_name": "Custom"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/home_assistant/settings", json={"enabled": True, "log_level": "INFO"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        saved = app_module.config_store.get("config")["home_assistant"]
        assert saved["mqtt_host"] == "10.0.0.5"
        assert saved["device_name"] == "Custom"
        assert saved["enabled"] is True
        assert saved["log_level"] == "INFO"


class TestMqttPublishPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/mqtt_publish", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/mqtt_publish", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_topics_get_requires_auth_and_defaults(self, client):
        assert (await client.get("/api/plugins/mqtt_publish/topics")).status_code == 401
        resp = (await client.get("/api/plugins/mqtt_publish/topics", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {
            "satel_topic": "satel", "tuya_topic": "tuya", "homewhiz_topic": "homewhiz",
            "lg_thinq_topic": "lg_thinq",
        }

    async def test_topics_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/mqtt_publish/topics", json={})).status_code == 401

    async def test_topics_post_persists_and_settings_do_not_clobber_them(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/mqtt_publish/topics",
            json={
                "satel_topic": "moj_satel", "tuya_topic": "moje_tuya", "homewhiz_topic": "moj_homewhiz",
                "lg_thinq_topic": "moj_lg_thinq",
            },
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/mqtt_publish/settings", json={"log_level": "WARNING"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        saved = app_module.config_store.get("config")["mqtt_publish"]
        assert saved["topics"] == {
            "satel": "moj_satel", "tuya": "moje_tuya", "homewhiz": "moj_homewhiz", "lg_thinq": "moj_lg_thinq",
        }
        assert saved["log_level"] == "WARNING"

    async def test_explorer_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/mqtt_publish/explorer")).status_code == 401

    async def test_explorer_get_default_prefixes_and_empty_topics(self, client):
        resp = await client.get("/api/plugins/mqtt_publish/explorer", headers=_basic_auth_header("test_haslo_123"))
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["topics"] == []
        assert set(data["known_prefixes"]) == {
            "homeassistant", "home/bridge", "satel", "tuya", "homewhiz", "lg_thinq", "home/boiler",
        }

    async def test_explorer_get_shows_recorded_topics(self, client):
        import dashboard.app as app_module
        app_module.history.set_status("mqtt:tuya/gniazdko/state", "mqtt_topic", "ON", label="tuya/gniazdko/state")
        resp = await client.get("/api/plugins/mqtt_publish/explorer", headers=_basic_auth_header("test_haslo_123"))
        data = await resp.get_json()
        assert data["topics"] == [{"topic": "tuya/gniazdko/state", "payload": "ON", "ts": data["topics"][0]["ts"]}]

    async def test_explorer_delete_without_auth_returns_401(self, client):
        assert (await client.delete("/api/plugins/mqtt_publish/explorer")).status_code == 401

    async def test_explorer_delete_clears_recorded_topics(self, client):
        import dashboard.app as app_module
        app_module.history.set_status("mqtt:tuya/gniazdko/state", "mqtt_topic", "ON", label="tuya/gniazdko/state")

        resp = await client.delete(
            "/api/plugins/mqtt_publish/explorer", headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200

        after = await client.get("/api/plugins/mqtt_publish/explorer", headers=_basic_auth_header("test_haslo_123"))
        assert (await after.get_json())["topics"] == []

    async def test_publish_requires_auth(self, client):
        assert (await client.post("/api/plugins/mqtt_publish/publish", json={"topic": "tuya/x"})).status_code == 401

    async def test_publish_rejects_topic_outside_known_prefixes(self, client):
        resp = await client.post(
            "/api/plugins/mqtt_publish/publish", json={"topic": "nie/nasz/temat", "payload": "x"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "prefix" in data["error"]

    async def test_publish_rejects_missing_topic(self, client):
        resp = await client.post(
            "/api/plugins/mqtt_publish/publish", json={}, headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 400

    async def test_publish_accepts_known_prefix_and_enqueues_command(self, client):
        import dashboard.app as app_module
        resp = await client.post(
            "/api/plugins/mqtt_publish/publish",
            json={"topic": "tuya/gniazdko/test", "payload": "ON", "retain": True},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "command_id" in data
        pending = app_module.history.get_pending_commands()
        mqtt_cmds = [c for c in pending if c["command"] == "mqtt_publish"]
        assert mqtt_cmds[0]["params"] == {"topic": "tuya/gniazdko/test", "payload": "ON", "retain": True}

    async def test_publish_accepts_bare_prefix_without_trailing_segment(self, client):
        resp = await client.post(
            "/api/plugins/mqtt_publish/publish", json={"topic": "satel", "payload": "x"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200


class TestUnifiProtectPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/unifi_protect", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/unifi_protect", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_credentials_get_requires_auth_and_defaults_when_unconfigured(self, client):
        assert (await client.get("/api/plugins/unifi_protect/credentials")).status_code == 401
        resp = (await client.get("/api/plugins/unifi_protect/credentials", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"configured": False, "host": "", "username": "", "password_set": False, "verify_ssl": False}

    async def test_credentials_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/unifi_protect/credentials", json={"host": "x"})).status_code == 401

    async def test_credentials_post_requires_host(self, client):
        resp = (await client.post(
            "/api/plugins/unifi_protect/credentials", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_credentials_post_persists_and_never_leaks_password_back(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/unifi_protect/credentials",
            json={"host": "192.168.1.1", "username": "admin", "password": "s3cr3t", "verify_ssl": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["unifi_protect"]
        assert saved["host"] == "192.168.1.1"
        assert saved["password"] == "s3cr3t"
        text = await (await client.get(
            "/api/plugins/unifi_protect/credentials", headers=_basic_auth_header("test_haslo_123"),
        )).get_data(as_text=True)
        assert "s3cr3t" not in text

    async def test_settings_post_rejects_enabling_when_not_configured(self, client):
        resp = (await client.post(
            "/api/plugins/unifi_protect/settings", json={"enabled": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_settings_post_succeeds_once_configured(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/unifi_protect/credentials", json={"host": "192.168.1.1"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/unifi_protect/settings", json={"enabled": True, "log_level": "ERROR"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["unifi_protect"]
        assert saved["enabled"] is True
        assert saved["log_level"] == "ERROR"
        assert saved["host"] == "192.168.1.1"

    async def test_cameras_get_without_config_returns_400(self, client):
        resp = (await client.get(
            "/api/plugins/unifi_protect/cameras", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_cameras_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/unifi_protect/cameras")).status_code == 401

    async def test_cameras_get_lists_cameras(self, client, monkeypatch):
        import dashboard.app as app_module
        import plugins.unifi_protect.routes as unifi_routes

        (await client.post(
            "/api/plugins/unifi_protect/credentials", json={"host": "192.168.1.1"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        class FakeCamera:
            def __init__(self, id, name):
                self.id = id
                self.name = name

        class FakeClient:
            async def get_cameras(self):
                return [FakeCamera("cam1", "Wejście")]

        async def _fake_get_client(section):
            return FakeClient()

        monkeypatch.setattr(unifi_routes, "_get_client", _fake_get_client)

        resp = (await client.get(
            "/api/plugins/unifi_protect/cameras", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["cameras"] == [{"id": "cam1", "name": "Wejście"}]

    async def test_cameras_get_connection_error_returns_502(self, client, monkeypatch):
        import plugins.unifi_protect.routes as unifi_routes

        (await client.post(
            "/api/plugins/unifi_protect/credentials", json={"host": "192.168.1.1"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def _fake_get_client(section):
            raise ConnectionError("nie udało się połączyć")

        monkeypatch.setattr(unifi_routes, "_get_client", _fake_get_client)

        resp = (await client.get(
            "/api/plugins/unifi_protect/cameras", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 502

    async def test_snapshot_get_without_config_returns_400(self, client):
        resp = (await client.get(
            "/api/plugins/unifi_protect/cameras/cam1/snapshot", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_snapshot_get_requires_auth(self, client):
        assert (await client.get("/api/plugins/unifi_protect/cameras/cam1/snapshot")).status_code == 401

    async def test_snapshot_get_returns_jpeg_bytes(self, client, monkeypatch):
        import plugins.unifi_protect.routes as unifi_routes

        (await client.post(
            "/api/plugins/unifi_protect/credentials", json={"host": "192.168.1.1"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        class FakeClient:
            async def get_camera_snapshot(self, camera_id):
                assert camera_id == "cam1"
                return b"\xff\xd8\xff\xe0fakejpeg"

        async def _fake_get_client(section):
            return FakeClient()

        monkeypatch.setattr(unifi_routes, "_get_client", _fake_get_client)

        resp = (await client.get(
            "/api/plugins/unifi_protect/cameras/cam1/snapshot", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert resp.headers["Cache-Control"] == "no-store"
        assert (await resp.get_data()) == b"\xff\xd8\xff\xe0fakejpeg"

    async def test_snapshot_get_returns_404_when_camera_offline(self, client, monkeypatch):
        import plugins.unifi_protect.routes as unifi_routes

        (await client.post(
            "/api/plugins/unifi_protect/credentials", json={"host": "192.168.1.1"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        class FakeClient:
            async def get_camera_snapshot(self, camera_id):
                return None

        async def _fake_get_client(section):
            return FakeClient()

        monkeypatch.setattr(unifi_routes, "_get_client", _fake_get_client)

        resp = (await client.get(
            "/api/plugins/unifi_protect/cameras/cam1/snapshot", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 404

    async def test_call_with_retry_retries_once_with_fresh_client(self, monkeypatch):
        import plugins.unifi_protect.routes as unifi_routes

        unifi_routes._client_cache.clear()
        section = {"host": "192.168.1.1", "username": "u", "password": "p", "verify_ssl": False}
        get_client_calls = {"n": 0}

        async def _fake_get_client(sect):
            get_client_calls["n"] += 1
            key = unifi_routes._client_cache_key(sect)
            client = object()
            unifi_routes._client_cache[key] = client
            return client

        monkeypatch.setattr(unifi_routes, "_get_client", _fake_get_client)

        action_calls = {"n": 0}

        async def action(client):
            action_calls["n"] += 1
            if action_calls["n"] == 1:
                raise RuntimeError("stale session")
            return "ok"

        result = await unifi_routes._call_with_retry(section, action)
        assert result == "ok"
        assert get_client_calls["n"] == 2
        assert action_calls["n"] == 2


class TestHomeWhizPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/homewhiz", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/homewhiz", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_credentials_get_requires_auth_and_defaults_when_unconfigured(self, client):
        assert (await client.get("/api/plugins/homewhiz/credentials")).status_code == 401
        resp = (await client.get("/api/plugins/homewhiz/credentials", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"username": "", "password_set": False, "language": "en"}

    async def test_credentials_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/homewhiz/credentials", json={"username": "x"})).status_code == 401

    async def test_credentials_post_requires_username(self, client):
        resp = (await client.post(
            "/api/plugins/homewhiz/credentials", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_credentials_post_persists_and_never_leaks_password_back(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/homewhiz/credentials",
            json={"username": "user@example.com", "password": "s3cr3t", "language": "pl"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["username"] == "user@example.com"
        assert saved["password"] == "s3cr3t"
        assert saved["language"] == "pl"
        text = await (await client.get(
            "/api/plugins/homewhiz/credentials", headers=_basic_auth_header("test_haslo_123"),
        )).get_data(as_text=True)
        assert "s3cr3t" not in text

    async def test_credentials_post_blank_password_keeps_existing(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "user@example.com", "password": "s3cr3t"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "user@example.com", "password": ""},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["password"] == "s3cr3t"

    async def test_appliances_get_defaults_to_empty(self, client):
        resp = (await client.get("/api/plugins/homewhiz/appliances", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"appliances": []}

    async def test_appliances_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/homewhiz/appliances", json={"appliances": []})).status_code == 401

    async def test_appliances_post_requires_credentials_first(self, client):
        resp = (await client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_appliances_post_rejects_missing_appliance_id(self, client):
        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/homewhiz/appliances", json={"appliances": [{"name": "Zmywarka", "appliance_id": ""}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_appliances_post_rejects_duplicate_names(self, client):
        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [
                {"name": "Zmywarka", "appliance_id": "a1"}, {"name": "Zmywarka", "appliance_id": "a2"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_appliances_post_persists(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["appliances"] == {"Zmywarka": {"appliance_id": "abc123"}}

    async def test_settings_post_rejects_enabling_when_not_configured(self, client):
        resp = (await client.post(
            "/api/plugins/homewhiz/settings", json={"enabled": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_settings_post_succeeds_once_configured(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/homewhiz/settings", json={"enabled": True, "log_level": "ERROR"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["homewhiz"]
        assert saved["enabled"] is True
        assert saved["log_level"] == "ERROR"
        assert saved["username"] == "u"

    async def test_discover_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/homewhiz/discover")).status_code == 401

    async def test_discover_requires_credentials_first(self, client):
        resp = (await client.post("/api/plugins/homewhiz/discover", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400

    async def test_discover_returns_appliances_from_client(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizSession

        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def fake_login(username, password):
            return HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")

        async def fake_list_appliances(session):
            return [
                {"applianceId": "a1", "name": "Zmywarka", "brand": "Beko", "model": "X1", "applianceType": 3},
                {"applianceId": "a2", "name": "Pralka", "brand": "Beko", "model": "Y1", "applianceType": 1},
            ]

        monkeypatch.setattr(homewhiz_routes_module, "login", fake_login)
        monkeypatch.setattr(homewhiz_routes_module, "list_appliances", fake_list_appliances)

        resp = (await client.post("/api/plugins/homewhiz/discover", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        appliances = (await resp.get_json())["appliances"]
        assert {a["appliance_id"] for a in appliances} == {"a1", "a2"}
        assert next(a for a in appliances if a["appliance_id"] == "a1")["is_dishwasher"] is True
        assert next(a for a in appliances if a["appliance_id"] == "a2")["is_dishwasher"] is False

    async def test_discover_reports_homewhiz_error(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizError

        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def fake_login(username, password):
            raise HomeWhizError("zły login lub hasło")

        monkeypatch.setattr(homewhiz_routes_module, "login", fake_login)

        resp = (await client.post("/api/plugins/homewhiz/discover", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 502
        assert "login" in (await resp.get_json())["error"]

    async def test_appliance_states_without_auth_returns_401(self, client):
        assert (await client.get("/api/plugins/homewhiz/appliances/Zmywarka/states")).status_code == 401

    async def test_appliance_states_requires_credentials_first(self, client):
        resp = (await client.get(
            "/api/plugins/homewhiz/appliances/Zmywarka/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_appliance_states_unknown_appliance_returns_404(self, client):
        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.get(
            "/api/plugins/homewhiz/appliances/Nieznana/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 404

    async def test_appliance_states_returns_list_from_config_descriptor(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizSession

        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))

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

        resp = (await client.get(
            "/api/plugins/homewhiz/appliances/Zmywarka/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["states"] == ["idle", "finished"]

    async def test_appliance_states_reports_homewhiz_error(self, client, monkeypatch):
        import plugins.homewhiz.routes as homewhiz_routes_module
        from bridge.homewhiz_client import HomeWhizError

        (await client.post(
            "/api/plugins/homewhiz/credentials", json={"username": "u", "password": "p"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/homewhiz/appliances",
            json={"appliances": [{"name": "Zmywarka", "appliance_id": "abc123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def fake_login(username, password):
            raise HomeWhizError("zły login lub hasło")

        monkeypatch.setattr(homewhiz_routes_module, "login", fake_login)

        resp = (await client.get(
            "/api/plugins/homewhiz/appliances/Zmywarka/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 502


class TestLgThinqPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/lg_thinq", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/lg_thinq", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_credentials_get_requires_auth_and_defaults_when_unconfigured(self, client):
        assert (await client.get("/api/plugins/lg_thinq/credentials")).status_code == 401
        resp = (await client.get("/api/plugins/lg_thinq/credentials", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"pat_set": False, "country_code": "PL", "client_id": ""}

    async def test_credentials_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/lg_thinq/credentials", json={"personal_access_token": "x"})).status_code == 401

    async def test_credentials_post_requires_country_code(self, client):
        resp = (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": ""},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_credentials_post_requires_pat_on_first_save(self, client):
        resp = (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_credentials_post_persists_and_generates_stable_client_id(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/lg_thinq/credentials",
            json={"personal_access_token": "s3cr3t-pat", "country_code": "pl"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["lg_thinq"]
        assert saved["personal_access_token"] == "s3cr3t-pat"
        assert saved["country_code"] == "PL"
        client_id = saved["client_id"]
        assert client_id
        text = await (await client.get(
            "/api/plugins/lg_thinq/credentials", headers=_basic_auth_header("test_haslo_123"),
        )).get_data(as_text=True)
        assert "s3cr3t-pat" not in text

        # Zapis bez PAT (np. sama zmiana kraju) NIE zmienia client_id - musi
        # zostać stały dla tego samego tokenu, inaczej LG traktuje to jak
        # inne urządzenie kliencie.
        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"country_code": "DE"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        saved_again = app_module.config_store.get("config")["lg_thinq"]
        assert saved_again["client_id"] == client_id
        assert saved_again["personal_access_token"] == "s3cr3t-pat"
        assert saved_again["country_code"] == "DE"

    async def test_devices_get_defaults_to_empty(self, client):
        resp = (await client.get("/api/plugins/lg_thinq/devices", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"devices": []}

    async def test_devices_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/lg_thinq/devices", json={"devices": []})).status_code == 401

    async def test_devices_post_requires_credentials_first(self, client):
        resp = (await client.post(
            "/api/plugins/lg_thinq/devices",
            json={"devices": [{"name": "Pralka", "device_id": "abc"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_devices_post_rejects_missing_device_id(self, client):
        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/lg_thinq/devices", json={"devices": [{"name": "Pralka", "device_id": ""}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_devices_post_rejects_duplicate_names(self, client):
        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/lg_thinq/devices",
            json={"devices": [
                {"name": "Pralka", "device_id": "d1"}, {"name": "Pralka", "device_id": "d2"},
            ]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_devices_post_persists(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/lg_thinq/devices",
            json={"devices": [{"name": "Pralka", "device_id": "dev-123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["lg_thinq"]
        assert saved["devices"] == {"Pralka": {"device_id": "dev-123"}}

    async def test_settings_post_rejects_enabling_when_not_configured(self, client):
        resp = (await client.post(
            "/api/plugins/lg_thinq/settings", json={"enabled": True},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_settings_post_succeeds_once_configured(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/lg_thinq/devices",
            json={"devices": [{"name": "Pralka", "device_id": "dev-123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/plugins/lg_thinq/settings", json={"enabled": True, "log_level": "ERROR"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["lg_thinq"]
        assert saved["enabled"] is True
        assert saved["log_level"] == "ERROR"
        assert saved["personal_access_token"] == "pat"

    async def test_discover_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/lg_thinq/discover")).status_code == 401

    async def test_discover_requires_credentials_first(self, client):
        resp = (await client.post("/api/plugins/lg_thinq/discover", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 400

    async def test_discover_returns_devices_from_client(self, client, monkeypatch):
        import plugins.lg_thinq.routes as lg_thinq_routes_module

        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def fake_list_devices(pat, country_code, client_id):
            assert pat == "pat"
            assert country_code == "PL"
            return [
                {"device_id": "d1", "alias": "Pralka", "device_type": "DEVICE_WASHER", "model_name": "F4WV"},
                {"device_id": "d2", "alias": "Suszarka", "device_type": "DEVICE_DRYER", "model_name": "R9"},
            ]

        monkeypatch.setattr(lg_thinq_routes_module, "list_devices", fake_list_devices)

        resp = (await client.post("/api/plugins/lg_thinq/discover", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        devices = (await resp.get_json())["devices"]
        assert {d["device_id"] for d in devices} == {"d1", "d2"}

    async def test_discover_reports_lg_thinq_error(self, client, monkeypatch):
        import plugins.lg_thinq.routes as lg_thinq_routes_module
        from bridge.lg_thinq_client import LgThinqError

        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def fake_list_devices(pat, country_code, client_id):
            raise LgThinqError("nieprawidłowy token PAT")

        monkeypatch.setattr(lg_thinq_routes_module, "list_devices", fake_list_devices)

        resp = (await client.post("/api/plugins/lg_thinq/discover", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 502
        assert "PAT" in (await resp.get_json())["error"]

    async def test_device_states_without_auth_returns_401(self, client):
        assert (await client.get("/api/plugins/lg_thinq/devices/Pralka/states")).status_code == 401

    async def test_device_states_requires_credentials_first(self, client):
        resp = (await client.get(
            "/api/plugins/lg_thinq/devices/Pralka/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_device_states_unknown_device_returns_404(self, client):
        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.get(
            "/api/plugins/lg_thinq/devices/Nieznane/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 404

    async def test_device_states_returns_list_from_profile(self, client, monkeypatch):
        import plugins.lg_thinq.routes as lg_thinq_routes_module

        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/lg_thinq/devices",
            json={"devices": [{"name": "Pralka", "device_id": "dev-123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def fake_fetch_possible_states(pat, country_code, client_id, device_id):
            assert device_id == "dev-123"
            return ["wyłączona", "pranie", "zakończone"]

        monkeypatch.setattr(lg_thinq_routes_module, "fetch_possible_states", fake_fetch_possible_states)

        resp = (await client.get(
            "/api/plugins/lg_thinq/devices/Pralka/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert (await resp.get_json())["states"] == ["wyłączona", "pranie", "zakończone"]

    async def test_device_states_reports_lg_thinq_error(self, client, monkeypatch):
        import plugins.lg_thinq.routes as lg_thinq_routes_module
        from bridge.lg_thinq_client import LgThinqError

        (await client.post(
            "/api/plugins/lg_thinq/credentials", json={"personal_access_token": "pat", "country_code": "PL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/lg_thinq/devices",
            json={"devices": [{"name": "Pralka", "device_id": "dev-123"}]},
            headers=_basic_auth_header("test_haslo_123"),
        ))

        async def fake_fetch_possible_states(pat, country_code, client_id, device_id):
            raise LgThinqError("nieprawidłowy token PAT")

        monkeypatch.setattr(lg_thinq_routes_module, "fetch_possible_states", fake_fetch_possible_states)

        resp = (await client.get(
            "/api/plugins/lg_thinq/devices/Pralka/states", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 502


class TestNotificationsPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/notifications", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/notifications", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_topic_get_requires_auth_and_defaults(self, client):
        assert (await client.get("/api/plugins/notifications/topic")).status_code == 401
        resp = (await client.get("/api/plugins/notifications/topic", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"ntfy_topic_url": "", "grace_seconds": 120}

    async def test_topic_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/notifications/topic", json={})).status_code == 401

    async def test_topic_post_rejects_negative_grace_seconds(self, client):
        resp = (await client.post(
            "/api/plugins/notifications/topic", json={"grace_seconds": -5},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_topic_post_persists_and_settings_do_not_clobber_it(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/notifications/topic",
            json={"ntfy_topic_url": "https://ntfy.sh/xyz", "grace_seconds": 60},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        (await client.post(
            "/api/plugins/notifications/settings", json={"log_level": "CRITICAL"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        saved = app_module.config_store.get("config")["notifications"]
        assert saved["ntfy_topic_url"] == "https://ntfy.sh/xyz"
        assert saved["grace_seconds"] == 60
        assert saved["log_level"] == "CRITICAL"


class TestPresenceSimulationPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/presence_simulation", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_page_loads_when_authenticated(self, client):
        resp = (await client.get("/plugins/presence_simulation", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200

    async def test_summary_get_requires_auth_and_defaults_empty(self, client):
        assert (await client.get("/api/plugins/presence_simulation/summary")).status_code == 401
        resp = (await client.get("/api/plugins/presence_simulation/summary", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"devices": []}

    async def test_settings_get_requires_auth_and_defaults(self, client):
        assert (await client.get("/api/plugins/presence_simulation/settings")).status_code == 401
        resp = (await client.get("/api/plugins/presence_simulation/settings", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert await resp.get_json() == {"enabled": True, "log_level": ""}

    async def test_settings_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/presence_simulation/settings", json={"log_level": "DEBUG"})).status_code == 401

    async def test_settings_post_persists_to_automations_doc_without_clobbering_scenes(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [{"name": "powitanie", "actions": [{"type": "satel_disarm"}]}],
            "reguły_ruchu": [],
            "presence_simulation": {"enabled": False, "devices": ["gniazdko"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.post(
            "/api/plugins/presence_simulation/settings", json={"enabled": True, "log_level": "DEBUG"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("automations")
        assert saved["presence_simulation"]["enabled"] is True
        assert saved["presence_simulation"]["log_level"] == "DEBUG"
        assert saved["presence_simulation"]["devices"] == ["gniazdko"]
        assert saved["scenes"][0]["name"] == "powitanie"

    async def test_summary_reflects_devices_from_automations_doc(self, client):
        import dashboard.app as app_module

        app_module.config_store.set("automations", {
            "scenes": [], "reguły_ruchu": [],
            "presence_simulation": {"enabled": True, "devices": ["gniazdko", "lampka"]},
        })
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        data = await (await client.get(
            "/api/plugins/presence_simulation/summary", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert data["devices"] == ["gniazdko", "lampka"]

    async def test_automations_page_save_does_not_clobber_log_level(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/presence_simulation/settings", json={"log_level": "WARNING"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        resp = (await client.post(
            "/api/config/automations",
            json={
                "scenes": [],
                "motion_rules": [],
                "presence_simulation": {"enabled": False, "devices": []},
            },
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("automations")["presence_simulation"]
        assert saved["log_level"] == "WARNING"


class TestHomekitPluginPage:
    async def test_page_redirects_to_login_without_auth(self, client):
        resp = (await client.get("/plugins/homekit", follow_redirects=False))
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    async def test_settings_default_enabled_false(self, client):
        resp = (await client.get("/api/plugins/homekit/settings", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        assert (await resp.get_json())["enabled"] is False

    async def test_config_get_requires_auth_and_defaults(self, client):
        assert (await client.get("/api/plugins/homekit/config")).status_code == 401
        resp = (await client.get("/api/plugins/homekit/config", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data == {
            "port": 51826, "bridge_name": "Satel-Tuya-eBUS", "pincode": None, "paired": False,
        }

    async def test_config_post_without_auth_returns_401(self, client):
        assert (await client.post("/api/plugins/homekit/config", json={})).status_code == 401

    async def test_config_post_rejects_out_of_range_port(self, client):
        resp = (await client.post(
            "/api/plugins/homekit/config", json={"port": 70000},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_config_post_rejects_non_numeric_port(self, client):
        resp = (await client.post(
            "/api/plugins/homekit/config", json={"port": "abc"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_config_post_persists_and_generates_pincode(self, client):
        import dashboard.app as app_module

        resp = (await client.post(
            "/api/plugins/homekit/config", json={"port": 51900, "bridge_name": "Mój most"},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        saved = app_module.config_store.get("config")["homekit"]
        assert saved["port"] == 51900
        assert saved["bridge_name"] == "Mój most"
        assert saved["pincode"]

        config_resp = await (await client.get(
            "/api/plugins/homekit/config", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert config_resp["port"] == 51900
        assert config_resp["bridge_name"] == "Mój most"
        assert config_resp["pincode"] == saved["pincode"]

    async def test_regenerate_pincode_requires_auth_and_changes_value(self, client):
        assert (await client.post("/api/plugins/homekit/regenerate-pincode")).status_code == 401

        first = await (await client.post(
            "/api/plugins/homekit/regenerate-pincode", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        second = await (await client.post(
            "/api/plugins/homekit/regenerate-pincode", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert first["pincode"] != second["pincode"]

    async def test_reset_pairings_requires_auth_and_clears_paired_flag(self, client, tmp_path):
        assert (await client.post("/api/plugins/homekit/reset-pairings")).status_code == 401

        persist_path = tmp_path / "homekit_state.json"
        persist_path.write_text('{"paired_clients": {"abc": "def"}}', encoding="utf-8")
        paired_before = await (await client.get(
            "/api/plugins/homekit/config", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert paired_before["paired"] is True

        resp = (await client.post(
            "/api/plugins/homekit/reset-pairings", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200
        assert not persist_path.exists()

        paired_after = await (await client.get(
            "/api/plugins/homekit/config", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert paired_after["paired"] is False

    async def test_reset_pairings_is_idempotent_when_no_state_file(self, client):
        resp = (await client.post(
            "/api/plugins/homekit/reset-pairings", headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200

    async def test_entities_get_lists_candidates_with_defaults(self, client):
        resp = (await client.get("/api/plugins/homekit/entities", headers=_basic_auth_header("test_haslo_123")))
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["rooms"] == []
        assert {"name": "Dom", "enabled": True} in data["partitions"]
        assert {
            "name": "gniazdko", "type": "", "room": "", "is_dimmer": False, "detected_type": "",
        } in data["devices"]
        assert data["scenes"] == []
        type_ids = {choice["id"] for choice in data["type_choices"]}
        assert type_ids == {
            "switch", "outlet", "lightbulb", "fan", "garage_door", "window_covering", "valve", "lock",
            "temperature_sensor", "humidity_sensor", "temp_humidity_sensor", "leak_sensor", "contact_sensor",
        }

    async def test_entities_get_exposes_detected_type_for_recognized_dp_schema(self, client):
        import dashboard.app as app_module

        current = app_module.config_store.get("config")
        current["tuya"]["devices"]["gniazdko"]["dp_schema"] = {
            "1": {"code": "watersensor_state", "type": "Enum"},
        }
        app_module.config_store.set("config", current)
        app_module.create_app(BridgeConfig.load(app_module.config_path))

        resp = (await client.get("/api/plugins/homekit/entities", headers=_basic_auth_header("test_haslo_123")))
        data = await resp.get_json()
        device = next(d for d in data["devices"] if d["name"] == "gniazdko")
        assert device["detected_type"] == "leak_sensor"
        assert device["type"] == "leak_sensor"

    async def test_entities_post_without_auth_returns_401(self, client):
        resp = (await client.post("/api/plugins/homekit/entities", json={}))
        assert resp.status_code == 401

    async def test_entities_post_rejects_unknown_partition(self, client):
        resp = (await client.post(
            "/api/plugins/homekit/entities", json={"partitions": {"Brak": {"enabled": True}}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_entities_post_rejects_unknown_device(self, client):
        resp = (await client.post(
            "/api/plugins/homekit/entities", json={"devices": {"brak": {"type": "switch"}}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_entities_post_rejects_unknown_device_type(self, client):
        resp = (await client.post(
            "/api/plugins/homekit/entities", json={"devices": {"gniazdko": {"type": "nie_taki"}}},
            headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 400

    async def test_entities_post_persists_rooms_and_selections(self, client):
        import dashboard.app as app_module

        payload = {
            "rooms": ["Salon", "Salon", "Kuchnia", ""],
            "partitions": {"Dom": {"enabled": False}},
            "devices": {"gniazdko": {"type": "outlet", "room": "Kuchnia"}},
            "scenes": {},
        }
        resp = (await client.post(
            "/api/plugins/homekit/entities", json=payload, headers=_basic_auth_header("test_haslo_123"),
        ))
        assert resp.status_code == 200

        saved = app_module.config_store.get("config")["homekit"]
        assert saved["rooms"] == ["Salon", "Kuchnia"]
        assert saved["partitions"] == {"Dom": {"enabled": False}}
        assert saved["devices"] == {"gniazdko": {"type": "outlet", "room": "Kuchnia"}}

        after = await (await client.get(
            "/api/plugins/homekit/entities", headers=_basic_auth_header("test_haslo_123"),
        )).get_json()
        assert after["rooms"] == ["Salon", "Kuchnia"]
        assert {"name": "Dom", "enabled": False} in after["partitions"]
        assert {
            "name": "gniazdko", "type": "outlet", "room": "Kuchnia", "is_dimmer": False, "detected_type": "",
        } in after["devices"]

    async def test_entities_post_enqueues_bridge_reload(self, client):
        import dashboard.app as app_module

        (await client.post(
            "/api/plugins/homekit/entities", json={}, headers=_basic_auth_header("test_haslo_123"),
        ))
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "reload_config" for c in pending)
