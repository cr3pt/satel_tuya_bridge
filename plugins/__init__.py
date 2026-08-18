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

from flask import Flask


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


def register_all(app: Flask, **deps) -> list[PluginMeta]:
    """Rejestruje routing wszystkich wtyczek z WŁASNĄ stroną konfiguracji na
    podanej aplikacji Flask i zwraca ich metadane (do zakładki Wtyczki).
    `deps` to współdzielony stan z dashboard/app.py (patrz docstring modułu
    wyżej) - każdy plugins/<id>/routes.py bierze z niego tylko to, czego
    faktycznie potrzebuje."""
    from plugins.satel import routes as satel_routes

    metas = []
    for routes_module in (satel_routes,):
        metas.append(routes_module.register(app, **deps))
    return metas
