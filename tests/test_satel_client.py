"""Testy dla bridge/satel_client.py - głównie test_connection() (podpowiedź
"Testuj połączenie" w kreatorze/panelu), bez prawdziwej centrali Satel."""
import asyncio
import contextlib

import pytest

from bridge.satel_client import SatelIntegraClient, discover_active_zones_and_outputs
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


class FakeAsyncSatelDiscovery:
    """Symuluje AsyncSatel dla discover_active_zones_and_outputs() -
    monitor_status() 'odgrywa' zadaną sekwencję zdarzeń (kolejne stany
    violated_zones/violated_outputs, wywołując odpowiednie callbacki), a
    potem zasypia w nieskończoność - dokładnie jak prawdziwe
    monitor_status(), które kończy działanie tylko przez cancel()/rozłączenie."""
    _events: list[tuple[list[int], list[int]]] = []

    def __init__(self, host, port, loop, connect_result=True):
        self._writer = None
        self._connect_result = connect_result
        self.violated_zones: list[int] = []
        self.violated_outputs: list[int] = []

    async def connect(self):
        return self._connect_result

    async def monitor_status(self, zone_changed_callback=None, output_changed_callback=None):
        for zones, outputs in self._events:
            self.violated_zones = zones
            self.violated_outputs = outputs
            if zone_changed_callback:
                zone_changed_callback({})
            if output_changed_callback:
                output_changed_callback({})
            await asyncio.sleep(0.01)
        await asyncio.sleep(999)


class TestDiscoverActiveZonesAndOutputs:
    """discover_active_zones_and_outputs() - wykrywanie best-effort w kroku
    1 kreatora: numery stref/wyjść, które ZMIENIŁY STAN podczas nasłuchu
    (nie pełna lista skonfigurowanych stref - to niemożliwe do uzyskania z
    samego protokołu integracji, patrz docstring funkcji)."""

    async def test_accumulates_zones_and_outputs_across_multiple_events(self, monkeypatch):
        class Fake(FakeAsyncSatelDiscovery):
            _events = [([12], []), ([12, 13], []), ([], [3])]

        import satel_integra.satel_integra as satel_module
        monkeypatch.setattr(satel_module, "AsyncSatel", lambda host, port, loop: Fake(host, port, loop))

        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.2)
        assert result["zones"] == [12, 13]
        assert result["outputs"] == [3]

    async def test_no_activity_returns_empty_lists(self, monkeypatch):
        class Fake(FakeAsyncSatelDiscovery):
            _events = []

        import satel_integra.satel_integra as satel_module
        monkeypatch.setattr(satel_module, "AsyncSatel", lambda host, port, loop: Fake(host, port, loop))

        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.05)
        assert result == {"zones": [], "outputs": []}

    async def test_connect_failure_raises_connection_error(self, monkeypatch):
        import satel_integra.satel_integra as satel_module
        monkeypatch.setattr(
            satel_module, "AsyncSatel",
            lambda host, port, loop: FakeAsyncSatelDiscovery(host, port, loop, connect_result=False),
        )
        with pytest.raises(ConnectionError, match="127.0.0.1"):
            await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.05)

    async def test_result_numbers_are_sorted(self, monkeypatch):
        class Fake(FakeAsyncSatelDiscovery):
            _events = [([5, 1, 3], [9, 2])]

        import satel_integra.satel_integra as satel_module
        monkeypatch.setattr(satel_module, "AsyncSatel", lambda host, port, loop: Fake(host, port, loop))

        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.2)
        assert result["zones"] == [1, 3, 5]
        assert result["outputs"] == [2, 9]


class FakeAsyncSatelKeepAlive:
    """Symuluje AsyncSatel dla run_forever() - rejestruje, czy keep_alive()
    zostało uruchomione OBOK monitor_status() (regresja dla zgłoszonego z
    prawdziwej centrali błędu 'Got exception: 0 bytes read... Most likely
    the other side has disconnected!' - ETHM-1 Plus rozłącza integrację po
    ok. 25s bezczynności, jeśli nic nie wysyła keep_alive())."""

    def __init__(self, host, port, loop):
        self._writer = None
        self.keep_alive_called = False
        self.keep_alive_cancelled = False
        self.monitor_status_called = False
        self.received_alarm_status_callback = None
        self.received_zone_changed_callback = None
        self.received_output_changed_callback = None

    async def connect(self):
        return True

    async def keep_alive(self):
        self.keep_alive_called = True
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            self.keep_alive_cancelled = True
            raise

    async def monitor_status(self, alarm_status_callback=None, zone_changed_callback=None,
                              output_changed_callback=None):
        self.monitor_status_called = True
        self.received_alarm_status_callback = alarm_status_callback
        self.received_zone_changed_callback = zone_changed_callback
        self.received_output_changed_callback = output_changed_callback
        await asyncio.sleep(0.05)  # symuluje krótkotrwałe połączenie, potem "rozłączenie" (normalny powrót)


class TestRunForeverKeepAlive:
    async def test_keep_alive_started_alongside_monitor_status(self, monkeypatch):
        import satel_integra.satel_integra as satel_module

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = FakeAsyncSatelKeepAlive(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        assert fakes[0].monitor_status_called is True
        assert fakes[0].keep_alive_called is True

    async def test_keep_alive_cancelled_when_monitor_status_returns(self, monkeypatch):
        import satel_integra.satel_integra as satel_module

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = FakeAsyncSatelKeepAlive(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        task = asyncio.create_task(client.run_forever())
        # monitor_status() w fejku kończy się po 0.05s (normalny powrót =
        # utrata połączenia) - dajemy margines, żeby finally: zdążyło
        # anulować keep_alive_task PRZED długim opóźnieniem ponawiania
        # (Supervisor.base_delay domyślnie 5s).
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes[0].keep_alive_cancelled is True

    async def test_registered_callbacks_are_passed_into_monitor_status(self, monkeypatch):
        """Regresja: monitor_status() w bibliotece PRZYPISUJE
        self._zone_changed_callback/self._output_changed_callback/
        self._alarm_status_callback z WŁASNYCH argumentów (domyślnie None) na
        starcie - wywołanie go bez argumentów nadpisywało wartością None to,
        co connect() dopiero co ustawił przez register_*_callback(), więc
        żaden callback nigdy się nie wywoływał (stąd zgłoszony brak stanów
        Satela na MQTT/w panelu poza wartościami zaszytymi na starcie)."""
        import satel_integra.satel_integra as satel_module

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = FakeAsyncSatelKeepAlive(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        async def zone_cb(zones):
            pass

        async def output_cb(outputs):
            pass

        async def alarm_cb(states):
            pass

        client.register_zone_callback(zone_cb)
        client.register_output_callback(output_cb)
        client.register_alarm_status_callback(alarm_cb)

        task = asyncio.create_task(client.run_forever())
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        # register_*_callback() nie przekazuje `zone_cb`/`output_cb`/`alarm_cb`
        # WPROST bibliotece, tylko opakowuje je w synchroniczny trampolinowy
        # wrapper (patrz SatelIntegraClient._schedule()) - dlatego dostane tu
        # obiekty nie są identyczne z oryginalnymi korutynami.
        assert fakes[0].received_zone_changed_callback is not None
        assert fakes[0].received_output_changed_callback is not None
        assert fakes[0].received_alarm_status_callback is not None

    async def test_callbacks_actually_run_when_library_calls_them_synchronously(self, monkeypatch):
        """Regresja dla realnego buga: biblioteka satel_integra woła
        _zone_changed_callback/_output_changed_callback/_alarm_status_callback
        SYNCHRONICZNIE (zwykłe wywołanie funkcji, bez await) - patrz
        _zone_violated()/_output_changed()/_armed() w satel_integra.py.
        Skoro nasze callbacki (zone_cb/output_cb/alarm_cb niżej) są `async
        def`, wywołanie ich BEZ await tylko tworzy i odrzuca obiekt korutyny
        - CIAŁO NIGDY SIĘ NIE WYKONUJE. register_*_callback() musi więc
        rejestrować w bibliotece synchroniczny wrapper, który sam planuje
        korutynę (patrz SatelIntegraClient._schedule())."""
        import satel_integra.satel_integra as satel_module

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = FakeAsyncSatelKeepAlive(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

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
        # SYNCHRONICZNIE, dokładnie tak jak _zone_violated()/_output_changed()/
        # _armed() w satel_integra.py.
        fakes[0].received_zone_changed_callback({1: 1})
        fakes[0].received_output_changed_callback({2: 0})
        fakes[0].received_alarm_status_callback()

        await asyncio.sleep(0.05)  # da zaplanowanym zadaniom szansę się wykonać
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert calls["zone"] == {1: 1}
        assert calls["output"] == {2: 0}
        assert calls["alarm"] is True


class FakeAsyncSatelInitialStatus(FakeAsyncSatelKeepAlive):
    """Jak FakeAsyncSatelKeepAlive, ale dodatkowo przechwytuje surowe dane
    wysłane przez _send_data() - do testu cyklicznego dopytywania o stan
    wejść/wyjść (patrz _periodic_status_sync())."""

    def __init__(self, host, port, loop):
        super().__init__(host, port, loop)
        self.closed = False
        self.sent_queries: list[bytes] = []

    async def _send_data(self, data):
        self.sent_queries.append(bytes(data))

    async def monitor_status(self, alarm_status_callback=None, zone_changed_callback=None,
                              output_changed_callback=None):
        # W odróżnieniu od FakeAsyncSatelKeepAlive (kończy się po 0.05s) -
        # tu połączenie ma "żyć" wystarczająco długo, żeby zaobserwować
        # WIELE cykli _periodic_status_sync(), nie tylko pierwsze zapytanie.
        self.monitor_status_called = True
        await asyncio.sleep(999)


class TestPeriodicStatusSync:
    async def test_queries_zones_and_outputs_after_connect(self, monkeypatch):
        """Regresja/funkcja: biblioteka satel_integra wywołuje
        zone_changed_callback/output_changed_callback TYLKO przy faktycznej
        zmianie stanu - na spokojnym systemie panel/MQTT nigdy nie
        dostałyby żadnego stanu wejść/wyjść aż do pierwszej zmiany.
        SatelIntegraClient.connect() dopytuje więc aktywnie o bieżący stan
        (funkcje protokołu 0x00/0x17) z opóźnieniem, żeby nie kolidować z
        odczytem potwierdzenia start_monitoring()."""
        import satel_integra.satel_integra as satel_module
        from satel_integra.satel_integra import generate_query

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = FakeAsyncSatelInitialStatus(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

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
        assert bytes(generate_query(b'\x00')) in fakes[0].sent_queries
        assert bytes(generate_query(b'\x17')) in fakes[0].sent_queries

    async def test_skips_queries_when_nothing_monitored(self, monkeypatch):
        import satel_integra.satel_integra as satel_module

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = FakeAsyncSatelInitialStatus(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

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
        import satel_integra.satel_integra as satel_module
        from satel_integra.satel_integra import generate_query

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = FakeAsyncSatelInitialStatus(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

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
        zone_queries = [q for q in fakes[0].sent_queries if q == bytes(generate_query(b'\x00'))]
        output_queries = [q for q in fakes[0].sent_queries if q == bytes(generate_query(b'\x17'))]
        assert len(zone_queries) >= 2, "dopytanie powinno się powtarzać, nie tylko raz po połączeniu"
        assert len(output_queries) >= 2

    async def test_stops_when_superseded_by_reconnect(self):
        """Zadanie NIE może żyć wiecznie po utracie aktualności - jeśli
        connect() (re)połączenia tworzy NOWY obiekt AsyncSatel, stare
        zadanie cyklicznej synchronizacji musi się samo zakończyć (inaczej
        dwa zadania wysyłałyby zapytania równolegle bez końca)."""
        from satel_integra.satel_integra import generate_query

        client = SatelIntegraClient("127.0.0.1", 7094, "key", monitored_zones={"Hol": 12})
        old_fake = FakeAsyncSatelInitialStatus("127.0.0.1", 7094, None)
        client._client = old_fake
        client._initial_status_delay_seconds = 0.01
        client.status_poll_interval_seconds = 0.02

        task = asyncio.create_task(client._periodic_status_sync(generate_query))
        await asyncio.sleep(0.07)  # kilka cykli na "starym" połączeniu
        count_before_swap = len(old_fake.sent_queries)
        assert count_before_swap > 0

        new_fake = FakeAsyncSatelInitialStatus("127.0.0.1", 7094, None)
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

    def test_status_poll_interval_seconds_defaults_to_15(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        assert client.status_poll_interval_seconds == 15.0


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
        from satel_integra.satel_integra import AlarmState

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
    (clear_alarm), bo centrala Satel trzyma ją jako osobną flagę."""

    def _client_with_triggered(self, triggered_partitions: list[int]):
        from satel_integra.satel_integra import AlarmState

        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            partition_states = {AlarmState.TRIGGERED: triggered_partitions}
            disarm_calls: list = []
            clear_alarm_calls: list = []

            async def disarm(self, code, partitions):
                self.disarm_calls.append((code, partitions))

            async def clear_alarm(self, code, partitions):
                self.clear_alarm_calls.append((code, partitions))

        client._client = FakeClient()
        return client

    async def test_sends_clear_alarm_when_partition_was_triggered(self, monkeypatch):
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real_sleep(0))
        client = self._client_with_triggered([1])
        await client.disarm(partitions=[1])
        assert client._client.disarm_calls == [("key", [1])]
        assert client._client.clear_alarm_calls == [("key", [1])]

    async def test_skips_clear_alarm_when_not_triggered(self):
        client = self._client_with_triggered([])
        await client.disarm(partitions=[1])
        assert client._client.disarm_calls == [("key", [1])]
        assert client._client.clear_alarm_calls == []

    async def test_skips_clear_alarm_when_different_partition_triggered(self):
        client = self._client_with_triggered([2])
        await client.disarm(partitions=[1])
        assert client._client.disarm_calls == [("key", [1])]
        assert client._client.clear_alarm_calls == []
