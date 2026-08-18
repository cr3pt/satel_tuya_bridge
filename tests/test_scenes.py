"""Testy dla bridge/scenes.py - grupy akcji na wielu urządzeniach naraz."""
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
]


@pytest.fixture
def runner(fake_tuya, fake_satel, fake_virtual_inputs, history):
    return SceneRunner(SCENES_CFG, fake_tuya, fake_satel, fake_virtual_inputs, history=history)


async def test_scene_executes_actions_in_order(runner, fake_tuya, fake_satel, fake_virtual_inputs):
    await runner.run("wykryto_intruza")
    assert fake_tuya.calls == [
        ("on", "gniazdko_salon", None),
        ("on", "swiatlo_korytarz", 100),
    ]
    assert fake_satel.calls == [("output", 1, True)]
    assert fake_virtual_inputs.calls == [("trigger", "motion_hall_camera")]


async def test_unknown_action_is_skipped_not_fatal(runner, fake_tuya):
    await runner.run("z_nieznana_akcja")
    # akcja 'cos_nieznanego' pominięta, ale kolejna akcja w scenie i tak się wykonała
    assert fake_tuya.calls == [("off", "gniazdko_salon")]


async def test_unknown_scene_does_not_raise(runner):
    await runner.run("nieistniejaca_scena")  # nie powinno rzucić wyjątku


async def test_arm_disarm_actions(runner, fake_satel):
    await runner.run("arm_disarm_test")
    assert fake_satel.calls == [("arm", 1, None), ("disarm", None)]


async def test_arm_disarm_passes_explicit_partitions(runner, fake_satel):
    """Regresja: sceny wcześniej NIGDY nie przekazywały `partitions` do
    arm()/disarm(), więc zawsze dotykały tylko domyślnej (jedynej) strefy
    (SatelIntegraClient.partition) - nawet gdy instalacja miała kilka
    nazwanych stref skonfigurowanych. Patrz też komentarz w
    bridge/scenes.py."""
    await runner.run("arm_disarm_specific_partitions")
    assert fake_satel.calls == [("arm", 0, [1, 2]), ("disarm", [2])]


async def test_scene_logs_event_to_history(runner, history):
    await runner.run("wykryto_intruza")
    events = history.get_events("scene", 0, 9999999999)
    assert len(events) == 1
    assert events[0]["source"] == "wykryto_intruza"


async def test_failing_action_does_not_abort_scene(fake_satel, fake_virtual_inputs, history):
    """Jedna nieudana akcja (np. martwe urządzenie Tuya) nie może przerwać
    wykonywania reszty sceny."""
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

    runner = SceneRunner(failing_tuya_cfg, FailingTuya(), fake_satel, fake_virtual_inputs, history=history)
    await runner.run("test_scena")
    # mimo błędu w pierwszej akcji, druga (satel_output) powinna się wykonać
    assert fake_satel.calls == [("output", 2, True)]
