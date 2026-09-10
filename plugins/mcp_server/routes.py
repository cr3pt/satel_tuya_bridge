from __future__ import annotations

import asyncio
import json
import logging
import time

from quart import Quart, jsonify, render_template, request

from plugins import PluginMeta, register_settings_route

logger = logging.getLogger("bridge.mcp_server")


def describe_state(config_raw: dict) -> dict:
    cfg = config_raw.get("mcp_server", {}) or {}
    enabled = bool(cfg.get("enabled"))
    return {
        "enabled": enabled,
        "configured": True,
        "detail": "aktywny (POST /api/mcp) - wymaga tokenu API" if enabled else "wyłączony",
    }


META = PluginMeta(
    id="mcp_server",
    name="Serwer MCP (Model Context Protocol)",
    icon="🤖",
    description="Udostępnia sterowanie systemem jako narzędzia MCP dla asystentów AI (Claude Desktop, "
                "Claude Code itp.) - wymaga tokenu API.",
    config_url="/plugins/mcp_server",
    config_key="mcp_server",
    restart_required=False,
    toggle_route="toggle_no_prereq",
    describe_state=describe_state,
    category="Dom",
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "satel-tuya-bridge"
SERVER_VERSION = "1.0.0"

_COMMAND_POLL_TIMEOUT_SECONDS = 5.0
_COMMAND_POLL_INTERVAL_SECONDS = 0.2


class _ToolError(Exception):
    pass


def _tool_defs() -> list[dict]:
    return [
        {
            "name": "get_status",
            "description": "Zwraca aktualny stan czujników/wejść/wyjść/urządzeń, opcjonalnie filtrowany po kategorii.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Opcjonalny filtr kategorii (np. 'satel_zone', 'tuya', 'ebus')."},
                },
            },
        },
        {
            "name": "list_scenes",
            "description": "Zwraca listę zdefiniowanych scen (nazwa, czy włączona, liczba akcji).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "run_scene",
            "description": "Uruchamia scenę o podanej nazwie.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Nazwa sceny."}},
                "required": ["name"],
            },
        },
        {
            "name": "satel_arm",
            "description": "Uzbraja wskazane strefy centrali Satel (domyślnie wszystkie skonfigurowane).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "partitions": {"type": "array", "items": {"type": "integer"}, "description": "Numery stref do uzbrojenia."},
                    "mode": {"type": "integer", "description": "Tryb uzbrojenia (0 = pełne, patrz dokumentacja centrali)."},
                },
            },
        },
        {
            "name": "satel_disarm",
            "description": "Rozbraja wskazane strefy centrali Satel (domyślnie wszystkie skonfigurowane).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "partitions": {"type": "array", "items": {"type": "integer"}, "description": "Numery stref do rozbrojenia."},
                },
            },
        },
        {
            "name": "trigger_virtual_input",
            "description": "Aktywuje wirtualne wejście centrali Satel o podanej nazwie.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Nazwa wirtualnego wejścia."}},
                "required": ["name"],
            },
        },
        {
            "name": "tuya_on",
            "description": "Włącza urządzenie Tuya o podanej nazwie.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Nazwa urządzenia Tuya."}},
                "required": ["name"],
            },
        },
        {
            "name": "tuya_off",
            "description": "Wyłącza urządzenie Tuya o podanej nazwie.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Nazwa urządzenia Tuya."}},
                "required": ["name"],
            },
        },
    ]


async def _await_command(history, command_id: int) -> dict | None:
    deadline = time.monotonic() + _COMMAND_POLL_TIMEOUT_SECONDS
    cmd = history.get_command(command_id)
    while cmd is not None and cmd["status"] == "pending" and time.monotonic() < deadline:
        await asyncio.sleep(_COMMAND_POLL_INTERVAL_SECONDS)
        cmd = history.get_command(command_id)
    return cmd


def _command_result(cmd: dict | None, fallback_message: str) -> dict:
    if cmd is None:
        return {"status": "unknown", "message": fallback_message}
    if cmd["status"] == "error":
        raise _ToolError((cmd.get("result") or {}).get("error", "Nieznany błąd wykonania komendy"))
    if cmd["status"] == "done":
        return {"status": "done", "message": fallback_message, "result": cmd.get("result")}
    return {"status": "pending", "command_id": cmd["id"], "message": fallback_message + " (nadal przetwarzane w tle)"}


async def _tool_get_status(args, *, history, **_ctx) -> dict:
    category = (args.get("category") or "").strip() or None
    return {"status": history.get_status(category)}


async def _tool_list_scenes(args, *, get_config_raw, **_ctx) -> dict:
    scenes = get_config_raw().get("scenes", [])
    return {"scenes": [
        {"name": s.get("name"), "enabled": s.get("enabled", True), "actions": len(s.get("actions", []))}
        for s in scenes
    ]}


async def _tool_run_scene(args, *, get_config_raw, history, **_ctx) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        raise _ToolError("Brak wymaganego argumentu 'name'")
    scene_names = {s["name"] for s in get_config_raw().get("scenes", [])}
    if name not in scene_names:
        raise _ToolError(f"Nieznana scena: '{name}'")
    command_id = history.enqueue_command("run_scene", {"name": name})
    cmd = await _await_command(history, command_id)
    return _command_result(cmd, f"Uruchomiono scenę '{name}'")


async def _tool_satel_arm(args, *, get_satel_partitions, history, **_ctx) -> dict:
    partitions = args.get("partitions") or list(get_satel_partitions().values())
    mode = args.get("mode", 0)
    command_id = history.enqueue_command("satel_arm", {"partitions": partitions, "mode": mode})
    history.write_event("system", source="mcp", detail={"message": f"MCP: zlecono uzbrojenie stref {partitions}"})
    cmd = await _await_command(history, command_id)
    return _command_result(cmd, f"Zlecono uzbrojenie stref {partitions}")


async def _tool_satel_disarm(args, *, get_satel_partitions, history, **_ctx) -> dict:
    partitions = args.get("partitions") or list(get_satel_partitions().values())
    command_id = history.enqueue_command("satel_disarm", {"partitions": partitions})
    history.write_event("system", source="mcp", detail={"message": f"MCP: zlecono rozbrojenie stref {partitions}"})
    cmd = await _await_command(history, command_id)
    return _command_result(cmd, f"Zlecono rozbrojenie stref {partitions}")


async def _tool_trigger_virtual_input(args, *, get_config_raw, history, **_ctx) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        raise _ToolError("Brak wymaganego argumentu 'name'")
    inputs = get_config_raw().get("satel", {}).get("virtual_inputs", {}).get("inputs", {})
    if name not in inputs:
        raise _ToolError(f"Nieznane wirtualne wejście '{name}'")
    command_id = history.enqueue_command("satel_virtual_input", {"name": name})
    history.write_event("system", source="mcp", detail={"message": f"MCP: zlecono aktywację wirtualnego wejścia '{name}'"})
    cmd = await _await_command(history, command_id)
    return _command_result(cmd, f"Aktywowano wirtualne wejście '{name}'")


async def _tool_tuya_set(args, *, get_config_raw, history, turn_on: bool, **_ctx) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        raise _ToolError("Brak wymaganego argumentu 'name'")
    tuya_cfg = get_config_raw().get("tuya", {})
    if name not in tuya_cfg.get("devices", {}) and name not in tuya_cfg.get("cloud_devices", {}):
        raise _ToolError(f"Nieznane urządzenie Tuya '{name}'")
    command_id = history.enqueue_command("tuya_on" if turn_on else "tuya_off", {"name": name})
    history.write_event("system", source="mcp", detail={
        "message": f"MCP: zlecono {'włączenie' if turn_on else 'wyłączenie'} urządzenia Tuya '{name}'",
    })
    cmd = await _await_command(history, command_id)
    return _command_result(cmd, f"{'Włączono' if turn_on else 'Wyłączono'} urządzenie '{name}'")


_TOOL_HANDLERS = {
    "get_status": _tool_get_status,
    "list_scenes": _tool_list_scenes,
    "run_scene": _tool_run_scene,
    "satel_arm": _tool_satel_arm,
    "satel_disarm": _tool_satel_disarm,
    "trigger_virtual_input": _tool_trigger_virtual_input,
    "tuya_on": lambda args, **ctx: _tool_tuya_set(args, turn_on=True, **ctx),
    "tuya_off": lambda args, **ctx: _tool_tuya_set(args, turn_on=False, **ctx),
}


def register(app: Quart, *, require_auth, get_auth_guard, get_config_raw, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, get_history, get_satel_partitions,
             **_ignored) -> PluginMeta:

    def _get_mcp_section() -> dict:
        return get_config_store().get("config").get("mcp_server", {}) or {}

    def _save_mcp_section(section: dict) -> None:
        current = get_config_store().get("config")
        current["mcp_server"] = section
        get_config_store().set("config", current)

    @app.route("/plugins/mcp_server")
    async def mcp_server_plugin_page():
        return await render_template("mcp_server/index.html")

    @app.route("/api/plugins/mcp_server/info", methods=["GET"])
    @require_auth(get_guard=get_auth_guard)
    async def api_plugin_mcp_server_info():
        return jsonify({
            "endpoint_url": request.url_root.rstrip("/") + "/api/mcp",
            "tool_count": len(_tool_defs()),
            "tools": [t["name"] for t in _tool_defs()],
        })

    async def _dispatch_tool_call(name: str, arguments: dict) -> dict:
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise _ToolError(f"Nieznane narzędzie: '{name}'")
        ctx = {"get_config_raw": get_config_raw, "get_satel_partitions": get_satel_partitions, "history": get_history()}
        return await handler(arguments or {}, **ctx)

    def _jsonrpc_result(req_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _jsonrpc_error(req_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    async def _handle_single(msg) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _jsonrpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Nieprawidłowe żądanie JSON-RPC")

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return _jsonrpc_result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _jsonrpc_result(req_id, {"tools": _tool_defs()})
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            logger.info("MCP tools/call: %s(%r)", tool_name, arguments)
            try:
                result = await _dispatch_tool_call(tool_name, arguments)
                return _jsonrpc_result(req_id, {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "isError": False,
                })
            except _ToolError as exc:
                logger.warning("MCP tools/call %s failed: %s", tool_name, exc)
                return _jsonrpc_result(req_id, {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                })
        if req_id is None:
            return None
        return _jsonrpc_error(req_id, -32601, f"Nieznana metoda: '{method}'")

    @app.route("/api/mcp", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_mcp():
        if not _get_mcp_section().get("enabled", False):
            return jsonify({"error": "Serwer MCP jest wyłączony (zakładka 🔌 Wtyczki -> Serwer MCP)"}), 404

        body = await request.get_json(silent=True)
        if body is None:
            return jsonify(_jsonrpc_error(None, -32700, "Nieprawidłowy JSON")), 400

        if isinstance(body, list):
            responses = [r for r in [await _handle_single(m) for m in body] if r is not None]
            if not responses:
                return "", 202
            return jsonify(responses)

        response = await _handle_single(body)
        if response is None:
            return "", 202
        return jsonify(response)

    register_settings_route(
        app, "mcp_server", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_mcp_section, save_section=_save_mcp_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
        default_enabled=False,
    )

    return META
