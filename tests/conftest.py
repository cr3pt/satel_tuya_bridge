"""Wspólne fixtures pytest dla całego zestawu testów."""
from __future__ import annotations

import sys
from pathlib import Path

# Pozwala uruchamiać `pytest` z katalogu głównego repo bez instalacji pakietu
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from bridge.history_store import HistoryStore


@pytest.fixture
def history(tmp_path) -> HistoryStore:
    """Świeża baza SQLite w katalogu tymczasowym pytest - każdy test dostaje
    własny, izolowany plik (tmp_path jest unikalny per test)."""
    return HistoryStore(str(tmp_path / "test_history.db"))


class FakeTuya:
    """Fałszywy TuyaLocalController - zapisuje wywołania zamiast łączyć się
    z prawdziwymi urządzeniami po sieci."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_devices: set[str] = set()  # symulacja niedostępnych urządzeń

    async def turn_on(self, device, dimmer_level_percent=None):
        if device in self.fail_devices:
            raise ConnectionError(f"symulowana awaria: {device}")
        self.calls.append(("on", device, dimmer_level_percent))

    async def turn_off(self, device):
        if device in self.fail_devices:
            raise ConnectionError(f"symulowana awaria: {device}")
        self.calls.append(("off", device))

    async def turn_on_for_duration(self, device, duration, dimmer_level_percent=None):
        self.calls.append(("on_for", device, duration, dimmer_level_percent))

    async def get_status(self, device):
        return {"1": device not in self.fail_devices}

    async def get_all_statuses(self):
        return {}


class FakeSatel:
    """Fałszywy SatelIntegraClient - zapisuje wywołania zamiast łączyć się
    z prawdziwą centralą."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def set_output(self, number, state):
        self.calls.append(("output", number, state))

    async def arm(self, mode=0, partitions=None):
        self.calls.append(("arm", mode, partitions))

    async def disarm(self, partitions=None):
        self.calls.append(("disarm", partitions))


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
