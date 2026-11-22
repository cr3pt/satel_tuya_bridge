# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Przycisk "Wyczyść config" na stronie Wtyczki - usuwa zapisaną sekcję konfiguracji
nieaktywnej wtyczki. Aktywnej wtyczki ani wtyczek z sekcją współdzieloną (połączenia,
automatyzacje) nie dotyka."""
from __future__ import annotations

from typing import Callable

from quart import Quart, jsonify


def is_clearable_meta(meta) -> bool:
    route = getattr(meta, "toggle_route", "") or ""
    return bool(
        getattr(meta, "config_key", None) and getattr(meta, "toggleable", False)
        and "connections" not in route and route != "automations"
    )


def plugin_is_inactive(meta, current: dict) -> bool:
    """Nieaktywna = flaga `enabled` jest jawnie wyłączona ORAZ describe_state nie widzi jej jako działającej."""
    section = current.get(meta.config_key)
    if not isinstance(section, dict) or "enabled" not in section or section.get("enabled"):
        return False
    describe = getattr(meta, "describe_state", None)
    if describe is not None:
        try:
            if (describe(current) or {}).get("enabled"):
                return False
        except Exception:
            return False
    return True


def clearable_plugin_ids(metas, current: dict) -> list[str]:
    return [
        m.id for m in metas
        if is_clearable_meta(m) and isinstance(current.get(m.config_key), dict)
        and current[m.config_key] and plugin_is_inactive(m, current)
    ]


def register_plugin_clear_config_routes(
    app: Quart, *, metas, require_auth: Callable, get_auth_guard: Callable,
    get_config_store: Callable, reload_bridge_config: Callable, enqueue_bridge_reload: Callable,
    get_history: Callable,
) -> None:
    metas_by_id = {m.id: m for m in metas}

    @app.route("/api/plugins/clearable", methods=["GET"], endpoint="plugins_clearable_get")
    async def _clearable_get():
        current = get_config_store().get("config") or {}
        return jsonify(clearable_plugin_ids(metas_by_id.values(), current))

    @app.route("/api/plugins/<plugin_id>/clear_config", methods=["POST"], endpoint="plugins_clear_config_post")
    @require_auth(get_guard=get_auth_guard)
    async def _clear_config_post(plugin_id: str):
        meta = metas_by_id.get(plugin_id)
        if meta is None:
            return jsonify({"error": f"Nieznana wtyczka: {plugin_id}"}), 404
        if not is_clearable_meta(meta):
            return jsonify({"error": f"Wtyczki '{plugin_id}' nie można wyczyścić tym przyciskiem"}), 400
        store = get_config_store()
        current = store.get("config") or {}
        if not isinstance(current.get(meta.config_key), dict) or not current[meta.config_key]:
            return jsonify({"error": "Wtyczka nie ma zapisanej konfiguracji"}), 404
        if not plugin_is_inactive(meta, current):
            return jsonify({"error": "Najpierw wyłącz wtyczkę i zapisz zmiany - aktywnej konfiguracji nie można usunąć"}), 409
        current.pop(meta.config_key)
        store.set("config", current)
        reload_bridge_config()
        enqueue_bridge_reload()
        get_history().write_event(
            "system", source="dashboard",
            detail={"message": f"Wyczyszczono konfigurację wtyczki {plugin_id}"},
        )
        return jsonify({"cleared": plugin_id, "message": f"Usunięto konfigurację wtyczki {meta.name}."})
