"""Testy dla bridge/satel_client.py - głównie test_connection() (podpowiedź
"Testuj połączenie" w kreatorze/panelu), bez prawdziwej centrali Satel.

Napisane pod satel_integra >=1.4.0 (patrz obszerny docstring
bridge/satel_client.py) - fejki poniżej odzwierciedlają PUBLICZNE API tej
wersji (register_callbacks()/start()/close()/stopped/_send_data/
_send_data_and_wait), nie starsze, prywatne API 0.3.x."""
import asyncio
import contextlib

import pytest

from bridge.satel_client import (
    SatelIntegraClient, SatelVirtualInputTrigger, _wait_for_command_result,
    discover_active_zones_and_outputs,
)
from bridge.satel_client import test_connection as satel_test_connection
from bridge.satel_client import test_output as satel_test_output
from bridge.satel_client import test_virtual_input_http as satel_test_virtual_input_http
# ^ import aliasu jest CELOWY, nie kosmetyczny - pytest zbiera każdą funkcję
# o nazwie pasującej do "test_*" widoczną w module testowym, więc zwykłe
# "from ... import test_connection" sprawiłoby, że pytest próbowałby
# uruchomić samą funkcję produkcyjną jako (bezargumentowy) test i wywalał
# się błędem "fixture 'host' not found".


class _FakeResult:
    """Odpowiednik SatelReadMessage zwracanego przez client._send_data_and_wait()
    - jedyne, co _wait_for_command_result() z niego czyta, to .msg_data[0]."""

    def __init__(self, msg_data: bytearray):
        self.msg_data = msg_data


def _ok_result() -> _FakeResult:
    return _FakeResult(bytearray([0x00]))


def _error_result(error_byte: int) -> _FakeResult:
    return _FakeResult(bytearray([error_byte]))


class FakeAsyncSatel:
    """Fejk `satel_integra.AsyncSatel` (publiczne API >=1.0.0, patrz obszerny
    docstring bridge/satel_client.py) - patchuje `satel_integra.AsyncSatel`
    na poziomie PAKIETU (`satel_integra.AsyncSatel`), bo bridge/satel_client.py
    zawsze robi `from satel_integra import AsyncSatel` WEWNĄTRZ funkcji
    (odroczony import) - podmiana atrybutu na `satel_integra.satel_integra`
    (podmoduł) NIE zostałaby zauważona, bo lookup dzieje się na atrybucie
    pakietu w momencie wywołania, nie submodułu."""

    def __init__(self, host, port, monitored_zones=None, monitored_outputs=None,
                 partitions=None, integration_key=None,
                 connect_result=True, connect_exception=None, hang_connect=False,
                 confirm_response=None, hang_confirm=False):
        self.host = host
        self.port = port
        self.monitored_zones = list(monitored_zones or [])
        self.monitored_outputs = list(monitored_outputs or [])
        # Nazwa pola biblioteki to `integration_key`, ale to KLUCZ SZYFROWANIA
        # (patrz obszerny docstring modułu) - przechowujemy pod jawną nazwą,
        # żeby testy nie myliły go z kodem użytkownika.
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


def _patch_async_satel(monkeypatch, factory=None, **behavior):
    """Patchuje satel_integra.AsyncSatel tak, żeby konstruować `factory`
    (domyślnie FakeAsyncSatel) z argumentami PRODUKCYJNYMI (monitored_zones/
    monitored_outputs/integration_key, przekazywanymi przez sam kod
    bridge/satel_client.py) scalonymi ze stałymi `behavior` (connect_result/
    connect_exception/hang_connect/confirm_response/hang_confirm - do
    sterowania zachowaniem fejka z poziomu testu). Zwraca listę utworzonych
    instancji (zwykle jeden element - jeden connect - ale np. test
    ponawiania połączenia tworzy ich kilka)."""
    cls = factory or FakeAsyncSatel
    fakes: list = []

    def fake_ctor(host, port, **prod_kwargs):
        fake = cls(host, port, **prod_kwargs, **behavior)
        fakes.append(fake)
        return fake

    monkeypatch.setattr("satel_integra.AsyncSatel", fake_ctor)
    return fakes


class TestConnectionTest:
    """test_connection() - podpowiedź "Testuj połączenie" w kreatorze/
    panelu. satel_integra >=1.0.0 sam wykonuje pełną weryfikację (busy-check
    + sonda protokołu) przy connect(raise_exceptions=True) - inaczej niż
    0.3.x (samo otwarcie gniazda TCP)."""

    async def test_succeeds_silently_when_connect_returns_true(self, monkeypatch):
        _patch_async_satel(monkeypatch, connect_result=True)
        await satel_test_connection("127.0.0.1", 7094)  # nie powinno rzucić wyjątku

    async def test_raises_connection_error_when_connect_fails(self, monkeypatch):
        from satel_integra.exceptions import SatelConnectFailedError
        _patch_async_satel(monkeypatch, connect_exception=SatelConnectFailedError("boom"))
        with pytest.raises(ConnectionError, match="127.0.0.1"):
            await satel_test_connection("127.0.0.1", 7094)

    async def test_raises_connection_error_when_panel_busy(self, monkeypatch):
        """SatelPanelBusyError - nowość w satel_integra >=1.0.0 (patrz
        obszerny docstring bridge/satel_client.py) - jednoznaczny sygnał
        "inny klient integracji już podłączony", zamiast zgadywania po
        serii rozłączeń jak w 0.3.x."""
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
        """Regresja dla rozróżnienia dwóch RÓŻNYCH kluczy (patrz obszerny
        docstring modułu): `encryption_key` z configu musi trafić do
        biblioteki jako jej WŁASNY parametr konstruktora `integration_key`
        (klucz szyfrowania AES), NIE do żadnego innego pola."""
        fakes = _patch_async_satel(monkeypatch)
        await satel_test_connection("127.0.0.1", 7094, encryption_key="AESKEY")
        assert fakes[0].encryption_key == "AESKEY"

    async def test_no_encryption_key_means_none_passed_to_library(self, monkeypatch):
        fakes = _patch_async_satel(monkeypatch)
        await satel_test_connection("127.0.0.1", 7094)
        assert fakes[0].encryption_key is None


class TestSatelIntegraClientConnect:
    """SatelIntegraClient.connect() - argumenty przekazywane do AsyncSatel."""

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
        """KLUCZOWE rozróżnienie (patrz obszerny docstring modułu): kod
        użytkownika (`self.integration_key`, drugi argument konstruktora
        SatelIntegraClient) NIE MOŻE trafić do biblioteki jako jej
        `integration_key` (klucz szyfrowania) - tam trafia WYŁĄCZNIE
        `encryption_key`."""
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
    """start() 'odgrywa' zadaną sekwencję zdarzeń (kolejne stany
    violated_zones/violated_outputs, wywołując zarejestrowane callbacki),
    potem zasypia w nieskończoność - dokładnie jak prawdziwe start(), które
    wraca od razu po wystartowaniu zadań w tle i kończy działanie tylko
    przez close()."""
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
    """discover_active_zones_and_outputs() - wykrywanie best-effort w kroku
    1 kreatora: numery stref/wyjść, które ZMIENIŁY STAN podczas nasłuchu
    (nie pełna lista skonfigurowanych stref - to niemożliwe do uzyskania z
    samego protokołu integracji, patrz docstring funkcji)."""

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
        """satel_integra >=1.0.0 rozpoznaje zajętą centralę JEDNOZNACZNIE i
        natychmiast przy connect() (SatelPanelBusyError) - poprzednia wersja
        0.3.x nie miała takiego wyjątku, trzeba było zgadywać po serii
        rozłączeń podczas nasłuchu (usunięty heurystyczny klucz "warning" w
        wyniku - patrz docstring discover_active_zones_and_outputs())."""
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


class TestRunForever:
    """SatelIntegraClient.run_forever() - ISTOTNIE PROSTSZE niż dla
    satel_integra 0.3.x (patrz obszerny docstring modułu): biblioteka 1.x
    SAMA obsługuje keepalive/reconnect/wykrywanie "centrala zajęta"
    wewnątrz start() - nie trzeba już własnego watchdoga "sprawdzaj
    `connected` z zewnątrz co 5s"."""

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
        """Regresja dla realnego buga: biblioteka satel_integra woła
        _zone_changed_callback/_output_changed_callback/_alarm_status_callback
        SYNCHRONICZNIE (zwykłe wywołanie funkcji, bez await) - bez zmian
        względem 0.3.x. Skoro nasze callbacki (zone_cb/output_cb/alarm_cb
        niżej) są `async def`, wywołanie ich BEZ await tylko tworzy i
        odrzuca obiekt korutyny - CIAŁO NIGDY SIĘ NIE WYKONUJE.
        register_*_callback() musi więc rejestrować w bibliotece
        synchroniczny wrapper, który sam planuje korutynę (patrz
        SatelIntegraClient._schedule())."""
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
        await asyncio.sleep(0.05)  # daje monitor_status() fejka czas na start

        # Symuluje bibliotekę: wywołuje zarejestrowane callbacki
        # SYNCHRONICZNIE, dokładnie tak jak w prawdziwej AsyncSatel.
        fakes[0]._zone_changed_callback({1: 1})
        fakes[0]._output_changed_callback({2: 0})
        fakes[0]._alarm_status_callback()

        await asyncio.sleep(0.05)  # da zaplanowanym zadaniom szansę się wykonać
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert calls["zone"] == {1: 1}
        assert calls["output"] == {2: 0}
        assert calls["alarm"] is True

    async def test_reconnects_when_client_reports_stopped(self, monkeypatch):
        """`stopped` (właściwość AsyncSatel, patrz obszerny docstring modułu)
        staje się True TYLKO po jawnym close() albo nienaprawialnym błędzie
        WEWNĘTRZNYM biblioteki - zwykłe, przejściowe zerwania sieci biblioteka
        naprawia sama w tle i `stopped` zostaje False. Watchdog w
        run_forever() musi wykryć terminalne `stopped=True` i pozwolić
        Supervisor.run() ponowić connect() (nowy obiekt AsyncSatel) zamiast
        wisieć w nieskończoność, obserwując martwe połączenie."""
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


class TestPeriodicStatusSync:
    async def test_queries_zones_and_outputs_after_connect(self, monkeypatch):
        """Regresja/funkcja: biblioteka satel_integra wywołuje
        zone_changed_callback/output_changed_callback TYLKO przy faktycznej
        zmianie stanu - na spokojnym systemie panel/MQTT nigdy nie
        dostałyby żadnego stanu wejść/wyjść aż do pierwszej zmiany.
        SatelIntegraClient.connect() dopytuje więc aktywnie o bieżący stan
        (funkcje protokołu 0x00/0x17)."""
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
        """Zgłoszone z prawdziwej centrali: zmiana stanu WYJŚCIA nie zawsze
        dociera jako broadcast (0x17) - samo jednorazowe dopytanie zaraz po
        połączeniu nie wystarcza do "ciągłej" synchronizacji panelu/MQTT,
        trzeba dopytywać cyklicznie przez cały czas życia połączenia."""
        from satel_integra.commands import SatelReadCommand

        fakes = _patch_async_satel(monkeypatch)
        client = SatelIntegraClient(
            "127.0.0.1", 7094, "key",
            monitored_zones={"Hol": 12}, monitored_outputs={"Brama": 3},
        )
        client._initial_status_delay_seconds = 0.01
        client.status_poll_interval_seconds = 0.02

        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.11)  # ok. 5 cykli po 0.02s + pierwsze opóźnienie
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        zone_queries = [q for q in fakes[0].sent_queries if q.cmd == SatelReadCommand.ZONES_VIOLATED]
        output_queries = [q for q in fakes[0].sent_queries if q.cmd == SatelReadCommand.OUTPUTS_STATE]
        assert len(zone_queries) >= 2, "dopytanie powinno się powtarzać, nie tylko raz po połączeniu"
        assert len(output_queries) >= 2

    async def test_stops_when_superseded_by_reconnect(self):
        """Zadanie NIE może żyć wiecznie po utracie aktualności - jeśli
        connect() (re)połączenia tworzy NOWY obiekt AsyncSatel, stare
        zadanie cyklicznej synchronizacji musi się samo zakończyć (inaczej
        dwa zadania wysyłałyby zapytania równolegle bez końca)."""
        client = SatelIntegraClient("127.0.0.1", 7094, "key", monitored_zones={"Hol": 12})
        old_fake = FakeAsyncSatel("127.0.0.1", 7094)
        client._client = old_fake
        client._initial_status_delay_seconds = 0.01
        client.status_poll_interval_seconds = 0.02

        task = asyncio.create_task(client._periodic_status_sync())
        await asyncio.sleep(0.07)  # kilka cykli na "starym" połączeniu
        count_before_swap = len(old_fake.sent_queries)
        assert count_before_swap > 0

        new_fake = FakeAsyncSatel("127.0.0.1", 7094)
        client._client = new_fake  # symuluje reconnect - nowy obiekt AsyncSatel

        await asyncio.sleep(0.07)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(old_fake.sent_queries) == count_before_swap, "stare zadanie powinno przestać wysyłać"
        assert new_fake.sent_queries == [], "stare zadanie nie powinno zacząć wysyłać na nowym obiekcie"

    def test_status_poll_interval_seconds_configurable_via_constructor(self):
        """Interwał odpytywania jest atrybutem PUBLICZNYM (bez podkreślnika)
        ustawianym przez konstruktor - RulesEngine.reload_config() musi móc
        go podmienić na żywo (bez restartu procesu), patrz też
        test_reload_config_updates_satel_poll_interval_live w
        tests/test_rules_engine.py."""
        client = SatelIntegraClient(
            "127.0.0.1", 7094, "key", status_poll_interval_seconds=42.0,
        )
        assert client.status_poll_interval_seconds == 42.0

    def test_status_poll_interval_seconds_defaults_to_10(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        assert client.status_poll_interval_seconds == 10.0


class TestArmedPartitionNumbers:
    """SatelIntegraClient.armed_partition_numbers - odczyt na żądanie
    (nie push) bieżącego stanu uzbrojenia, potrzebny bo
    register_alarm_status_callback woła callback BEZ ŻADNYCH argumentów
    (patrz RulesEngine._handle_alarm_status_change w rules_engine.py -
    regresja dla zgłoszonego 'missing 1 required positional argument:
    partition_states')."""

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
    """SatelIntegraClient.triggered_partition_numbers - patrz disarm()
    niżej, gdzie decyduje, czy po rozbrojeniu wysłać też clear_alarm()."""

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
    """disarm() - tak jak integracja Home Assistant (alarm_control_panel.py:
    async_alarm_disarm), po rozbrojeniu strefy będącej akurat w stanie
    TRIGGERED/TRIGGERED_FIRE wysyła też komendę czyszczącą pamięć alarmu
    (clear_alarm), bo centrala Satel trzyma ją jako osobną flagę. Komendy
    idą teraz przez _wait_for_command_result()/_send_data_and_wait()
    (patrz obszerny docstring modułu), nie przez publiczne
    AsyncSatel.disarm()/clear_alarm() (fire-and-forget, bez potwierdzenia)."""

    def _client_with_triggered(self, triggered_partitions: list[int]):
        from satel_integra import AlarmState

        # UWAGA: kod użytkownika MUSI być poprawnym łańcuchem hex - biblioteka
        # koduje go przez bytearray.fromhex() (SatelWriteMessage.__init__) -
        # prawdziwe kody DLOADX/GUARDX są numeryczne (same cyfry), więc
        # zawsze są poprawnym hex, ale placeholder "key" użyty gdzie indziej
        # w tym pliku (tam, gdzie nigdy nie trafia do SatelWriteMessage) by
        # nie zadziałał tutaj.
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
    """Minimalny fejk do testowania _wait_for_command_result() w izolacji -
    tylko _send_data_and_wait(), bo to jedyna metoda, z której korzysta."""

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
    """_wait_for_command_result() - regresja dla zgłoszonego z realnej
    instalacji 'nie uzbraja stref, ale poprawnie wyświetla ich stan' -
    biblioteka satel_integra sama NIE czeka na potwierdzenie komendy zapisu
    (0xEF) - PUBLICZNE arm()/disarm()/set_output()/clear_alarm() zawsze idą
    fire-and-forget (patrz obszerny docstring modułu). Błędny kod
    użytkownika albo odrzucenie komendy przez centralę kończyłyby się więc
    CISZĄ porażką - stąd czytanie stanu działało poprawnie, a same komendy
    zapisu nie."""

    async def test_succeeds_when_panel_confirms_ok(self):
        client = _FakeCommandClient(response=_ok_result())
        await _wait_for_command_result(client, "test", object())  # nie powinno rzucić

    async def test_succeeds_when_panel_confirms_with_0xff(self):
        """Patrz _RESULT_OK_CODES w satel_client.py - 0xFF też jest sukcesem
        (zarówno satel_integra 0.3.x jak i 1.x od zawsze tak to traktują,
        mimo że protokół normalnie rezerwuje 0xFF na "inny błąd")."""
        client = _FakeCommandClient(response=_error_result(0xFF))
        await _wait_for_command_result(client, "test", object())  # nie powinno rzucić

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
    """test_output() - przycisk "TEST" obok pola klucza integracji
    (tryb "library")."""

    def _patch(self, monkeypatch, **kwargs):
        return _patch_async_satel(monkeypatch, **kwargs)

    async def test_succeeds_and_starts_client_without_monitoring(self, monkeypatch):
        fakes = self._patch(monkeypatch)
        await satel_test_output("127.0.0.1", 7094, "1234", 15, True)  # nie powinno rzucić wyjątku
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
    """SatelVirtualInputTrigger.trigger() i test_virtual_input_http() -
    tryb "http" wirtualnych wejść IP (funkcja "Wejścia IP" ETHM-1 Plus,
    patrz obszerny docstring bridge/satel_client.py) - czysty aiohttp, bez
    zależności od satel_integra, więc BEZ ZMIAN względem migracji."""

    def _patch_aiohttp(self, monkeypatch, urls: list, exc=None):
        import aiohttp
        monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeHttpSession(urls, exc))

    async def test_trigger_sends_get_with_key_and_input_number(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        vi = SatelVirtualInputTrigger("192.168.1.50", {
            "port": 8888,
            "url_template": "http://{host}:{port}/input.cgi?key={key}&input={input_no}",
            "inputs": {"motion_hall_camera": {"number": 17, "key": "KLUCZWE17"}},
        })
        await vi.trigger("motion_hall_camera")
        assert urls == ["http://192.168.1.50:8888/input.cgi?key=KLUCZWE17&input=17"]

    async def test_trigger_unknown_name_logs_and_does_not_raise(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        vi = SatelVirtualInputTrigger("192.168.1.50", {"inputs": {}})
        await vi.trigger("nieznane")  # nie powinno rzucić wyjątku
        assert urls == []

    async def test_trigger_uses_default_port_and_url_template_when_absent(self, monkeypatch):
        """cfg bez "port"/"url_template" (np. stary config.yaml) dostaje
        bezpieczne wartości domyślne - patrz DEFAULT_VIRTUAL_INPUT_HTTP_PORT/
        DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE."""
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        vi = SatelVirtualInputTrigger("192.168.1.50", {
            "inputs": {"x": {"number": 1, "key": "k"}},
        })
        await vi.trigger("x")
        assert urls == ["http://192.168.1.50:8888/input.cgi?key=k&input=1"]

    async def test_test_virtual_input_http_succeeds_silently(self, monkeypatch):
        urls = []
        self._patch_aiohttp(monkeypatch, urls)
        await satel_test_virtual_input_http(
            "192.168.1.50", 8888, "http://{host}:{port}/input.cgi?key={key}&input={input_no}", "k", 17,
        )  # nie powinno rzucić wyjątku
        assert urls == ["http://192.168.1.50:8888/input.cgi?key=k&input=17"]

    async def test_test_virtual_input_http_raises_connection_error_on_network_failure(self, monkeypatch):
        import aiohttp
        urls = []
        self._patch_aiohttp(monkeypatch, urls, exc=aiohttp.ClientConnectionError("refused"))
        with pytest.raises(ConnectionError, match="Błąd sieci"):
            await satel_test_virtual_input_http(
                "192.168.1.50", 8888, "http://{host}:{port}/input.cgi?key={key}&input={input_no}", "k", 17,
            )
