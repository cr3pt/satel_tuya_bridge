"""Testy dla bridge/satel_client.py - głównie test_connection() (podpowiedź
"Testuj połączenie" w kreatorze/panelu), bez prawdziwej centrali Satel."""
import asyncio

import pytest

from bridge.satel_client import SatelIntegraClient
from bridge.satel_client import test_connection as satel_test_connection
# ^ import aliasu jest CELOWY, nie kosmetyczny - pytest zbiera każdą funkcję
# o nazwie pasującej do "test_*" widoczną w module testowym, więc zwykłe
# "from ... import test_connection" sprawiłoby, że pytest próbowałby
# uruchomić samą funkcję produkcyjną jako (bezargumentowy) test i wywalał
# się błędem "fixture 'host' not found".


class FakeAsyncSatel:
    def __init__(self, host, port, loop, connect_result=True, hang=False):
        self._host = host
        self._port = port
        self._writer = None
        self._connect_result = connect_result
        self._hang = hang

    async def connect(self):
        if self._hang:
            await asyncio.sleep(999)
        return self._connect_result


def _patch_async_satel(monkeypatch, **kwargs):
    import satel_integra.satel_integra as satel_module

    monkeypatch.setattr(
        satel_module, "AsyncSatel",
        lambda host, port, loop: FakeAsyncSatel(host, port, loop, **kwargs),
    )


async def test_succeeds_silently_when_connect_returns_true(monkeypatch):
    _patch_async_satel(monkeypatch, connect_result=True)
    await satel_test_connection("127.0.0.1", 7094)  # nie powinno rzucić wyjątku


async def test_raises_connection_error_when_connect_returns_false(monkeypatch):
    _patch_async_satel(monkeypatch, connect_result=False)
    with pytest.raises(ConnectionError, match="127.0.0.1"):
        await satel_test_connection("127.0.0.1", 7094)


async def test_raises_connection_error_on_timeout(monkeypatch):
    _patch_async_satel(monkeypatch, hang=True)
    with pytest.raises(ConnectionError, match="Przekroczono czas"):
        await satel_test_connection("127.0.0.1", 7094, timeout_seconds=0.05)


async def test_closes_writer_if_present(monkeypatch):
    closed = {}

    class WithWriter(FakeAsyncSatel):
        async def connect(self):
            class FakeWriter:
                def close(self):
                    closed["called"] = True
            self._writer = FakeWriter()
            return True

    import satel_integra.satel_integra as satel_module
    monkeypatch.setattr(satel_module, "AsyncSatel", lambda host, port, loop: WithWriter(host, port, loop))

    await satel_test_connection("127.0.0.1", 7094)
    assert closed.get("called") is True


class FakeAsyncSatelCapture:
    """Do testu SatelIntegraClient.connect() - przechwytuje argumenty
    monitored_zones/monitored_outputs, żeby sprawdzić dokładnie CO
    dostaje AsyncSatel (a nie tylko czy connect() się powiodło)."""

    def __init__(self, host, port, loop, monitored_zones=None, monitored_outputs=None):
        self.host = host
        self.port = port
        self.monitored_zones = monitored_zones
        self.monitored_outputs = monitored_outputs
        self.closed = True

    async def connect(self):
        self.closed = False
        return True


async def test_connect_passes_empty_lists_not_none_when_no_monitored_items(monkeypatch):
    """Regresja dla zgłoszonego z prawdziwej centrali błędu 'NoneType' object
    is not iterable: biblioteka satel_integra bezwarunkowo iteruje
    `for zone in self._monitored_zones` przy każdym zdarzeniu naruszenia
    strefy (id 0x00, patrz satel_integra.py:_zone_violated) - pusta lista
    jest dla niej bezpieczna, ale None wywala TypeError w momencie
    pierwszego zdarzenia z centrali. SatelIntegraClient.connect() musi więc
    przekazywać [] (nie None) gdy nie skonfigurowano żadnych stref/wyjść."""
    captured = {}

    def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
        captured["zones"] = monitored_zones
        captured["outputs"] = monitored_outputs
        return FakeAsyncSatelCapture(host, port, loop, monitored_zones, monitored_outputs)

    import satel_integra.satel_integra as satel_module
    monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

    client = SatelIntegraClient("127.0.0.1", 7094, "key", monitored_zones={}, monitored_outputs={})
    await client.connect()

    assert captured["zones"] == []
    assert captured["outputs"] == []


async def test_connect_passes_configured_monitored_zones_and_outputs(monkeypatch):
    captured = {}

    def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
        captured["zones"] = monitored_zones
        captured["outputs"] = monitored_outputs
        return FakeAsyncSatelCapture(host, port, loop, monitored_zones, monitored_outputs)

    import satel_integra.satel_integra as satel_module
    monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

    client = SatelIntegraClient(
        "127.0.0.1", 7094, "key",
        monitored_zones={"Hol": 12}, monitored_outputs={"Brama": 3},
    )
    await client.connect()

    assert captured["zones"] == [12]
    assert captured["outputs"] == [3]
