"""Testy dla bridge/satel_client.py - głównie test_connection() (podpowiedź
"Testuj połączenie" w kreatorze/panelu), bez prawdziwej centrali Satel."""
import asyncio
import contextlib

import pytest

from bridge.satel_client import SatelIntegraClient, SatelVirtualInputTrigger, discover_active_zones_and_outputs
from bridge.satel_client import test_connection as satel_test_connection
from bridge.satel_client import test_output as satel_test_output
from bridge.satel_client import test_virtual_input_http as satel_test_virtual_input_http
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
        # Odzwierciedla `AsyncSatel.connected` (właściwość: `_writer and
        # _reader`) - domyślnie stabilne połączenie (jak w prawdziwym
        # udanym scenariuszu), żeby watchdog w
        # discover_active_zones_and_outputs (patrz "busy_suspected") nie
        # fałszywie zgłaszał zajętej centrali w testach, które w ogóle
        # tego nie symulują.
        self.connected = connect_result

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

    async def test_repeated_disconnects_with_no_activity_add_busy_warning(self, monkeypatch):
        """Reprodukcja zgłoszonego przypadku: centrala Satel Integra
        obsługuje TYLKO JEDNEGO klienta integracji naraz - gdy bridge już
        trzyma połączenie, NASZE (drugie) połączenie jest przyjmowane na
        poziomie TCP, ale zrywane zaraz po pierwszej komendzie, w kółko
        (biblioteka ponawia WEWNĘTRZNIE, nigdy nie rzucając wyjątku) - bez
        watchdoga funkcja zwróciłaby puste wyniki nie do odróżnienia od
        "nikt fizycznie nie wyzwolił żadnej czujki"."""
        class Fake(FakeAsyncSatelDiscovery):
            _events = []

            async def monitor_status(self, zone_changed_callback=None, output_changed_callback=None):
                # `connected` opada od razu i zostaje False - jak przy
                # ciągłym zrywaniu połączenia przez centralę.
                self.connected = False
                await asyncio.sleep(999)

        import satel_integra.satel_integra as satel_module
        monkeypatch.setattr(satel_module, "AsyncSatel", lambda host, port, loop: Fake(host, port, loop))

        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.06)
        assert result["zones"] == []
        assert result["outputs"] == []
        assert "warning" in result
        assert "jednego klienta integracji" in result["warning"].lower()

    async def test_stable_connection_with_no_activity_has_no_busy_warning(self, monkeypatch):
        """Kontrast do testu wyżej: gdy połączenie jest stabilne (nie
        opada), brak aktywności NIE powinien dawać ostrzeżenia o zajętej
        centrali - to normalny, oczekiwany wynik (nikt nic nie wyzwolił)."""
        class Fake(FakeAsyncSatelDiscovery):
            _events = []

        import satel_integra.satel_integra as satel_module
        monkeypatch.setattr(satel_module, "AsyncSatel", lambda host, port, loop: Fake(host, port, loop))

        result = await discover_active_zones_and_outputs("127.0.0.1", 7094, timeout_seconds=0.06)
        assert result == {"zones": [], "outputs": []}


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


class TestConnectionWatchdog:
    """run_forever() - regresja dla realnie zaobserwowanego wzorca w logach:
    'Starting monitor_status loop' -> 'Got exception: N bytes read... Most
    likely the other side has disconnected!' -> 'Start monitoring failed,
    sleeping for 10s...' - biblioteka satel_integra, gdy start_monitoring()
    zaraz po connect() się nie powiedzie, PONAWIA połączenie WEWNĘTRZNIE, w
    nieskończoność, nigdy nie zwracając sterowania do run_forever() - bez
    watchdoga Supervisor (i system_health w panelu) nigdy by się o tym nie
    dowiedział. Watchdog obserwuje `self._client.connected` z ZEWNĄTRZ i
    wymusza pełne ponowne połączenie (cancel monitor_task), gdy widzi dwa
    kolejne 'nie' pod rząd (~10s)."""

    async def test_cancels_stuck_monitor_task_when_disconnected(self, monkeypatch):
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real_sleep(0))
        import satel_integra.satel_integra as satel_module

        class StuckFake(FakeAsyncSatelKeepAlive):
            """`connected` nie jest zdefiniowane (jak w reszcie fejków w tym
            pliku) - domyślne getattr(..., False) w watchdogu odzwierciedla
            więc dokładnie zaobserwowany scenariusz: biblioteka "utknęła"
            wewnątrz monitor_status(), które nigdy nie wraca samo z siebie."""

            def __init__(self, host, port, loop):
                super().__init__(host, port, loop)
                self.cancelled = False

            async def monitor_status(self, alarm_status_callback=None,
                                      zone_changed_callback=None, output_changed_callback=None):
                # Event, które nigdy się nie ustawi - w odróżnieniu od
                # asyncio.sleep(...), nie jest dotknięte spatchowaniem
                # asyncio.sleep wyżej, więc naprawdę "wisi" w miejscu, dopóki
                # watchdog go nie anuluje - dokładnie symulując bibliotekę
                # utkniętą wewnątrz własnej, wewnętrznej pętli ponawiania.
                self.monitor_status_called = True
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        fakes = []

        def fake_ctor(host, port, loop, monitored_zones=None, monitored_outputs=None):
            fake = StuckFake(host, port, loop)
            fakes.append(fake)
            return fake

        monkeypatch.setattr(satel_module, "AsyncSatel", fake_ctor)

        client = SatelIntegraClient("127.0.0.1", 7094, "key")
        task = asyncio.create_task(client.run_forever())
        await real_sleep(0.1)  # kilka obrotów pętli watchdoga (asyncio.sleep spatchowany na 0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert fakes, "AsyncSatel nigdy nie zostało utworzone"
        assert fakes[0].monitor_status_called is True
        assert fakes[0].cancelled is True


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

            def __init__(self):
                # Symuluje 0xEF ("command result") - patrz
                # SatelIntegraClient._send_and_confirm(), które na to czeka.
                self._command_status_event = asyncio.Event()
                self._command_status = False

            async def disarm(self, code, partitions):
                self.disarm_calls.append((code, partitions))
                self._command_status = {"error": "OK"}
                self._command_status_event.set()

            async def clear_alarm(self, code, partitions):
                self.clear_alarm_calls.append((code, partitions))
                self._command_status = {"error": "OK"}
                self._command_status_event.set()

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


class TestSendAndConfirm:
    """_send_and_confirm() - regresja dla zgłoszonego z realnej instalacji
    'nie uzbraja stref, ale poprawnie wyświetla ich stan' - biblioteka
    satel_integra sama NIE czeka na potwierdzenie komendy zapisu (0xEF), a
    arm()/disarm()/set_output() wcześniej uznawały samo wysłanie bajtów po
    sieci za sukces. Błędny kod użytkownika (satel.integration_key) albo
    odrzucenie komendy przez centralę kończyły się więc CISZĄ porażką -
    stąd czytanie stanu (broadcast/poll, patrz TestAlarmStatusCallback w
    test_rules_engine.py) działało poprawnie, a same komendy zapisu nie."""

    def _client_with_fake(self):
        client = SatelIntegraClient("127.0.0.1", 7094, "key")

        class FakeClient:
            def __init__(self):
                self._command_status_event = asyncio.Event()
                self._command_status = False

        client._client = FakeClient()
        return client

    async def test_succeeds_when_panel_confirms_ok(self):
        client = self._client_with_fake()

        async def send():
            client._client._command_status = {"error": "OK"}
            client._client._command_status_event.set()

        await client._send_and_confirm("test", send())  # nie powinno rzucić

    async def test_raises_when_panel_rejects_command(self):
        client = self._client_with_fake()

        async def send():
            client._client._command_status = {"error": "User code not found"}
            client._client._command_status_event.set()

        with pytest.raises(RuntimeError, match="User code not found"):
            await client._send_and_confirm("test", send())

    async def test_raises_connection_error_on_timeout(self):
        client = self._client_with_fake()

        async def send():
            pass  # nigdy nie ustawia _command_status_event - symuluje brak odpowiedzi centrali

        with pytest.raises(ConnectionError, match="nie potwierdziła"):
            await client._send_and_confirm("test", send(), timeout=0.05)


class FakeAsyncSatelOutput(FakeAsyncSatel):
    """Do testowania test_output() - tryb "library" wirtualnych wejść IP
    (patrz obszerny docstring bridge/satel_client.py) - jednorazowe
    połączenie + set_output + potwierdzenie (0xEF), jak
    SatelIntegraClient.set_output, tylko bez pełnego SatelIntegraClient."""

    def __init__(self, host, port, loop, confirm_error=None, hang_confirm=False, **kwargs):
        super().__init__(host, port, loop, **kwargs)
        self._command_status_event = asyncio.Event()
        self._command_status = None
        self._confirm_error = confirm_error
        self._hang_confirm = hang_confirm
        self.set_output_calls: list[tuple] = []

    async def connect(self):
        result = await super().connect()

        class FakeWriter:
            def close(self):
                pass
        self._writer = FakeWriter()
        return result

    async def set_output(self, code, output_id, state):
        self.set_output_calls.append((code, output_id, state))
        if not self._hang_confirm:
            self._command_status = {"error": self._confirm_error or "OK"}
            self._command_status_event.set()


class TestTestOutput:
    """test_output() - przycisk "TEST" obok pola klucza integracji
    (tryb "library")."""

    def _patch(self, monkeypatch, **kwargs):
        import satel_integra.satel_integra as satel_module
        monkeypatch.setattr(
            satel_module, "AsyncSatel",
            lambda host, port, loop: FakeAsyncSatelOutput(host, port, loop, **kwargs),
        )

    async def test_succeeds_and_calls_set_output_with_given_args(self, monkeypatch):
        self._patch(monkeypatch)
        await satel_test_output("127.0.0.1", 7094, "1234", 15, True)  # nie powinno rzucić wyjątku

    async def test_raises_connection_error_when_connect_fails(self, monkeypatch):
        self._patch(monkeypatch, connect_result=False)
        with pytest.raises(ConnectionError, match="127.0.0.1"):
            await satel_test_output("127.0.0.1", 7094, "1234", 15, True)

    async def test_raises_runtime_error_when_panel_rejects(self, monkeypatch):
        self._patch(monkeypatch, confirm_error="User code not found")
        with pytest.raises(RuntimeError, match="User code not found"):
            await satel_test_output("127.0.0.1", 7094, "bad", 15, True)

    async def test_raises_connection_error_on_confirm_timeout(self, monkeypatch):
        self._patch(monkeypatch, hang_confirm=True)
        with pytest.raises(ConnectionError, match="nie potwierdziła"):
            await satel_test_output("127.0.0.1", 7094, "1234", 15, True, timeout_seconds=0.05)


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
    patrz obszerny docstring bridge/satel_client.py)."""

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
