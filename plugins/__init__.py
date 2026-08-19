"""Wtyczki - jeden podkatalog na integrację (plugins/<id>/), każda ze
swoim routingiem (konfiguracja + skanowanie specyficzne dla tego systemu)
i własnym szablonem HTML (plugins/<id>/templates/<id>/index.html).

Rejestracja jest jawna (patrz register_all() niżej), nie przez
auto-discovery skanujące katalog - to repo ma stałą, znaną listę integracji
(nie jest to ogólny framework wtyczek osób trzecich), więc jawna lista jest
prostsza do prześledzenia niż magia importu wszystkiego z katalogu.

WAŻNE o imporcie z dashboard/app.py: każdy plugins/<id>/routes.py
eksportuje `register(app, **deps)`, które PRZYJMUJE potrzebny stan
(require_auth, ścieżki plików, referencję do `history` itd.) jako
JAWNE argumenty zamiast importować `dashboard.app` samemu. Powód:
`python -m dashboard.app` uruchamia ten plik jako `__main__`, więc gdyby
plugins/<id>/routes.py zrobiło `import dashboard.app`, dostałoby DRUGĄ,
osobną kopię tego modułu w sys.modules (pod inną nazwą niż "__main__") -
z własnymi, nigdy nie zainicjowanymi zmiennymi globalnymi (ten sam problem,
który dashboard/auth.py:require_auth() już obchodzi przez fn.__globals__).
Wstrzykiwanie zależności zamiast importu omija to ryzyko całkowicie."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from flask import Flask, jsonify, request

from bridge.logging_config import VALID_LEVELS


@dataclass(frozen=True)
class PluginMeta:
    """Metadane do zakładki 🧩 Wtyczki - `config_url` to link 'Konfiguruj'
    do własnej strony wtyczki (None, jeśli integracja nie ma jeszcze
    własnej strony - wtedy Wtyczki kierują do 🔌 Połączenia/🎬 Automatyzacje
    jak dotychczas)."""
    id: str
    name: str
    icon: str
    description: str
    config_url: str | None = None


def register_settings_route(
    app: Flask, plugin_id: str, *, require_auth: Callable, get_auth_guard: Callable,
    get_section: Callable[[], dict], save_section: Callable[[dict], None],
    reload_bridge_config: Callable, enqueue_bridge_reload: Callable,
    supports_enabled: bool = True,
    validate_enable: Callable[[], str | None] | None = None,
) -> None:
    """Rejestruje GET/POST /api/plugins/<id>/settings - wspólny wzorzec
    "włącznik + poziom logowania zdarzeń", ten sam dla każdej wtyczki (patrz
    bridge/logging_config.py po globalny/per-wtyczka poziom logowania).
    `get_section`/`save_section` izolują to, GDZIE faktycznie żyje sekcja
    configu danej wtyczki (różne dokumenty ConfigStore - "config" vs
    "automations" dla presence_simulation - i różne zagnieżdżenie, np.
    tuya_cloud to config["tuya"]["cloud"]) - ten kod zna tylko dwa wspólne
    pola: "enabled" i "log_level". Włączenie/wyłączenie wymaga pełnego
    restartu procesu bridge'a (patrz rules_engine.py: start()), poziom
    logowania działa na żywo przez zwykłe reload_config().

    `validate_enable` - opcjonalny hak dla wtyczek, których "enabled" nie
    ma sensu bez wcześniej zapisanych innych pól (np. UniFi Protect bez
    hosta kontrolera) - zwraca komunikat błędu (odrzuca żądanie) albo
    `None` (zezwala). Wywoływany TYLKO gdy żądanie faktycznie włącza
    wtyczkę (nie przy wyłączaniu/samym poziomie logowania)."""

    @app.route(f"/api/plugins/{plugin_id}/settings", methods=["GET"],
               endpoint=f"plugin_{plugin_id}_settings_get")
    def _settings_get():
        # Publiczny, tak jak GET /api/plugins (przegląd Wtyczek) - "enabled"/
        # "log_level" to nie sekrety, a strona wtyczki musi dać się otworzyć
        # i odczytać bez logowania (jak reszta jej zawartości), zapis wciąż
        # wymaga hasła (patrz _settings_post niżej).
        section = get_section() or {}
        result = {"log_level": section.get("log_level") or ""}
        if supports_enabled:
            result["enabled"] = bool(section.get("enabled", True))
        return jsonify(result)

    @app.route(f"/api/plugins/{plugin_id}/settings", methods=["POST"],
               endpoint=f"plugin_{plugin_id}_settings_post")
    @require_auth(get_guard=get_auth_guard)
    def _settings_post():
        body = request.get_json(silent=True)
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
            enabled_changed = bool(section.get("enabled", True)) != new_enabled
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


def register_all(app: Flask, **deps) -> list[PluginMeta]:
    """Rejestruje routing wszystkich wtyczek z WŁASNĄ stroną konfiguracji na
    podanej aplikacji Flask i zwraca ich metadane (do zakładki Wtyczki).
    `deps` to współdzielony stan z dashboard/app.py (patrz docstring modułu
    wyżej) - każdy plugins/<id>/routes.py bierze z niego tylko to, czego
    faktycznie potrzebuje."""
    from plugins.satel import routes as satel_routes
    from plugins.tuya_local import routes as tuya_routes
    from plugins.ebus import routes as ebus_routes
    from plugins.tuya_cloud import routes as tuya_cloud_routes
    from plugins.home_assistant import routes as home_assistant_routes
    from plugins.mqtt_publish import routes as mqtt_publish_routes
    from plugins.unifi_protect import routes as unifi_protect_routes
    from plugins.notifications import routes as notifications_routes
    from plugins.presence_simulation import routes as presence_simulation_routes

    metas = []
    for routes_module in (
        satel_routes, tuya_routes, ebus_routes, tuya_cloud_routes, home_assistant_routes,
        mqtt_publish_routes, unifi_protect_routes, notifications_routes, presence_simulation_routes,
    ):
        metas.append(routes_module.register(app, **deps))
    return metas
