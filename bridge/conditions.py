"""Zagnieżdżone warunki (and/or/not/state/time_window) dla reguł ruchu -
odpowiednik systemu warunków (`condition:`) z Home Assistant, okrojony do
tego, czego faktycznie potrzebuje ten bridge: porównanie aktualnego stanu
(dowolny klucz z tabeli `status` w HistoryStore - wejście/wyjście Satela,
urządzenie Tuya, czujnik kotła) i okno czasowe, złożone dowolnie głęboko
przez and/or/not.

Przykład (dokładnie taki kształt zapisuje /automations, patrz
dashboard/setup.py: _build_condition):
  {
    "type": "and",
    "conditions": [
      {"type": "state", "key": "satel_input:3", "equals": "naruszone"},
      {"type": "not", "conditions": [
        {"type": "time_window", "start": "22:00", "end": "06:00"}
      ]}
    ]
  }
czyli: "wejście 3 naruszone" ORAZ NIE "jest między 22:00 a 06:00".
"""
from __future__ import annotations

import datetime as dt
from typing import Callable

from .scheduling import is_within_window

_COMPOSITE_TYPES = ("and", "or", "not")
_LEAF_TYPES = ("state", "time_window")


def evaluate_condition(
    condition: dict | None,
    *,
    get_status_value: Callable[[str], str | None],
    location_cfg: dict | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Rekurencyjnie ewaluuje warunek. Brak warunku (None/{}) = zawsze
    spełniony - tak samo jak brak time_window w is_within_window, żeby
    reguły bez sekcji "condition" (czyli zdecydowana większość istniejących
    reguł w configach użytkowników) zachowywały się dokładnie tak jak
    wcześniej, bez żadnej zmiany zachowania."""
    if not condition:
        return True
    ctype = condition.get("type")

    if ctype == "and":
        return all(
            evaluate_condition(c, get_status_value=get_status_value, location_cfg=location_cfg, now=now)
            for c in condition.get("conditions", [])
        )
    if ctype == "or":
        return any(
            evaluate_condition(c, get_status_value=get_status_value, location_cfg=location_cfg, now=now)
            for c in condition.get("conditions", [])
        )
    if ctype == "not":
        # Tak jak w Home Assistant: "not" neguje ORAZ (and) swoich
        # warunków potomnych - lista jednoelementowa to zwykła negacja,
        # ale wieloelementowa pozwala napisać "not (A and B)" bez
        # zagnieżdżania dodatkowego "and" w środku.
        return not all(
            evaluate_condition(c, get_status_value=get_status_value, location_cfg=location_cfg, now=now)
            for c in condition.get("conditions", [])
        )
    if ctype == "state":
        actual = get_status_value(condition["key"])
        if "not_equals" in condition:
            return actual != condition["not_equals"]
        return actual == condition.get("equals")
    if ctype == "time_window":
        return is_within_window(
            {"start": condition["start"], "end": condition["end"]}, location_cfg, now,
        )
    raise ValueError(f"Nieznany typ warunku: {ctype!r}")
