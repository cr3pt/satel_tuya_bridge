from __future__ import annotations

import datetime as dt
from typing import Callable

from .scheduling import is_within_window

_COMPOSITE_TYPES = ("and", "or", "not")
_LEAF_TYPES = ("state", "time_window")


def _num(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _numeric_compare(actual: object, threshold: object, op: Callable[[float, float], bool]) -> bool:
    a, b = _num(actual), _num(threshold)
    if a is None or b is None:
        return False
    return op(a, b)


def evaluate_condition(
    condition: dict | None,
    *,
    get_status_value: Callable[[str], str | None],
    location_cfg: dict | None = None,
    now: dt.datetime | None = None,
) -> bool:
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
        return not all(
            evaluate_condition(c, get_status_value=get_status_value, location_cfg=location_cfg, now=now)
            for c in condition.get("conditions", [])
        )
    if ctype == "state":
        actual = get_status_value(condition["key"])
        if "not_equals" in condition:
            return actual != condition["not_equals"]
        if "gt" in condition:
            return _numeric_compare(actual, condition["gt"], lambda a, b: a > b)
        if "lt" in condition:
            return _numeric_compare(actual, condition["lt"], lambda a, b: a < b)
        if "gte" in condition:
            return _numeric_compare(actual, condition["gte"], lambda a, b: a >= b)
        if "lte" in condition:
            return _numeric_compare(actual, condition["lte"], lambda a, b: a <= b)
        return actual == condition.get("equals")
    if ctype == "time_window":
        return is_within_window(
            {"start": condition["start"], "end": condition["end"]}, location_cfg, now,
        )
    raise ValueError(f"Nieznany typ warunku: {ctype!r}")
