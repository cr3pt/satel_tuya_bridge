"""Testy dla bridge/tuya_dispatch.py - rozdziela sterowanie między
TuyaLocalController i TuyaCloudController po nazwie urządzenia, z
pierwszeństwem dla lokalnego."""
import pytest

from bridge.tuya_dispatch import TuyaController


class FakeSubController:
    def __init__(self, devices_cfg):
        self.devices_cfg = devices_cfg
        self.calls: list[tuple] = []

    async def turn_on(self, device_name, dimmer_level_percent=None):
        self.calls.append(("turn_on", device_name, dimmer_level_percent))

    async def turn_off(self, device_name):
        self.calls.append(("turn_off", device_name))

    async def get_status(self, device_name):
        self.calls.append(("get_status", device_name))
        return {"1": True}

    async def get_all_statuses(self):
        return {name: {"1": True} for name in self.devices_cfg}

    async def turn_on_for_duration(self, device_name, duration_seconds, dimmer_level_percent=None):
        self.calls.append(("turn_on_for_duration", device_name, duration_seconds, dimmer_level_percent))


@pytest.fixture
def local():
    return FakeSubController({"gniazdko_local": {"id": "l1"}})


@pytest.fixture
def cloud():
    return FakeSubController({"lampa_cloud": {"id": "c1"}})


@pytest.fixture
def dispatch(local, cloud):
    return TuyaController(local, cloud)


async def test_turn_on_routes_local_device_to_local(dispatch, local, cloud):
    await dispatch.turn_on("gniazdko_local")
    assert local.calls == [("turn_on", "gniazdko_local", None)]
    assert cloud.calls == []


async def test_turn_on_routes_cloud_only_device_to_cloud(dispatch, local, cloud):
    await dispatch.turn_on("lampa_cloud")
    assert cloud.calls == [("turn_on", "lampa_cloud", None)]
    assert local.calls == []


async def test_turn_off_routes_correctly(dispatch, local, cloud):
    await dispatch.turn_off("lampa_cloud")
    assert cloud.calls == [("turn_off", "lampa_cloud")]


async def test_unknown_device_raises_key_error(dispatch):
    with pytest.raises(KeyError):
        await dispatch.turn_on("nieznane_urzadzenie")


def test_local_takes_precedence_when_device_configured_in_both(local, cloud):
    """Dokładnie scenariusz ze zgłoszenia: urządzenie zaczyna jako
    chmurowe (brak local_key), a potem użytkownik konfiguruje je lokalnie
    pod TĄ SAMĄ nazwą - lokalne musi wygrać."""
    cloud.devices_cfg["gniazdko_local"] = {"id": "c2"}  # ta sama nazwa co w local
    dispatch = TuyaController(local, cloud)
    assert dispatch._resolve("gniazdko_local") is local


async def test_get_all_statuses_merges_both_controllers(dispatch):
    statuses = await dispatch.get_all_statuses()
    assert statuses == {"gniazdko_local": {"1": True}, "lampa_cloud": {"1": True}}


def test_devices_cfg_merges_both_with_local_precedence(local, cloud):
    cloud.devices_cfg["gniazdko_local"] = {"id": "stale_cloud_entry"}
    dispatch = TuyaController(local, cloud)
    assert dispatch.devices_cfg["gniazdko_local"] == {"id": "l1"}  # lokalne nadpisuje
    assert "lampa_cloud" in dispatch.devices_cfg


async def test_turn_on_for_duration_routes_correctly(dispatch, cloud):
    await dispatch.turn_on_for_duration("lampa_cloud", 30, dimmer_level_percent=None)
    assert cloud.calls == [("turn_on_for_duration", "lampa_cloud", 30, None)]
