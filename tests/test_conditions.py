"""Testy bridge/conditions.py: evaluate_condition (and/or/not/state/
time_window) - logika czysta, bez RulesEngine/HistoryStore (patrz
tests/test_rules_engine.py: TestMotionRuleCondition/TestMotionRuleChaining
dla testów integracyjnych przez pełną ścieżkę reguł ruchu)."""
import datetime as dt

import pytest

from bridge.conditions import evaluate_condition


def _status(values: dict):
    return lambda key: values.get(key)


class TestNoCondition:
    def test_none_is_always_true(self):
        assert evaluate_condition(None, get_status_value=_status({})) is True

    def test_empty_dict_is_always_true(self):
        assert evaluate_condition({}, get_status_value=_status({})) is True


class TestStateLeaf:
    def test_equals_true(self):
        cond = {"type": "state", "key": "satel_input:3", "equals": "naruszone"}
        assert evaluate_condition(cond, get_status_value=_status({"satel_input:3": "naruszone"})) is True

    def test_equals_false(self):
        cond = {"type": "state", "key": "satel_input:3", "equals": "naruszone"}
        assert evaluate_condition(cond, get_status_value=_status({"satel_input:3": "OK"})) is False

    def test_unknown_key_never_matches_equals(self):
        cond = {"type": "state", "key": "brak_klucza", "equals": "x"}
        assert evaluate_condition(cond, get_status_value=_status({})) is False

    def test_not_equals_true(self):
        cond = {"type": "state", "key": "k", "not_equals": "x"}
        assert evaluate_condition(cond, get_status_value=_status({"k": "y"})) is True

    def test_not_equals_false(self):
        cond = {"type": "state", "key": "k", "not_equals": "x"}
        assert evaluate_condition(cond, get_status_value=_status({"k": "x"})) is False


class TestTimeWindowLeaf:
    def test_inside_window(self):
        cond = {"type": "time_window", "start": "08:00", "end": "09:00"}
        now = dt.datetime(2026, 1, 15, 8, 30)
        assert evaluate_condition(cond, get_status_value=_status({}), now=now) is True

    def test_outside_window(self):
        cond = {"type": "time_window", "start": "08:00", "end": "09:00"}
        now = dt.datetime(2026, 1, 15, 20, 0)
        assert evaluate_condition(cond, get_status_value=_status({}), now=now) is False


class TestComposite:
    def test_and_all_true(self):
        cond = {"type": "and", "conditions": [
            {"type": "state", "key": "a", "equals": "1"},
            {"type": "state", "key": "b", "equals": "2"},
        ]}
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "2"})) is True

    def test_and_one_false(self):
        cond = {"type": "and", "conditions": [
            {"type": "state", "key": "a", "equals": "1"},
            {"type": "state", "key": "b", "equals": "WRONG"},
        ]}
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "2"})) is False

    def test_or_one_true(self):
        cond = {"type": "or", "conditions": [
            {"type": "state", "key": "a", "equals": "WRONG"},
            {"type": "state", "key": "b", "equals": "2"},
        ]}
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "2"})) is True

    def test_or_all_false(self):
        cond = {"type": "or", "conditions": [
            {"type": "state", "key": "a", "equals": "WRONG"},
            {"type": "state", "key": "b", "equals": "WRONG"},
        ]}
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "2"})) is False

    def test_not_negates_single(self):
        cond = {"type": "not", "conditions": [{"type": "state", "key": "a", "equals": "1"}]}
        assert evaluate_condition(cond, get_status_value=_status({"a": "1"})) is False
        assert evaluate_condition(cond, get_status_value=_status({"a": "2"})) is True

    def test_not_negates_and_of_multiple(self):
        # not [(A and B)] - fałszywe TYLKO gdy A i B są prawdziwe naraz
        cond = {"type": "not", "conditions": [
            {"type": "state", "key": "a", "equals": "1"},
            {"type": "state", "key": "b", "equals": "2"},
        ]}
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "2"})) is False
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "WRONG"})) is True

    def test_deeply_nested(self):
        cond = {
            "type": "and",
            "conditions": [
                {"type": "state", "key": "a", "equals": "1"},
                {"type": "or", "conditions": [
                    {"type": "state", "key": "b", "equals": "2"},
                    {"type": "not", "conditions": [{"type": "state", "key": "c", "equals": "3"}]},
                ]},
            ],
        }
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "WRONG", "c": "WRONG"})) is True
        assert evaluate_condition(cond, get_status_value=_status({"a": "1", "b": "WRONG", "c": "3"})) is False

    def test_empty_conditions_list_and_is_vacuously_true(self):
        assert evaluate_condition({"type": "and", "conditions": []}, get_status_value=_status({})) is True

    def test_empty_conditions_list_or_is_vacuously_false(self):
        assert evaluate_condition({"type": "or", "conditions": []}, get_status_value=_status({})) is False


class TestUnknownType:
    def test_raises_value_error(self):
        with pytest.raises(ValueError):
            evaluate_condition({"type": "bogus"}, get_status_value=_status({}))
