import asyncio

import pytest

from bridge.scenes import SceneRunner


SCENES_CFG = [
    {
        "name": "wykryto_intruza",
        "actions": [
            {"type": "tuya_on", "device": "gniazdko_salon"},
            {"type": "tuya_on", "device": "swiatlo_korytarz", "dimmer_level_percent": 100},
            {"type": "satel_output", "output_number": 1, "state": True},
            {"type": "delay", "seconds": 0.01},
            {"type": "satel_virtual_input", "virtual_input": "motion_hall_camera"},
        ],
    },
    {
        "name": "z_nieznana_akcja",
        "actions": [
            {"type": "cos_nieznanego"},
            {"type": "tuya_off", "device": "gniazdko_salon"},
        ],
    },
    {
        "name": "arm_disarm_test",
        "actions": [
            {"type": "satel_arm", "mode": 1},
            {"type": "satel_disarm"},
        ],
    },
    {
        "name": "arm_disarm_specific_partitions",
        "actions": [
            {"type": "satel_arm", "mode": 0, "partitions": [1, 2]},
            {"type": "satel_disarm", "partitions": [2]},
        ],
    },
    {
        "name": "scena_wylaczona",
        "enabled": False,
        "actions": [
            {"type": "tuya_on", "device": "gniazdko_salon"},
        ],
    },
]


@pytest.fixture
def runner(fake_tuya, fake_satel, history):
    fake_satel.virtual_inputs = {"motion_hall_camera": 17}
    return SceneRunner(SCENES_CFG, fake_tuya, fake_satel, history=history)


async def test_scene_executes_actions_in_order(runner, fake_tuya, fake_satel):
    await runner.run("wykryto_intruza")
    assert fake_tuya.calls == [
        ("on", "gniazdko_salon", None),
        ("on", "swiatlo_korytarz", 100),
    ]
    assert fake_satel.calls == [("output", 1, True), ("output", 17, True)]


async def test_scene_passes_scene_name_as_reason(runner, fake_tuya):
    await runner.run("wykryto_intruza")
    assert fake_tuya.reasons == ["scena 'wykryto_intruza'", "scena 'wykryto_intruza'"]


async def test_unknown_action_is_skipped_not_fatal(runner, fake_tuya):
    await runner.run("z_nieznana_akcja")
    assert fake_tuya.calls == [("off", "gniazdko_salon")]


async def test_unknown_scene_does_not_raise(runner):
    await runner.run("nieistniejaca_scena")


async def test_disabled_scene_does_not_run(runner, fake_tuya, history):
    await runner.run("scena_wylaczona")
    assert fake_tuya.calls == []
    assert history.get_status_value("automation:scena_wylaczona") is None


async def test_arm_disarm_actions(runner, fake_satel):
    await runner.run("arm_disarm_test")
    assert fake_satel.calls == [("arm", 1, None), ("disarm", None)]


async def test_scene_sets_running_status_during_execution_then_idle(runner, history):
    await runner.run("wykryto_intruza")
    assert history.get_status_value("automation:wykryto_intruza") == "bezczynna"


async def test_scene_status_is_running_while_delay_action_pending(runner, history):
    task = asyncio.create_task(runner.run("wykryto_intruza"))
    await asyncio.sleep(0)
    assert history.get_status_value("automation:wykryto_intruza") == "uruchomiona"
    await task
    assert history.get_status_value("automation:wykryto_intruza") == "bezczynna"


async def test_scene_status_returns_idle_even_after_action_error(runner, history):
    await runner.run("z_nieznana_akcja")
    assert history.get_status_value("automation:z_nieznana_akcja") == "bezczynna"


async def test_unknown_scene_does_not_write_any_automation_status(runner, history):
    await runner.run("nieistniejaca_scena")
    assert history.get_status_value("automation:nieistniejaca_scena") is None


async def test_scene_without_history_does_not_crash(fake_tuya, fake_satel):
    fake_satel.virtual_inputs = {"motion_hall_camera": 17}
    runner_no_history = SceneRunner(SCENES_CFG, fake_tuya, fake_satel, history=None)
    await runner_no_history.run("wykryto_intruza")


async def test_arm_disarm_passes_explicit_partitions(runner, fake_satel):
    await runner.run("arm_disarm_specific_partitions")
    assert fake_satel.calls == [("arm", 0, [1, 2]), ("disarm", [2])]


async def test_scene_logs_event_to_history(runner, history):
    await runner.run("wykryto_intruza")
    events = history.get_events("scene", 0, 9999999999)
    assert len(events) == 1
    assert events[0]["source"] == "wykryto_intruza"


async def test_unknown_virtual_input_is_skipped_not_fatal(runner, fake_satel, history):
    fake_satel.virtual_inputs = {}
    await runner.run("wykryto_intruza")
    assert fake_satel.calls == [("output", 1, True)]


async def test_failing_action_does_not_abort_scene(fake_satel, history):
    failing_tuya_cfg = [
        {
            "name": "test_scena",
            "actions": [
                {"type": "tuya_on", "device": "martwe_urzadzenie"},
                {"type": "satel_output", "output_number": 2, "state": True},
            ],
        }
    ]

    class FailingTuya:
        async def turn_on(self, device, dimmer_level_percent=None):
            raise ConnectionError("symulowana awaria")

    runner = SceneRunner(failing_tuya_cfg, FailingTuya(), fake_satel, history=history)
    await runner.run("test_scena")
    assert fake_satel.calls == [("output", 2, True)]


async def test_tuya_on_for_duration_reports_state_change_via_callback(fake_tuya, fake_satel, history):
    """Regresja: `tuya_on_for_duration` (np. "włącz światło na X" jako
    reakcja na inną automatyzację) wołał `tuya.turn_on_for_duration`
    bezpośrednio, z pominięciem `on_tuya_state` - inna reguła/flow nasłuchujące
    na stan TEGO urządzenia nie widziało włączenia od razu (patrz
    RulesEngine._apply_tuya_own_state)."""
    cfg = [{
        "name": "impuls",
        "actions": [{"type": "tuya_on_for_duration", "device": "gniazdko_salon", "duration_seconds": 600}],
    }]
    reported = []

    async def on_tuya_state(device, value):
        reported.append((device, value))

    runner = SceneRunner(cfg, fake_tuya, fake_satel, history=history, on_tuya_state=on_tuya_state)
    await runner.run("impuls")
    await asyncio.sleep(0)

    assert reported == [("gniazdko_salon", "włączone")]
