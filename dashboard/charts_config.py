# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

"""Konfigurowalne wykresy użytkownika (dowolna liczba) - osobno dla strony
głównej (`dashboard.charts`) i dla zakładki Koszty (`energy.charts`)."""
from __future__ import annotations

import re

from quart import Quart, jsonify, request

MAX_CHARTS = 20
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_charts(raw) -> tuple[list[dict] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "charts: oczekiwano listy obiektów"
    if len(raw) > MAX_CHARTS:
        return None, f"Maksymalna liczba wykresów to {MAX_CHARTS}"
    cleaned: list[dict] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"Wykres #{idx + 1}: oczekiwano obiektu"
        title = str(item.get("title") or f"Wykres {idx + 1}").strip()[:80]
        sensors = item.get("sensors") or []
        if isinstance(sensors, str):
            sensors = [sensors]
        if not isinstance(sensors, list) or not all(isinstance(s, str) and s.strip() for s in sensors):
            return None, f"Wykres '{title}': 'sensors' musi być listą nazw czujników"
        chart_type = str(item.get("type") or "line").strip().lower()
        if chart_type not in ("line", "bar"):
            return None, f"Wykres '{title}': typ musi być 'line' lub 'bar'"
        agg = str(item.get("agg") or "avg").strip().lower()
        if agg not in ("avg", "sum", "last"):
            return None, f"Wykres '{title}': agregacja musi być 'avg', 'sum' lub 'last'"
        raw_opts = item.get("options") or {}
        if not isinstance(raw_opts, dict):
            return None, f"Wykres '{title}': 'options' musi być obiektem"
        options: dict[str, dict] = {}
        for name in [s.strip() for s in sensors]:
            o = raw_opts.get(name)
            if o is None:
                continue
            if not isinstance(o, dict):
                return None, f"Wykres '{title}': opcje czujnika '{name}' muszą być obiektem"
            axis = str(o.get("axis") or "left").strip().lower()
            if axis not in ("left", "right"):
                return None, f"Wykres '{title}': oś musi być 'left' lub 'right'"
            color = str(o.get("color") or "").strip()
            if color and not _COLOR_RE.match(color):
                return None, f"Wykres '{title}': kolor musi mieć format #rrggbb"
            if axis != "left" or color:
                options[name] = {"axis": axis, "color": color}
        try:
            hours = float(item.get("hours", 24))
        except (TypeError, ValueError):
            return None, f"Wykres '{title}': 'hours' musi być liczbą"
        if not (0.1 <= hours <= 24 * 366):
            return None, f"Wykres '{title}': 'hours' poza zakresem 0.1 - 8784"
        chart_id = str(item.get("id") or f"chart_{idx + 1}").strip()[:40] or f"chart_{idx + 1}"
        # Unikalne id - edycja/usuwanie w UI wskazuje wykres po id.
        while chart_id in seen_ids:
            chart_id = f"{chart_id[:34]}_{idx + 1}"
        seen_ids.add(chart_id)
        cleaned.append({
            "id": chart_id,
            "title": title,
            "sensors": [s.strip() for s in sensors],
            "type": chart_type,
            "hours": hours,
            "agg": agg,
            "options": options,
        })
    return cleaned, None


def register_dashboard_charts_routes(app: Quart, *, require_auth, get_auth_guard, get_config_store) -> None:
    @app.route("/api/dashboard/charts", methods=["GET"])
    async def api_dashboard_charts_get():
        cfg = get_config_store().get("config")
        charts = (cfg.get("dashboard", {}) or {}).get("charts", []) or []
        return jsonify({"charts": charts})

    @app.route("/api/dashboard/charts", methods=["POST"])
    @require_auth(get_guard=get_auth_guard)
    async def api_dashboard_charts_post():
        body = await request.get_json(silent=True)
        raw_charts = body.get("charts") if isinstance(body, dict) else body
        charts, error = validate_charts(raw_charts)
        if error:
            return jsonify({"error": error}), 400
        current = get_config_store().get("config")
        dash = dict(current.get("dashboard", {}) or {})
        dash["charts"] = charts
        current["dashboard"] = dash
        get_config_store().set("config", current)
        return jsonify({"charts": charts, "message": "Zapisano wykresy strony głównej."})
