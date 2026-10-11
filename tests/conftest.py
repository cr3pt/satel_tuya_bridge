# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from bridge.history_store import HistoryStore


@pytest.fixture
def history(tmp_path) -> HistoryStore:
    return HistoryStore(str(tmp_path / "test_history.db"))


class FakeTuya:

    def __init__(self):
        self.calls: list[tuple] = []
        self.reasons: list = []
        self.fail_devices: set[str] = set()
        self.devices_cfg: dict = {}

    async def turn_on(self, device, dimmer_level_percent=None, reason=None):
        if device in self.fail_devices:
            raise ConnectionError(f"symulowana awaria: {device}")
        self.calls.append(("on", device, dimmer_level_percent))
        self.reasons.append(reason)

    async def turn_off(self, device, reason=None):
        if device in self.fail_devices:
            raise ConnectionError(f"symulowana awaria: {device}")
        self.calls.append(("off", device))
        self.reasons.append(reason)

    async def toggle(self, device, reason=None):
        if device in self.fail_devices:
            raise ConnectionError(f"symulowana awaria: {device}")
        self.calls.append(("toggle", device))
        self.reasons.append(reason)

    async def turn_on_for_duration(self, device, duration, dimmer_level_percent=None, reason=None,
                                    on_state_change=None):
        self.calls.append(("on_for", device, duration, dimmer_level_percent))
        self.reasons.append(reason)
        if on_state_change:
            await on_state_change(True)

    async def get_status(self, device):
        return {"1": device not in self.fail_devices}

    async def get_all_statuses(self):
        return {name: await self.get_status(name) for name in self.devices_cfg}

    async def set_dp(self, device, dp_code, value, reason=None):
        self.calls.append(("set_dp", device, dp_code, value))
        self.reasons.append(reason)


class FakeSatel:

    def __init__(self):
        self.calls: list[tuple] = []
        self.monitored_zones: dict = {}
        self.monitored_outputs: dict = {}
        self.virtual_inputs: dict = {}
        self.armed_partition_numbers: set[int] = set()
        self.triggered_partition_numbers: set[int] = set()
        self.violated_outputs: list[int] = []
        self._zone_cb = None
        self._output_cb = None
        self._alarm_status_cb = None

    async def set_output(self, number, state):
        self.calls.append(("output", number, state))
        if state:
            if number not in self.violated_outputs:
                self.violated_outputs.append(number)
        elif number in self.violated_outputs:
            self.violated_outputs.remove(number)

    async def arm(self, mode=0, partitions=None):
        self.calls.append(("arm", mode, partitions))

    async def disarm(self, partitions=None):
        self.calls.append(("disarm", partitions))

    def register_zone_callback(self, callback):
        self._zone_cb = callback

    def register_output_callback(self, callback):
        self._output_cb = callback

    def register_alarm_status_callback(self, callback):
        self._alarm_status_cb = callback

    async def run_forever(self):
        await asyncio.sleep(999)


class FakeVirtualInputs:

    def __init__(self):
        self.calls: list[tuple] = []

    async def trigger(self, name, retries=1):
        self.calls.append(("trigger", name))


@pytest.fixture
def fake_tuya() -> FakeTuya:
    return FakeTuya()


@pytest.fixture
def fake_satel() -> FakeSatel:
    return FakeSatel()


@pytest.fixture
def fake_virtual_inputs() -> FakeVirtualInputs:
    return FakeVirtualInputs()


@pytest.fixture
def warsaw_location() -> dict:
    return {"latitude": 52.23, "longitude": 21.01}
