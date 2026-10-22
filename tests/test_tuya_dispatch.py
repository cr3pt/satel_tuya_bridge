# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import pytest

from bridge.tuya_dispatch import TuyaController


class FakeSubController:
    def __init__(self, devices_cfg):
        self.devices_cfg = devices_cfg
        self.calls: list[tuple] = []
        self.on_state_change_callbacks: list = []

    async def turn_on(self, device_name, dimmer_level_percent=None, reason=None):
        self.calls.append(("turn_on", device_name, dimmer_level_percent))

    async def turn_off(self, device_name, reason=None):
        self.calls.append(("turn_off", device_name))

    async def toggle(self, device_name, reason=None):
        self.calls.append(("toggle", device_name))

    async def get_status(self, device_name):
        self.calls.append(("get_status", device_name))
        return {"1": True}

    async def get_all_statuses(self):
        return {name: {"1": True} for name in self.devices_cfg}

    async def turn_on_for_duration(self, device_name, duration_seconds, dimmer_level_percent=None, reason=None,
                                    on_state_change=None, still_valid=None):
        self.calls.append(("turn_on_for_duration", device_name, duration_seconds, dimmer_level_percent))
        self.on_state_change_callbacks.append(on_state_change)

    async def set_dp(self, device_name, dp_code, value, reason=None):
        self.calls.append(("set_dp", device_name, dp_code, value))


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


async def test_toggle_routes_correctly(dispatch, local, cloud):
    await dispatch.toggle("gniazdko_local")
    assert local.calls == [("toggle", "gniazdko_local")]
    assert cloud.calls == []


async def test_unknown_device_raises_key_error(dispatch):
    with pytest.raises(KeyError):
        await dispatch.turn_on("nieznane_urzadzenie")


def test_local_takes_precedence_when_device_configured_in_both(local, cloud):
    cloud.devices_cfg["gniazdko_local"] = {"id": "c2"}
    dispatch = TuyaController(local, cloud)
    assert dispatch._resolve("gniazdko_local") is local


async def test_get_all_statuses_merges_both_controllers(dispatch):
    statuses = await dispatch.get_all_statuses()
    assert statuses == {"gniazdko_local": {"1": True}, "lampa_cloud": {"1": True}}


def test_devices_cfg_merges_both_with_local_precedence(local, cloud):
    cloud.devices_cfg["gniazdko_local"] = {"id": "stale_cloud_entry"}
    dispatch = TuyaController(local, cloud)
    assert dispatch.devices_cfg["gniazdko_local"] == {"id": "l1"}
    assert "lampa_cloud" in dispatch.devices_cfg


async def test_turn_on_for_duration_routes_correctly(dispatch, cloud):
    await dispatch.turn_on_for_duration("lampa_cloud", 30, dimmer_level_percent=None)
    assert cloud.calls == [("turn_on_for_duration", "lampa_cloud", 30, None)]


async def test_turn_on_for_duration_forwards_on_state_change_callback(dispatch, cloud):
    received = []

    async def on_state_change(is_on):
        received.append(is_on)

    await dispatch.turn_on_for_duration("lampa_cloud", 30, on_state_change=on_state_change)
    assert cloud.on_state_change_callbacks == [on_state_change]


async def test_set_dp_routes_local_device_to_local(dispatch, local, cloud):
    await dispatch.set_dp("gniazdko_local", "manual_delay", "2")
    assert local.calls == [("set_dp", "gniazdko_local", "manual_delay", "2")]
    assert cloud.calls == []


async def test_set_dp_routes_cloud_only_device_to_cloud(dispatch, local, cloud):
    await dispatch.set_dp("lampa_cloud", "manual_delay", "2")
    assert cloud.calls == [("set_dp", "lampa_cloud", "manual_delay", "2")]
    assert local.calls == []
