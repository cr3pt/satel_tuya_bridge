import asyncio

import pytest

from bridge.boiler_automation import BoilerAutomation


class FakeSceneRunner:
    def __init__(self):
        self.calls: list[str] = []

    async def run(self, scene_name: str) -> None:
        self.calls.append(scene_name)


RULES = [
    {
        "name": "Ochrona przed mrozem",
        "sensor": "temp_zewnetrzna",
        "condition": "below",
        "threshold": -5,
        "scene": "ochrona_mroz",
        "cooldown_seconds": 0.2,
    },
    {
        "name": "Zbyt wysoka temp",
        "sensor": "temp_zasilania",
        "condition": "above",
        "threshold": 85,
        "scene": "przegrzanie",
        "cooldown_seconds": 0,
    },
]


@pytest.fixture
def scene_runner():
    return FakeSceneRunner()


@pytest.fixture
def automation(scene_runner):
    return BoilerAutomation(RULES, scene_runner)


async def test_threshold_breach_triggers_scene(automation, scene_runner):
    await automation.on_value("temp_zewnetrzna", -10)
    assert scene_runner.calls == ["ochrona_mroz"]


async def test_value_not_breaching_threshold_does_nothing(automation, scene_runner):
    await automation.on_value("temp_zewnetrzna", 5)
    assert scene_runner.calls == []


async def test_unrelated_sensor_does_nothing(automation, scene_runner):
    await automation.on_value("inny_czujnik", -100)
    assert scene_runner.calls == []


async def test_text_value_is_safely_ignored(automation, scene_runner):
    await automation.on_value("temp_zewnetrzna", "błąd_odczytu")
    assert scene_runner.calls == []


async def test_cooldown_suppresses_repeated_triggers(automation, scene_runner):
    await automation.on_value("temp_zewnetrzna", -10)
    await automation.on_value("temp_zewnetrzna", -12)
    assert scene_runner.calls == ["ochrona_mroz"]

    await asyncio.sleep(0.25)
    await automation.on_value("temp_zewnetrzna", -8)
    assert scene_runner.calls == ["ochrona_mroz", "ochrona_mroz"]


async def test_above_condition(automation, scene_runner):
    await automation.on_value("temp_zasilania", 90)
    assert scene_runner.calls == ["przegrzanie"]


async def test_multiple_rules_independent(automation, scene_runner):
    await automation.on_value("temp_zewnetrzna", -10)
    await automation.on_value("temp_zasilania", 90)
    assert scene_runner.calls == ["ochrona_mroz", "przegrzanie"]
