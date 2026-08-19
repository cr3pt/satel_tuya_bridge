"""Wtyczka symulacji obecności - włącznik + poziom logowania mają teraz
własną, dedykowaną stronę (jak reszta wtyczek), ale lista urządzeń/reguł
zostaje tam, gdzie już jest - w zakładce 🎬 Automatyzacje (razem ze
scenami i regułami ruchu, bo dzielą ten sam edytor/dokument "automations").
Duplikowanie tamtego edytora tutaj nie miałoby sensu - ta strona tylko
linkuje i pokazuje krótkie podsumowanie. Patrz plugins/satel/routes.py po
ten sam wzorzec wstrzykiwania zależności."""
from __future__ import annotations

from flask import Flask, jsonify, render_template

from plugins import PluginMeta, register_settings_route

META = PluginMeta(
    id="presence_simulation",
    name="Symulacja obecności",
    icon="🎭",
    description="Losowe załączanie wybranych urządzeń Tuya, gdy nikogo nie ma w domu.",
    config_url="/plugins/presence_simulation",
)


def register(app: Flask, *, require_auth, get_auth_guard, get_config_store,
             reload_bridge_config, enqueue_bridge_reload, **_ignored) -> PluginMeta:
    """Rejestruje routing wtyczki symulacji obecności. `_ignored` połyka
    zależności przeznaczone dla INNYCH wtyczek - register_all() w
    plugins/__init__.py woła każdą wtyczkę z tym samym, wspólnym zestawem
    `deps`."""

    def _get_presence_section() -> dict:
        # UWAGA: presence_simulation żyje w dokumencie "automations", NIE
        # "config" (patrz bridge/config.py: _AUTOMATIONS_DEFAULTS) - inaczej
        # niż reszta wtyczek w tym pliku.
        return get_config_store().get("automations").get("presence_simulation", {}) or {}

    def _save_presence_section(section: dict) -> None:
        current = get_config_store().get("automations")
        current["presence_simulation"] = section
        get_config_store().set("automations", current)

    @app.route("/plugins/presence_simulation")
    def presence_simulation_plugin_page():
        return render_template("presence_simulation/index.html")

    @app.route("/api/plugins/presence_simulation/summary", methods=["GET"])
    def api_plugin_presence_simulation_summary_get():
        section = _get_presence_section()
        return jsonify({"devices": section.get("devices", [])})

    register_settings_route(
        app, "presence_simulation", require_auth=require_auth, get_auth_guard=get_auth_guard,
        get_section=_get_presence_section, save_section=_save_presence_section,
        reload_bridge_config=reload_bridge_config, enqueue_bridge_reload=enqueue_bridge_reload,
    )

    return META
