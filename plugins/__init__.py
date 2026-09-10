from __future__ import annotations

import importlib
import pathlib
from dataclasses import dataclass
from typing import Callable

from quart import Quart, jsonify, request

from bridge.logging_config import VALID_LEVELS

_PACKAGE_DIR = pathlib.Path(__file__).resolve().parent


@dataclass(frozen=True)
class PluginMeta:
    id: str
    name: str
    icon: str
    description: str
    config_url: str | None = None
    config_key: str | None = None
    toggleable: bool = True
    restart_required: bool = True
    has_log_level: bool = True
    has_poll_interval: bool = False
    min_poll_interval_seconds: int | None = None
    toggle_route: str = "toggle_prereq"
    describe_state: Callable[[dict], dict] | None = None
    category: str = "Inne"


PLUGIN_CATEGORIES = ["Ochrona", "Tuya", "Dom", "Inne"]


@dataclass(frozen=True)
class PluginLoadError:
    plugin_dir: str
    error: str


def register_settings_route(
    app: Quart, plugin_id: str, *, require_auth: Callable, get_auth_guard: Callable,
    get_section: Callable[[], dict], save_section: Callable[[dict], None],
    reload_bridge_config: Callable, enqueue_bridge_reload: Callable,
    supports_enabled: bool = True,
    default_enabled: bool = True,
    validate_enable: Callable[[], str | None] | None = None,
) -> None:

    @app.route(f"/api/plugins/{plugin_id}/settings", methods=["GET"],
               endpoint=f"plugin_{plugin_id}_settings_get")
    async def _settings_get():
        section = get_section() or {}
        result = {"log_level": section.get("log_level") or ""}
        if supports_enabled:
            result["enabled"] = bool(section.get("enabled", default_enabled))
        return jsonify(result)

    @app.route(f"/api/plugins/{plugin_id}/settings", methods=["POST"],
               endpoint=f"plugin_{plugin_id}_settings_post")
    @require_auth(get_guard=get_auth_guard)
    async def _settings_post():
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Nieprawidłowe dane"}), 400

        section = dict(get_section() or {})
        enabled_changed = False
        if supports_enabled and "enabled" in body:
            new_enabled = bool(body.get("enabled"))
            if new_enabled and validate_enable is not None:
                error = validate_enable()
                if error:
                    return jsonify({"error": error}), 400
            enabled_changed = bool(section.get("enabled", default_enabled)) != new_enabled
            section["enabled"] = new_enabled
        if "log_level" in body:
            level = (body.get("log_level") or "").strip().upper()
            if level and level not in VALID_LEVELS:
                return jsonify({"error": f"Nieprawidłowy poziom logowania: {level}"}), 400
            if level:
                section["log_level"] = level
            else:
                section.pop("log_level", None)

        save_section(section)
        reload_bridge_config()
        enqueue_bridge_reload()

        message = "Zapisano."
        if enabled_changed:
            message += " Włączenie/wyłączenie wymaga pełnego restartu procesu bridge'a."
        else:
            message += " Zmiany zadziałają w ciągu ok. 1-2s, bez restartu."
        return jsonify({"message": message})


def _iter_plugin_dir_names() -> list[str]:
    return sorted(
        entry.name for entry in _PACKAGE_DIR.iterdir()
        if entry.is_dir() and not entry.name.startswith("_") and (entry / "routes.py").is_file()
    )


def _discover_modules() -> tuple[list[tuple[str, object, list[PluginMeta]]], list[PluginLoadError]]:
    modules: list[tuple[str, object, list[PluginMeta]]] = []
    errors: list[PluginLoadError] = []
    for dir_name in _iter_plugin_dir_names():
        module_name = f"plugins.{dir_name}.routes"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(PluginLoadError(dir_name, f"błąd importu: {exc}"))
            continue

        if not callable(getattr(module, "register", None)):
            errors.append(PluginLoadError(dir_name, "brak funkcji register(app, **deps)"))
            continue

        raw_metas = getattr(module, "METAS", None)
        if raw_metas is None:
            single = getattr(module, "META", None)
            raw_metas = [single] if single is not None else []
        if not raw_metas or not all(isinstance(meta, PluginMeta) for meta in raw_metas):
            errors.append(PluginLoadError(dir_name, "brak poprawnego META/METAS (obiekt PluginMeta)"))
            continue

        modules.append((dir_name, module, list(raw_metas)))
    return modules, errors


def discover_plugin_metas() -> tuple[list[PluginMeta], list[PluginLoadError]]:
    modules, errors = _discover_modules()
    metas = [meta for _, _, module_metas in modules for meta in module_metas]
    return metas, errors


def register_all(app: Quart, **deps) -> tuple[list[PluginMeta], list[PluginLoadError]]:
    modules, errors = _discover_modules()
    metas: list[PluginMeta] = []
    for dir_name, module, module_metas in modules:
        try:
            module.register(app, **deps)
        except Exception as exc:
            errors.append(PluginLoadError(dir_name, f"błąd rejestracji: {exc}"))
            continue
        metas.extend(module_metas)
    return metas, errors
