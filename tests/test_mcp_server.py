# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import base64

import pytest
from werkzeug.security import generate_password_hash

from bridge.config import BridgeConfig
from plugins.mcp_server import routes as mcp_routes

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
scenes:
  - name: "Scena testowa"
    enabled: true
    actions:
      - type: tuya_on
        device: gniazdko
  - name: "Scena wyłączona"
    enabled: false
    actions:
      - type: tuya_on
        device: gniazdko
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
def client(tmp_path, monkeypatch):
    from dashboard.app import app as quart_app
    from dashboard.app import create_app

    monkeypatch.setattr(mcp_routes, "_COMMAND_POLL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(mcp_routes, "_COMMAND_POLL_INTERVAL_SECONDS", 0.01)

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


async def _enable_mcp(client):
    resp = await client.post(
        "/api/plugins/mcp_server/settings", json={"enabled": True},
        headers=_basic_auth_header("test_haslo_123"),
    )
    assert resp.status_code == 200, await resp.get_data(as_text=True)


class TestMcpGate:

    async def test_disabled_by_default_returns_404(self, client):
        resp = await client.post(
            "/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 404

    async def test_requires_auth_even_when_enabled(self, client):
        await _enable_mcp(client)
        resp = await client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code == 401

    async def test_settings_default_enabled_false(self, client):
        resp = await client.get(
            "/api/plugins/mcp_server/settings", headers=_basic_auth_header("test_haslo_123"),
        )
        data = await resp.get_json()
        assert data["enabled"] is False


class TestMcpProtocol:

    async def test_initialize(self, client):
        await _enable_mcp(client)
        resp = await client.post(
            "/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert data["result"]["protocolVersion"]
        assert data["result"]["serverInfo"]["name"] == "satel-tuya-bridge"

    async def test_notifications_initialized_returns_202_empty(self, client):
        await _enable_mcp(client)
        resp = await client.post(
            "/api/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        assert resp.status_code == 202
        assert (await resp.get_data(as_text=True)) == ""

    async def test_tools_list_includes_expected_tools(self, client):
        await _enable_mcp(client)
        resp = await client.post(
            "/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        data = await resp.get_json()
        names = {t["name"] for t in data["result"]["tools"]}
        assert names == {
            "get_status", "list_scenes", "run_scene", "satel_arm", "satel_disarm",
            "trigger_virtual_input", "tuya_on", "tuya_off",
        }

    async def test_unknown_method_returns_jsonrpc_error(self, client):
        await _enable_mcp(client)
        resp = await client.post(
            "/api/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "nope"},
            headers=_basic_auth_header("test_haslo_123"),
        )
        data = await resp.get_json()
        assert data["error"]["code"] == -32601

    async def test_invalid_json_returns_parse_error(self, client):
        await _enable_mcp(client)
        resp = await client.post(
            "/api/mcp", data=b"not json", headers={**_basic_auth_header("test_haslo_123"), "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        data = await resp.get_json()
        assert data["error"]["code"] == -32700

    async def test_batch_of_requests(self, client):
        await _enable_mcp(client)
        resp = await client.post(
            "/api/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ],
            headers=_basic_auth_header("test_haslo_123"),
        )
        data = await resp.get_json()
        assert isinstance(data, list) and len(data) == 2
        assert {d["id"] for d in data} == {1, 2}


async def _call_tool(client, name: str, arguments: dict | None = None, req_id=1):
    resp = await client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
        headers=_basic_auth_header("test_haslo_123"),
    )
    assert resp.status_code == 200
    data = await resp.get_json()
    return data["result"]


class TestMcpTools:

    async def test_get_status_returns_status_list(self, client):
        await _enable_mcp(client)
        result = await _call_tool(client, "get_status")
        assert result["isError"] is False

    async def test_list_scenes(self, client):
        await _enable_mcp(client)
        result = await _call_tool(client, "list_scenes")
        assert result["isError"] is False
        import json
        payload = json.loads(result["content"][0]["text"])
        by_name = {s["name"]: s for s in payload["scenes"]}
        assert by_name["Scena testowa"]["enabled"] is True
        assert by_name["Scena wyłączona"]["enabled"] is False

    async def test_run_scene_unknown_is_tool_error(self, client):
        await _enable_mcp(client)
        result = await _call_tool(client, "run_scene", {"name": "nie ma takiej"})
        assert result["isError"] is True
        assert "nie ma takiej" in result["content"][0]["text"]

    async def test_run_scene_known_enqueues_command(self, client):
        await _enable_mcp(client)
        import dashboard.app as app_module
        result = await _call_tool(client, "run_scene", {"name": "Scena testowa"})
        assert result["isError"] is False
        pending = app_module.history.get_pending_commands()
        run_scene_cmds = [c for c in pending if c["command"] == "run_scene"]
        assert run_scene_cmds[0]["params"]["name"] == "Scena testowa"

    async def test_satel_arm_enqueues_command_with_default_partitions(self, client):
        await _enable_mcp(client)
        import dashboard.app as app_module
        result = await _call_tool(client, "satel_arm", {})
        assert result["isError"] is False
        pending = app_module.history.get_pending_commands()
        arm_cmds = [c for c in pending if c["command"] == "satel_arm"]
        assert len(arm_cmds) == 1
        assert set(arm_cmds[0]["params"]["partitions"]) == {1, 2}

    async def test_satel_disarm_with_explicit_partitions(self, client):
        await _enable_mcp(client)
        import dashboard.app as app_module
        result = await _call_tool(client, "satel_disarm", {"partitions": [1]})
        assert result["isError"] is False
        pending = app_module.history.get_pending_commands()
        disarm_cmds = [c for c in pending if c["command"] == "satel_disarm"]
        assert disarm_cmds[0]["params"]["partitions"] == [1]

    async def test_trigger_virtual_input_known(self, client):
        await _enable_mcp(client)
        import dashboard.app as app_module
        result = await _call_tool(client, "trigger_virtual_input", {"name": "motion_hall"})
        assert result["isError"] is False
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "satel_virtual_input" for c in pending)

    async def test_trigger_virtual_input_unknown_is_tool_error(self, client):
        await _enable_mcp(client)
        result = await _call_tool(client, "trigger_virtual_input", {"name": "brak"})
        assert result["isError"] is True

    async def test_tuya_on_known_device(self, client):
        await _enable_mcp(client)
        import dashboard.app as app_module
        result = await _call_tool(client, "tuya_on", {"name": "gniazdko"})
        assert result["isError"] is False
        pending = app_module.history.get_pending_commands()
        assert any(c["command"] == "tuya_on" for c in pending)

    async def test_tuya_off_unknown_device_is_tool_error(self, client):
        await _enable_mcp(client)
        result = await _call_tool(client, "tuya_off", {"name": "brak_takiego"})
        assert result["isError"] is True

    async def test_tuya_on_missing_name_is_tool_error(self, client):
        await _enable_mcp(client)
        result = await _call_tool(client, "tuya_on", {})
        assert result["isError"] is True

    async def test_unknown_tool_name_is_tool_error(self, client):
        await _enable_mcp(client)
        result = await _call_tool(client, "nie_takie_narzedzie")
        assert result["isError"] is True


class TestMcpInfoEndpoint:

    async def test_info_requires_auth(self, client):
        assert (await client.get("/api/plugins/mcp_server/info")).status_code == 401

    async def test_info_lists_tool_names(self, client):
        resp = await client.get("/api/plugins/mcp_server/info", headers=_basic_auth_header("test_haslo_123"))
        data = await resp.get_json()
        assert data["endpoint_url"].endswith("/api/mcp")
        assert "get_status" in data["tools"]
