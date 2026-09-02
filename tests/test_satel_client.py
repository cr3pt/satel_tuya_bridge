import asyncio
import contextlib
import time

import pytest

from bridge.satel_client import (
    SatelIntegraClient, SatelVirtualInputTrigger, _wait_for_command_result,
    discover_active_zones_and_outputs, BackgroundZoneOutputScan,
)
from bridge.satel_client import test_connection as satel_test_connection
from bridge.satel_client import test_output as satel_test_output
from bridge.satel_client import test_virtual_input_http as satel_test_virtual_input_http


class _FakeResult:

    def __init__(self, msg_data: bytearray):
        self.msg_data = msg_data


def _ok_result() -> _FakeResult:
    return _FakeResult(bytearray([0x00]))


def _error_result(error_byte: int) -> _FakeResult:
    return _FakeResult(bytearray([error_byte]))


class FakeAsyncSatel:

    def __init__(self, host, port, monitored_zones=None, monitored_outputs=None,
                 partitions=None, integration_key=None,
                 connect_result=True, connect_exception=None, hang_connect=False,
                 confirm_response=None, hang_confirm=False):
        self.host = host
        self.port = port
        self.monitored_zones = list(monitored_zones or [])
        self.monitored_outputs = list(monitored_outputs or [])
        self.encryption_key = integration_key
        self._connect_result = connect_result
        self._connect_exception = connect_exception
        self._hang_connect = hang_connect
        self._confirm_response = confirm_response if confirm_response is not None else _ok_result()
        self._hang_confirm = hang_confirm

        self.violated_zones: list[int] = []
        self.violated_outputs: list[int] = []
        self.partition_states: dict = {}

        self._stopped = False
        self.closed_called = False
        self.start_called = False
        self.start_enable_monitoring = None

        self._alarm_status_callback = None
        self._zone_changed_callback = None
        self._output_changed_callback = None

        self.sent_queries: list = []

    async def connect(self, verify_connection=True, *, raise_exceptions=False):
        if self._hang_connect:
            await asyncio.sleep(999)
        if self._connect_exception is not None:
            if raise_exceptions:
                raise self._connect_exception
            return False
        return self._connect_result

    def register_callbacks(self, alarm_status_callback=None, zone_changed_callback=None,
                            output_changed_callback=None):
        if alarm_status_callback:
            self._alarm_status_callback = alarm_status_callback
        if zone_changed_callback:
            self._zone_changed_callback = zone_changed_callback
        if output_changed_callback:
            self._output_changed_callback = output_changed_callback

    async def start(self, enable_monitoring=True):
        self.start_called = True
        self.start_enable_monitoring = enable_monitoring

    async def close(self):
        self.closed_called = True
        self._stopped = True

    @property
    def stopped(self):
        return self._stopped

    async def _send_data(self, msg):
        self.sent_queries.append(msg)

    async def _send_data_and_wait(self, msg):
        self.sent_queries.append(msg)
        if self._hang_confirm:
            await asyncio.sleep(999)
        return self._confirm_response

    async def _reading_loop(self):
        await asyncio.sleep(999)


def _patch_async_satel(monkeypatch, factory=None, **behavior):
    cls = factory or FakeAsyncSatel
    fakes: list = []

    def fake_ctor(host, port, **prod_kwargs):
        fake = cls(host, port, **prod_kwargs, **behavior)
        fakes.append(fake)
        return fake

    monkeypatch.setattr("satel_integra.AsyncSatel", fake_ctor)
    return fakes


class TestConnectionTest:

    async def test_succeeds_silently_when_connect_returns_true(self, monkeypatch):
        _patch_async_satel(monkeypatch, connect_result=True)
        await satel_test_connection("127.0.0.1", 7094)

    async def test_raises_connection_error_when_connect_fails(self, monkeypatch):
        from satel_integra.exceptions import SatelConnectFailedError
        _patch_async_satel(monkeypatch, connect_exception=SatelConnectFailedError("boom"))
        with pytest.raises(ConnectionError, match="127.0.0.1"):
            await satel_test_connection("127.0.0.1", 7094)

    async def test_raises_connection_error_when_panel_busy(self, monkeypatch):
        from satel_integra.exceptions import SatelPanelBusyError
        _patch_async_satel(monkeypatch, connect_exception=SatelPanelBusyError("busy"))
        with pytest.raises(ConnectionError, match="zajęta"):
            await satel_test_connection("127.0.0.1", 7094)

    async def test_raises_connection_error_on_timeout(self, monkeypatch):
        _patch_async_satel(monkeypatch, hang_connect=True)
        with pytest.raises(ConnectionError, match="Przekroczono czas"):
            await satel_test_connection("127.0.0.1", 7094, timeout_seconds=0.05)

    async def test_closes_client_after_connect(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        await satel_test_connection("127.0.0.1", 7094)
        assert fakes[0].closed_called is True

    async def test_passes_encryption_key_as_library_integration_key_param(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        await satel_test_connection("127.0.0.1", 7094, encryption_key="AESKEY")
        assert fakes[0].encryption_key == "AESKEY"

    async def test_no_encryption_key_means_none_passed_to_library(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        await satel_test_connection("127.0.0.1", 7094)
        assert fakes[0].encryption_key is None


class TestSatelIntegraClientConnect:

    async def test_passes_empty_lists_when_no_monitored_items(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key", monitored_zones={}, monitored_outputs={})
        await client.connect()

        assert fakes[0].monitored_zones == []
        assert fakes[0].monitored_outputs == []

    async def test_passes_configured_monitored_zones_and_outputs(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient(
            "127.0.0.1", 7094, "key",
            monitored_zones={"Hol": 12}, monitored_outputs={"Brama": 3},
        )
        await client.connect()

        assert fakes[0].monitored_zones == [12]
        assert fakes[0].monitored_outputs == [3]

    async def test_passes_encryption_key_as_library_integration_key_not_user_code(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "kod_uzytkownika", encryption_key="AESKEY")
        await client.connect()

        assert fakes[0].encryption_key == "AESKEY"
        assert client.integration_key == "kod_uzytkownika"

    async def test_no_encryption_key_configured_means_cleartext(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        await client.connect()

        assert fakes[0].encryption_key is None

    async def test_raises_connection_error_when_panel_busy(self, monkeypatch):
        from satel_integra.exceptions import SatelPanelBusyError
        _patch_async_satel(monkeypatch, connect_exception=SatelPanelBusyError("busy"))
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        with pytest.raises(ConnectionError, match="zajęta"):
            await client.connect()

    async def test_starts_client_with_monitoring_enabled(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        await client.connect()

        assert fakes[0].start_called is True
        assert fakes[0].start_enable_monitoring is True


class FakeAsyncSatelDiscovery(FakeAsyncSatel):
    _events: list[tuple[list[int], list[int]]] = []

    async def start(self, enable_monitoring=True):
        await super().start(enable_monitoring=enable_monitoring)
        self._play_task = asyncio.ensure_future(self._play_events())

    async def _play_events(self):
        for zones, outputs in self._events:
            self.violated_zones = zones
            self.violated_outputs = outputs
            if self._zone_changed_callback:
                self._zone_changed_callback({})
            if self._output_changed_callback:
                self._output_changed_callback({})
            await asyncio.sleep(0.01)
        await asyncio.sleep(999)

    async def close(self):
        await super().close()
        task = getattr(self, "_play_task", None)
        if task:
            task.cancel()


class TestDiscoverActiveZonesAndOutputs:

    async def test_accumulates_zones_and_outputs_across_multiple_events(self, monkeypatch):
        class Fake(FakeAsyncSatelDiscovery):
            _events = [([12], []), ([12, 13], []), ([], [3])]

        _patch_async_satel(monkeypatch, factory=Fake)
        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.2)
        assert result["zones"] == [12, 13]
        assert result["outputs"] == [3]

    async def test_no_activity_returns_empty_lists(self, monkeypatch):
        class Fake(FakeAsyncSatelDiscovery):
            _events = []

        _patch_async_satel(monkeypatch, factory=Fake)
        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.05)
        assert result == {"zones": [], "outputs": []}

    async def test_connect_failure_raises_connection_error(self, monkeypatch):
        from satel_integra.exceptions import SatelConnectFailedError
        _patch_async_satel(
            monkeypatch, factory=FakeAsyncSatelDiscovery, connect_exception=SatelConnectFailedError("boom"),
        )
        with pytest.raises(ConnectionError, match="127.0.0.1"):
            await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.05)

    async def test_panel_busy_raises_readable_connection_error(self, monkeypatch):
        from satel_integra.exceptions import SatelPanelBusyError
        _patch_async_satel(
            monkeypatch, factory=FakeAsyncSatelDiscovery, connect_exception=SatelPanelBusyError("busy"),
        )
        with pytest.raises(ConnectionError, match="zajęta"):
            await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.05)

    async def test_result_numbers_are_sorted(self, monkeypatch):
        class Fake(FakeAsyncSatelDiscovery):
            _events = [([5, 1, 3], [9, 2])]

        _patch_async_satel(monkeypatch, factory=Fake)
        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.2)
        assert result["zones"] == [1, 3, 5]
        assert result["outputs"] == [2, 9]

    async def test_passes_encryption_key_to_library(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch, factory=FakeAsyncSatelDiscovery)
        await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.02, encryption_key="AESKEY")
        assert fakes[0].encryption_key == "AESKEY"


def _wait_until_finished(scan: BackgroundZoneOutputScan, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = scan.status()
        if not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError("skan w tle nie zakończył się w oczekiwanym czasie")


class TestBackgroundZoneOutputScan:

    def test_status_reports_running_immediately_after_start(self, monkeypatch):
        _patch_async_satel(monkeypatch, factory=FakeAsyncSatelDiscovery)
        scan = BackgroundZoneOutputScan()
        scan.start("127.0.0.1", 7094, timeout_seconds=0.3)
        try:
            status = scan.status()
            assert status["running"] is True
            assert status["duration_seconds"] == 0.3
        finally:
            _wait_until_finished(scan)

    def test_status_reports_result_after_completion(self, monkeypatch):
        class Fake(FakeAsyncSatelDiscovery):
            _events = [([12], [3])]

        _patch_async_satel(monkeypatch, factory=Fake)
        scan = BackgroundZoneOutputScan()
        scan.start("127.0.0.1", 7094, timeout_seconds=0.05)
        status = _wait_until_finished(scan)
        assert status["result"] == {"zones": [12], "outputs": [3]}
        assert status["error"] is None

    def test_connection_error_surfaces_in_status(self, monkeypatch):
        from satel_integra.exceptions import SatelConnectFailedError
        _patch_async_satel(
            monkeypatch, factory=FakeAsyncSatelDiscovery, connect_exception=SatelConnectFailedError("boom"),
        )
        scan = BackgroundZoneOutputScan()
        scan.start("127.0.0.1", 7094, timeout_seconds=0.05)
        status = _wait_until_finished(scan)
        assert status["result"] is None
        assert "127.0.0.1" in status["error"]

    def test_import_error_produces_friendly_message(self, monkeypatch):
        async def fake_discover(*args, **kwargs):
            raise ImportError("no module named satel_integra")

        monkeypatch.setattr("bridge.satel_client.discover_active_zones_and_outputs", fake_discover)
        scan = BackgroundZoneOutputScan()
        scan.start("127.0.0.1", 7094, timeout_seconds=0.05)
        status = _wait_until_finished(scan)
        assert status["error"] == "Biblioteka satel_integra nie jest zainstalowana na serwerze"

    def test_generic_exception_wrapped_with_prefix(self, monkeypatch):
        async def fake_discover(*args, **kwargs):
            raise RuntimeError("coś nieoczekiwanego")

        monkeypatch.setattr("bridge.satel_client.discover_active_zones_and_outputs", fake_discover)
        scan = BackgroundZoneOutputScan()
        scan.start("127.0.0.1", 7094, timeout_seconds=0.05)
        status = _wait_until_finished(scan)
        assert status["error"] == "Błąd wykrywania: coś nieoczekiwanego"

    def test_starting_twice_while_running_raises(self, monkeypatch):
        _patch_async_satel(monkeypatch, factory=FakeAsyncSatelDiscovery)
        scan = BackgroundZoneOutputScan()
        scan.start("127.0.0.1", 7094, timeout_seconds=0.5)
        try:
            with pytest.raises(RuntimeError):
                scan.start("127.0.0.1", 7094, timeout_seconds=0.5)
        finally:
            _wait_until_finished(scan)


class TestRunForever:

    async def test_connects_and_starts_monitoring(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        assert fakes[0].start_called is True
        assert fakes[0].start_enable_monitoring is True

    async def test_registered_callbacks_are_wired_into_library(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        async def zone_cb(zones):
            pass

        async def output_cb(outputs):
            pass

        async def alarm_cb():
            pass

        client.register_zone_callback(zone_cb)
        client.register_output_callback(output_cb)
        client.register_alarm_status_callback(alarm_cb)

        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        assert fakes[0]._zone_changed_callback is not None
        assert fakes[0]._output_changed_callback is not None
        assert fakes[0]._alarm_status_callback is not None

    async def test_callbacks_actually_run_when_library_calls_them_synchronously(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        calls = {"zone": None, "output": None, "alarm": False}

        async def zone_cb(zones):
            calls["zone"] = zones

        async def output_cb(outputs):
            calls["output"] = outputs

        async def alarm_cb():
            calls["alarm"] = True

        client.register_zone_callback(zone_cb)
        client.register_output_callback(output_cb)
        client.register_alarm_status_callback(alarm_cb)

        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.05)

        fakes[0]._zone_changed_callback({1: 1})
        fakes[0]._output_changed_callback({2: 0})
        fakes[0]._alarm_status_callback()

        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert calls["zone"] == {1: 1}
        assert calls["output"] == {2: 0}
        assert calls["alarm"] is True

    async def test_reconnects_when_client_reports_stopped(self, monkeypatch):
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real_sleep(0))

        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        task = asyncio.create_task(client.run_forever())
        await real_sleep(0.01)
        assert len(fakes) == 1, "AsyncSatel nigdy nie zostało utworzone"
        fakes[0]._stopped = True

        for _ in range(30):
            await real_sleep(0.001)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(fakes) >= 2, "Supervisor powinien ponowić connect() po wykryciu stopped"

    async def test_reconnects_when_reading_loop_dies_without_stopped_flag(self, monkeypatch):
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real_sleep(0))

        class CrashingReadingLoopFakeAsyncSatel(FakeAsyncSatel):
            async def start(self, enable_monitoring=True):
                await super().start(enable_monitoring)
                asyncio.ensure_future(self._reading_loop())

            async def _reading_loop(self):
                return

        fakes = _patch_async_satel(monkeypatch, factory=CrashingReadingLoopFakeAsyncSatel)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        task = asyncio.create_task(client.run_forever())
        await real_sleep(0.01)
        assert len(fakes) >= 1, "AsyncSatel nigdy nie zostało utworzone"
        assert fakes[0].stopped is False, "Fałszywy klient nie powinien zgłaszać stopped"

        for _ in range(30):
            await real_sleep(0.001)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(fakes) >= 2, (
            "Supervisor powinien ponowić connect() po śmierci _reading_loop, "
            "nawet gdy client.stopped pozostaje False"
        )


class TestPatchSatelIntegraVariableLengthBitmasks:

    def test_get_active_bits_tolerates_shorter_bitmask_than_expected(self):
        from bridge.satel_client import _patch_satel_integra_variable_length_bitmasks
        from satel_integra.commands import SatelReadCommand
        from satel_integra.messages import SatelReadMessage

        _patch_satel_integra_variable_length_bitmasks()

        msg_data = bytearray(16)
        msg_data[0] = 0b00000101
        msg = SatelReadMessage(SatelReadCommand.ZONES_VIOLATED, msg_data)

        assert msg.get_active_bits(32) == [1, 3]

    def test_zones_violated_handler_survives_16_byte_panel(self):
        from bridge.satel_client import _patch_satel_integra_variable_length_bitmasks
        from satel_integra import AsyncSatel
        from satel_integra.commands import SatelReadCommand
        from satel_integra.messages import SatelReadMessage

        _patch_satel_integra_variable_length_bitmasks()

        client = AsyncSatel("127.0.0.1", 7094)
        msg_data = bytearray(16)
        msg_data[0] = 0b00000101
        msg = SatelReadMessage(SatelReadCommand.ZONES_VIOLATED, msg_data)

        client._zones_violated(msg)

        assert client.violated_zones == [1, 3]

    def test_matching_length_still_decoded_correctly(self):
        from bridge.satel_client import _patch_satel_integra_variable_length_bitmasks
        from satel_integra.commands import SatelReadCommand
        from satel_integra.messages import SatelReadMessage

        _patch_satel_integra_variable_length_bitmasks()

        msg_data = bytearray(4)
        msg_data[0] = 0b00000001
        msg = SatelReadMessage(SatelReadCommand.PARTITIONS_ARMED_MODE0, msg_data)

        assert msg.get_active_bits(4) == [1]

    def test_patch_is_idempotent(self):
        from bridge.satel_client import _patch_satel_integra_variable_length_bitmasks

        _patch_satel_integra_variable_length_bitmasks()
        _patch_satel_integra_variable_length_bitmasks()


class TestIsConnected:
    def test_false_before_connect(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        assert client.is_connected is False

    def test_true_when_client_present_and_not_stopped(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            stopped = False

        client._client = FakeClient()
        assert client.is_connected is True

    def test_false_when_client_reports_stopped(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            stopped = True

        client._client = FakeClient()
        assert client.is_connected is False

    def test_false_when_reading_loop_has_died_even_if_client_not_stopped(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            stopped = False

        client._client = FakeClient()
        client._reading_loop_alive = False
        assert client.is_connected is False


class TestReadingLoopWatchdog:

    async def test_reading_loop_alive_true_right_after_connect(self, monkeypatch):
        _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        await client.connect()
        assert client._reading_loop_alive is True
        assert client.is_connected is True

    async def test_reading_loop_alive_flips_false_once_loop_returns(self, monkeypatch):
        class CrashingReadingLoopFakeAsyncSatel(FakeAsyncSatel):
            async def _reading_loop(self):
                return

        _patch_async_satel(monkeypatch, factory=CrashingReadingLoopFakeAsyncSatel)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        await client.connect()
        assert client._reading_loop_alive is True

        await client._client._reading_loop()
        assert client._reading_loop_alive is False
        assert client.is_connected is False


class TestPeriodicStatusSync:
    async def test_queries_zones_and_outputs_after_connect(self, monkeypatch):
        from satel_integra.commands import SatelReadCommand

        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient(
            "127.0.0.1", 7094, "key",
            monitored_zones={"Hol": 12}, monitored_outputs={"Brama": 3},
        )
        client._initial_status_delay_seconds = 0.01

        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        sent_cmds = [q.cmd for q in fakes[0].sent_queries]
        assert SatelReadCommand.ZONES_VIOLATED in sent_cmds
        assert SatelReadCommand.OUTPUTS_STATE in sent_cmds

    async def test_skips_queries_when_nothing_monitored(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        client._initial_status_delay_seconds = 0.01

        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        assert fakes[0].sent_queries == []

    async def test_queries_repeat_periodically_not_just_once(self, monkeypatch):
        from satel_integra.commands import SatelReadCommand

        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient(
            "127.0.0.1", 7094, "key",
            monitored_zones={"Hol": 12}, monitored_outputs={"Brama": 3},
        )
        client._initial_status_delay_seconds = 0.01
        client.status_poll_interval_seconds = 0.02

        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.11)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        zone_queries = [q for q in fakes[0].sent_queries if q.cmd == SatelReadCommand.ZONES_VIOLATED]
        output_queries = [q for q in fakes[0].sent_queries if q.cmd == SatelReadCommand.OUTPUTS_STATE]
        assert len(zone_queries) >= 2, "dopytanie powinno się powtarzać, nie tylko raz po połączeniu"
        assert len(output_queries) >= 2

    async def test_survives_transient_send_error_and_keeps_polling(self):
        class FlakyFakeAsyncSatel(FakeAsyncSatel):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.calls = 0

            async def _send_data(self, msg):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionResetError("chwilowa zadyszka sieci")
                await super()._send_data(msg)

        client = SatelIntegraClient("127.0.0.1", 7094, "key", monitored_outputs={"Brama": 3})
        fake = FlakyFakeAsyncSatel("127.0.0.1", 7094)
        client._client = fake
        client._initial_status_delay_seconds = 0.01
        client.status_poll_interval_seconds = 0.02

        task = asyncio.create_task(client._periodic_status_sync())
        await asyncio.sleep(0.09)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fake.calls >= 3, "pętla powinna próbować dalej po jednym błędzie, nie zatrzymać się na stałe"
        assert len(fake.sent_queries) >= 2, "kolejne (udane) zapytania powinny nadal trafiać do kolejki"

    async def test_stops_when_superseded_by_reconnect(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key", monitored_zones={"Hol": 12})
        old_fake = FakeAsyncSatel("127.0.0.1", 7094)
        client._client = old_fake
        client._initial_status_delay_seconds = 0.01
        client.status_poll_interval_seconds = 0.02

        task = asyncio.create_task(client._periodic_status_sync())
        await asyncio.sleep(0.07)
        count_before_swap = len(old_fake.sent_queries)
        assert count_before_swap > 0

        new_fake = FakeAsyncSatel("127.0.0.1", 7094)
        client._client = new_fake

        await asyncio.sleep(0.07)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(old_fake.sent_queries) == count_before_swap, "stare zadanie powinno przestać wysyłać"
        assert new_fake.sent_queries == [], "stare zadanie nie powinno zacząć wysyłać na nowym obiekcie"

    def test_status_poll_interval_seconds_configurable_via_constructor(self):
        client = SatelIntegraClient(
            "127.0.0.1", 7094, "key", status_poll_interval_seconds=42.0,
        )
        assert client.status_poll_interval_seconds == 42.0

    def test_status_poll_interval_seconds_defaults_to_3(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        assert client.status_poll_interval_seconds == 3.0


class TestArmedPartitionNumbers:

    def test_empty_when_not_connected(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        assert client.armed_partition_numbers == set()

    def test_unions_partitions_across_all_alarm_states(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            partition_states = {"mode_a": [1, 2], "mode_b": [2, 3]}

        client._client = FakeClient()
        assert client.armed_partition_numbers == {1, 2, 3}

    def test_empty_partition_states_gives_empty_set(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            partition_states = {}

        client._client = FakeClient()
        assert client.armed_partition_numbers == set()


class TestTriggeredPartitionNumbers:

    def test_empty_when_not_connected(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        assert client.triggered_partition_numbers == set()

    def test_unions_triggered_and_triggered_fire_only(self):
        from satel_integra import AlarmState

        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            partition_states = {
                AlarmState.TRIGGERED: [1],
                AlarmState.TRIGGERED_FIRE: [2],
                AlarmState.ARMED_MODE0: [3],
            }

        client._client = FakeClient()
        assert client.triggered_partition_numbers == {1, 2}


class TestDisarmClearsAlarmMemory:

    def _client_with_triggered(self, triggered_partitions: list[int]):
        from satel_integra import AlarmState

        client = SatelIntegraClient("127.0.0.1", 7094, "1234")

        class FakeClient:
            partition_states = {AlarmState.TRIGGERED: triggered_partitions}

            def __init__(self):
                self.sent: list = []

            async def _send_data_and_wait(self, msg):
                self.sent.append(msg)
                return _ok_result()

        client._client = FakeClient()
        return client

    async def test_sends_clear_alarm_when_partition_was_triggered(self, monkeypatch):
        from satel_integra.commands import SatelWriteCommand

        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real_sleep(0))
        client = self._client_with_triggered([1])
        await client.disarm(partitions=[1])

        sent = client._client.sent
        assert len(sent) == 2
        assert sent[0].cmd == SatelWriteCommand.PARTITIONS_DISARM
        assert sent[1].cmd == SatelWriteCommand.PARTITIONS_CLEAR_ALARM

    async def test_skips_clear_alarm_when_not_triggered(self):
        from satel_integra.commands import SatelWriteCommand

        client = self._client_with_triggered([])
        await client.disarm(partitions=[1])

        sent = client._client.sent
        assert len(sent) == 1
        assert sent[0].cmd == SatelWriteCommand.PARTITIONS_DISARM

    async def test_skips_clear_alarm_when_different_partition_triggered(self):
        from satel_integra.commands import SatelWriteCommand

        client = self._client_with_triggered([2])
        await client.disarm(partitions=[1])

        sent = client._client.sent
        assert len(sent) == 1
        assert sent[0].cmd == SatelWriteCommand.PARTITIONS_DISARM


class _FakeCommandClient:

    def __init__(self, response=None, hang=False):
        self._response = response
        self._hang = hang
        self.sent: list = []

    async def _send_data_and_wait(self, msg):
        self.sent.append(msg)
        if self._hang:
            await asyncio.sleep(999)
        return self._response


class TestWaitForCommandResult:

    async def test_succeeds_when_panel_confirms_ok(self):
        client = _FakeCommandClient(response=_ok_result())
        await _wait_for_command_result(client, "test", object())

    async def test_succeeds_when_panel_confirms_with_0xff(self):
        client = _FakeCommandClient(response=_error_result(0xFF))
        await _wait_for_command_result(client, "test", object())

    async def test_raises_when_panel_rejects_command(self):
        client = _FakeCommandClient(response=_error_result(0x01))
        with pytest.raises(RuntimeError, match="nieznany kod użytkownika"):
            await _wait_for_command_result(client, "test", object())

    async def test_raises_connection_error_when_no_response(self):
        client = _FakeCommandClient(response=None)
        with pytest.raises(ConnectionError, match="brak odpowiedzi"):
            await _wait_for_command_result(client, "test", object())

    async def test_raises_connection_error_on_timeout(self):
        client = _FakeCommandClient(hang=True)
        with pytest.raises(ConnectionError, match="nie potwierdziła"):
            await _wait_for_command_result(client, "test", object(), timeout=0.05)


class TestTestOutput:

    def _patch(self, monkeypatch, **kwargs):
        return _patch_async_satel(monkeypatch, **kwargs)

    async def test_succeeds_and_starts_client_without_monitoring(self, monkeypatch):
        fakes = self._patch(monkeypatch)
        await satel_test_output("127.0.0.1", 7094, "1234", 15, True)
        assert fakes[0].start_called is True
        assert fakes[0].start_enable_monitoring is False

    async def test_raises_connection_error_when_connect_fails(self, monkeypatch):
        from satel_integra.exceptions import SatelConnectFailedError
        self._patch(monkeypatch, connect_exception=SatelConnectFailedError("boom"))
        with pytest.raises(ConnectionError, match="127.0.0.1"):
            await satel_test_output("127.0.0.1", 7094, "1234", 15, True)

    async def test_raises_connection_error_when_panel_busy(self, monkeypatch):
        from satel_integra.exceptions import SatelPanelBusyError
        self._patch(monkeypatch, connect_exception=SatelPanelBusyError("busy"))
        with pytest.raises(ConnectionError, match="zajęta"):
            await satel_test_output("127.0.0.1", 7094, "1234", 15, True)

    async def test_raises_runtime_error_when_panel_rejects(self, monkeypatch):
        self._patch(monkeypatch, confirm_response=_error_result(0x01))
        with pytest.raises(RuntimeError, match="nieznany kod użytkownika"):
            await satel_test_output("127.0.0.1", 7094, "bad", 15, True)

    async def test_raises_connection_error_on_confirm_timeout(self, monkeypatch):
        self._patch(monkeypatch, hang_confirm=True)
        with pytest.raises(ConnectionError, match="nie potwierdziła"):
            await satel_test_output("127.0.0.1", 7094, "1234", 15, True, timeout_seconds=0.05)

    async def test_closes_client_after_test(self, monkeypatch):
        fakes = self._patch(monkeypatch)
        await satel_test_output("127.0.0.1", 7094, "1234", 15, True)
        assert fakes[0].closed_called is True


class _FakeHttpResponse:
    def __init__(self, exc=None):
        self._exc = exc

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        return b""


class _FakeHttpSession:
    def __init__(self, urls: list, exc=None):
        self._urls = urls
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, timeout=None):
        self._urls.append(url)
        return _FakeHttpResponse(self._exc)


class TestVirtualInputHttpTrigger:

    def _patch_aiohttp(self, monkeypatch, urls: list, exc=None):
        import aiohttp
        monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeHttpSession(urls, exc))

    async def test_trigger_sends_get_to_host_port_name(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        vi = SatelVirtualInputTrigger("192.168.1.50", {
            "port": 9786,
            "inputs": {"zmywarka": 17},
        })
        await vi.trigger("zmywarka")
        assert urls == ["http://192.168.1.50:9786/zmywarka"]

    async def test_trigger_ignores_stale_stored_url_template(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        vi = SatelVirtualInputTrigger("192.168.1.50", {
            "port": 9786,
            "url_template": "http://{host}:{port}/input.cgi?key={key}&input={input_no}",
            "inputs": {"zmywarka": 17},
        })
        await vi.trigger("zmywarka")
        assert urls == ["http://192.168.1.50:9786/zmywarka"]

    async def test_trigger_unknown_name_logs_and_does_not_raise(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        vi = SatelVirtualInputTrigger("192.168.1.50", {"inputs": {}})
        await vi.trigger("nieznane")
        assert urls == []

    async def test_trigger_uses_default_port_when_absent(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        vi = SatelVirtualInputTrigger("192.168.1.50", {"inputs": {"x": 1}})
        await vi.trigger("x")
        assert urls == ["http://192.168.1.50:8888/x"]

    async def test_test_virtual_input_http_succeeds_silently(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        await satel_test_virtual_input_http(
            "192.168.1.50", 9786, "http://{host}:{port}/{name}", "zmywarka",
        )
        assert urls == ["http://192.168.1.50:9786/zmywarka"]

    async def test_test_virtual_input_http_raises_connection_error_on_network_failure(self, monkeypatch):
        import aiohttp
        urls = []
        self._patch_aiohttp(monkeypatch, urls, exc=aiohttp.ClientConnectionError("refused"))
        with pytest.raises(ConnectionError, match="Błąd sieci"):
            await satel_test_virtual_input_http(
                "192.168.1.50", 9786, "http://{host}:{port}/{name}", "zmywarka",
            )
