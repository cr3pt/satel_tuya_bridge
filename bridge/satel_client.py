"""Integracja z centralą Satel Integra przez moduł ETHM-1 Plus.

Dwa niezależne kanały:

1. `SatelIntegraClient` - protokół integracji Satela (biblioteka
   `satel_integra`, ta sama używana przez Home Assistant). Pozwala
   ODCZYTYWAĆ stan wejść/wyjść/stref oraz STEROWAĆ wyjściami i
   (roz)uzbrajaniem. Nie pozwala zapisywać stanu wejść.

2. `SatelVirtualInputTrigger` - osobny, prostszy kanał: funkcja
   "Wejścia IP" modułu ETHM-1 Plus (od firmware z INTEGRA 1.22+).
   Wysyłając żądanie HTTP GET na skonfigurowany w DLOADX port z
   właściwym kluczem, wywołujemy NARUSZENIE konkretnego wejścia -
   dokładnie to, czego potrzebujemy dla zdarzeń z Tuya.

   WAŻNE: moduł nie zwraca standardowego kodu HTTP 200 dla tych
   zapytań (potwierdzone przez Satela - nie planują tego poprawiać).
   Klient poniżej celowo ignoruje kod odpowiedzi i traktuje sam brak
   wyjątku sieciowego jako sukces.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

import aiohttp

from .resilience import Supervisor

logger = logging.getLogger("bridge.satel")


async def test_connection(host: str, port: int, timeout_seconds: float = 5.0) -> None:
    """Sprawdza, czy pod podanym adresem/portem w ogóle coś odpowiada po TCP
    (do kroku "Testuj połączenie" w kreatorze/panelu, ZANIM zapiszemy
    config.yaml) - nie zgłasza wyjątku przy sukcesie, rzuca ConnectionError
    z czytelnym komunikatem przy niepowodzeniu.

    UCZCIWIE: to test SAMEJ OSIĄGALNOŚCI hosta/portu, NIE klucza integracji.
    `AsyncSatel.connect()` tylko otwiera gniazdo TCP - klucz jest wysyłany
    dopiero przy konkretnych poleceniach (set_output/arm/disarm), których
    test świadomie nie wykonuje (nie chcemy niczego zmieniać w systemie
    alarmowym w ramach "testu"). Błędny klucz integracji ujawni się dopiero
    przy pierwszej próbie uzbrojenia/rozbrojenia/sterowania wyjściem."""
    # Import odroczony - jak w SatelIntegraClient.connect() (patrz niżej) -
    # żeby moduł dało się zaimportować nawet bez zainstalowanej biblioteki.
    from satel_integra.satel_integra import AsyncSatel  # type: ignore

    client = AsyncSatel(host, port, asyncio.get_event_loop())
    try:
        connected = await asyncio.wait_for(client.connect(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Przekroczono czas oczekiwania ({timeout_seconds:.0f}s) na połączenie z {host}:{port}"
        )
    finally:
        if client._writer is not None:
            client._writer.close()

    if not connected:
        raise ConnectionError(f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port")


async def discover_active_zones_and_outputs(host: str, port: int, timeout_seconds: float = 60.0) -> dict:
    """Best-effort 'wykrywanie' stref (wejść) i wyjść Satela, do kroku 1
    kreatora/panelu - PO PODANIU SAMEGO adresu IP/portu (klucz integracji
    NIE jest potrzebny, tak jak w test_connection() wyżej - monitorowanie
    statusu jest częścią protokołu integracji dostępną bez niego).

    UCZCIWIE, zanim to użyjesz: protokół integracji Satela NIE udostępnia
    listy WSZYSTKICH skonfigurowanych stref/wyjść wraz z nazwami - to
    ograniczenie samego protokołu/biblioteki `satel_integra`, którego nie
    da się obejść (stąd też brak przycisku "wykryj" w panelu /config, patrz
    tamtejszy komentarz). To, co REALNIE można zrobić: biblioteka zawsze
    parsuje PEŁNĄ maskę bitową naruszonych stref/aktywnych wyjść z każdej
    transmisji o zmianie stanu (`AsyncSatel.violated_zones`/
    `.violated_outputs`, niezależnie od tego, co jest "monitorowane") -
    więc nasłuchując przez `timeout_seconds` i notując numery, które w tym
    czasie ZMIENIŁY STAN, wykryjemy numery stref/wyjść, które użytkownik
    fizycznie wyzwoli w tym oknie (otworzy drzwi/okno, wywoła czujkę ruchu,
    załączy wyjście). Strefy, które NIE zmienią stanu podczas nasłuchu
    (np. zamknięte drzwi, które nikt nie otworzy), pozostaną niewykryte -
    to jest świadome ograniczenie tej funkcji, nie błąd.

    Zwraca {"zones": [numery...], "outputs": [numery...]} posortowane
    rosnąco. Rzuca ConnectionError przy niepowodzeniu połączenia."""
    from satel_integra.satel_integra import AsyncSatel  # type: ignore

    client = AsyncSatel(host, port, asyncio.get_event_loop())
    seen_zones: set[int] = set()
    seen_outputs: set[int] = set()

    def _on_zone(_status: dict) -> None:
        seen_zones.update(client.violated_zones)

    def _on_output(_status: dict) -> None:
        seen_outputs.update(client.violated_outputs)

    try:
        connected = await asyncio.wait_for(client.connect(), timeout=5.0)
    except asyncio.TimeoutError:
        raise ConnectionError(f"Przekroczono czas oczekiwania na połączenie z {host}:{port}")
    if not connected:
        raise ConnectionError(f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port")

    monitor_task = asyncio.ensure_future(
        client.monitor_status(zone_changed_callback=_on_zone, output_changed_callback=_on_output)
    )
    try:
        await asyncio.wait_for(asyncio.shield(monitor_task), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass  # oczekiwane - monitor_status() działa w nieskończoność, dopóki go nie przerwiemy
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except (asyncio.CancelledError, Exception):
            pass
        if client._writer is not None:
            client._writer.close()

    return {"zones": sorted(seen_zones), "outputs": sorted(seen_outputs)}


class SatelVirtualInputTrigger:
    """Wywołuje naruszenie wirtualnego wejścia IP w ETHM-1 Plus."""

    def __init__(self, host: str, cfg: dict):
        self.host = host
        self.port = cfg["port"]
        self.url_template = cfg["url_template"]
        self.inputs = cfg["inputs"]  # nazwa -> {number, key}

    async def trigger(self, input_name: str, retries: int = 1) -> None:
        if input_name not in self.inputs:
            logger.error("Nieznane wirtualne wejście: %s", input_name)
            return
        entry = self.inputs[input_name]
        url = self.url_template.format(
            host=self.host,
            port=self.port,
            key=entry["key"],
            input_no=entry["number"],
        )
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        # Celowo nie sprawdzamy resp.status == 200 - ETHM-1 Plus
                        # go nie zwraca w standardowej formie. Sam brak wyjątku
                        # przy wysyłce = uznajemy za wysłane.
                        await resp.read()
                logger.info(
                    "Wysłano naruszenie wirtualnego wejścia '%s' (nr %s)",
                    input_name, entry["number"],
                )
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout przy wywołaniu wejścia IP '%s' (próba %s/%s) - ETHM-1 Plus może "
                    "mimo to zarejestrować zdarzenie (brak potwierdzenia to znana cecha).",
                    input_name, attempt + 1, retries + 1,
                )
                return  # timeout nie oznacza porażki (patrz komentarz wyżej) - nie ponawiamy
            except aiohttp.ClientError as exc:
                if attempt < retries:
                    logger.warning(
                        "Błąd sieci przy wywołaniu wejścia IP '%s' (próba %s/%s): %s - ponawiam",
                        input_name, attempt + 1, retries + 1, exc,
                    )
                    await asyncio.sleep(1)
                else:
                    logger.error(
                        "Błąd sieci przy wywołaniu wejścia IP '%s' - wyczerpano próby: %s",
                        input_name, exc,
                    )


class SatelIntegraClient:
    """Cienki wrapper na bibliotekę `satel_integra` (odczyt stanu, sterowanie
    wyjściami, uzbrajanie/rozbrajanie). Instalacja: pip install satel-integra

    Zweryfikowane wg źródeł biblioteki (github.com/c-soft/satel_integra):
    - konstruktor: AsyncSatel(host, port, loop, monitored_zones=[], monitored_outputs=[], partitions=[])
    - atrybuty stanu: `violated_zones` (lista naruszonych wejść), `violated_outputs`
      (lista aktywnych wyjść), `partition_states` (stan stref)
    - terminologia Satela bywa myląca: to, co biblioteka nazywa "zone", to
      fizyczne WEJŚCIE (czujka) w systemie Integra, a "partition" to STREFA.
    """

    def __init__(self, host: str, port: int, integration_key: str, partition: int = 1,
                 monitored_zones: dict | None = None, monitored_outputs: dict | None = None,
                 history=None, notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.host = host
        self.port = port
        self.integration_key = integration_key
        self.partition = partition
        # nazwa -> numer, do etykietowania w panelu (np. {"Hol": 12, "Garaż": 13})
        self.monitored_zones = monitored_zones or {}
        self.monitored_outputs = monitored_outputs or {}
        self._client = None  # utworzony leniwie, patrz connect()
        self._zone_cb = None
        self._output_cb = None
        self._alarm_status_cb = None
        self._supervisor = Supervisor("satel", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)
        # Opóźnienie przed pierwszym dopytaniem o stan wejść/wyjść po
        # (re)połączeniu, oraz odstęp między kolejnymi dopytaniami - patrz
        # _periodic_status_sync(). Wydzielone jako atrybuty instancji
        # (zamiast stałych), żeby testy mogły je skrócić.
        self._initial_status_delay_seconds = 1.0
        self._status_sync_interval_seconds = 15.0

    async def connect(self) -> None:
        # Import odroczony - żeby moduł dało się zaimportować nawet bez
        # zainstalowanej biblioteki, jeśli ktoś używa tylko wirtualnych wejść.
        from satel_integra.satel_integra import AsyncSatel, AlarmState, generate_query  # type: ignore

        self._AlarmState = AlarmState
        self._client = AsyncSatel(
            self.host,
            self.port,
            asyncio.get_event_loop(),
            monitored_zones=list(self.monitored_zones.values()),
            monitored_outputs=list(self.monitored_outputs.values()),
        )
        connected = await self._client.connect()
        if not connected:
            raise ConnectionError(f"Nie udało się połączyć z Satelem pod {self.host}:{self.port}")
        # Callbacki trzeba ponownie podpiąć po każdym (re)połączeniu, bo
        # tworzymy nowy obiekt AsyncSatel - stare przypisania giną razem z nim.
        if self._zone_cb:
            self._client._zone_changed_callback = self._zone_cb
        if self._output_cb:
            self._client._output_changed_callback = self._output_cb
        if self._alarm_status_cb:
            self._client._alarm_status_callback = self._alarm_status_cb
        logger.info("Połączono z centralą Satel Integra (%s:%s)", self.host, self.port)

        # Biblioteka satel_integra wywołuje zone_changed_callback/
        # output_changed_callback TYLKO gdy centrala sama wyśle broadcast o
        # zmianie (funkcje 0x00/0x17) - na spokojnym systemie, gdzie nic się
        # nie narusza, może to nigdy nie nastąpić, więc panel/MQTT nie
        # dostają ŻADNEGO stanu aż do pierwszej faktycznej zmiany. GORZEJ:
        # zgłoszono z prawdziwej centrali, że zmiana stanu WYJŚCIA (0x17) w
        # ogóle nie dociera jako broadcast (w odróżnieniu od wejść) - część
        # central/wersji firmware ETHM-1 Plus nie wysyła go spontanicznie,
        # tylko w odpowiedzi na zapytanie (znane ograniczenie tej samej
        # biblioteki, używanej też przez integrację Home Assistant). Same
        # broadcasty więc NIE WYSTARCZAJĄ do rzetelnie "ciągłej"
        # synchronizacji panelu/MQTT ze stanem centrali.
        #
        # Funkcje 0x00 ("zones violation") i 0x17 ("outputs state") to
        # jednak też zwykłe polecenia ODCZYTU w protokole integracji -
        # centrala odpowiada tym samym typem wiadomości niezależnie, czy
        # przyszła jako broadcast, czy jako odpowiedź na nasze zapytanie, a
        # _zone_violated()/_output_changed() w bibliotece i tak zawsze
        # wywołują callback z pełnym bieżącym stanem WSZYSTKICH monitorowanych
        # wejść/wyjść. Dlatego dopytujemy CYKLICZNIE (nie tylko raz po
        # połączeniu) - patrz _periodic_status_sync() - żeby panel/MQTT
        # zawsze pokazywały aktualny stan, niezależnie od tego, czy broadcast
        # od centrali w ogóle nadejdzie.
        #
        # UWAGA na kolejność PIERWSZEGO zapytania: wysyłamy je z OPÓŹNIENIEM
        # (osobne zadanie w tle, nie tutaj synchronicznie) - zaraz po
        # connect() biblioteka sama wysyła własne polecenie startu nasłuchu
        # (monitor_status() -> start_monitoring()) i czyta DOKŁADNIE JEDNĄ
        # odpowiedź na nie. Gdyby nasze zapytania trafiły do kolejki PRZED
        # tamtym poleceniem, start_monitoring() błędnie potraktowałby naszą
        # odpowiedź jako potwierdzenie własnego polecenia (i zalogował
        # mylące "Monitoring not accepted."), a nasza odpowiedź przepadłaby
        # nieobsłużona. Krótkie opóźnienie daje pewność, że
        # start_monitoring() (pojedynczy odczyt, ~milisekundy w sieci
        # lokalnej) zdąży się zakończyć pierwszy.
        asyncio.ensure_future(self._periodic_status_sync(generate_query))
        return

    async def _periodic_status_sync(self, generate_query) -> None:
        """Aktywnie dopytuje o stan wejść/wyjść w regularnych odstępach
        (co self._status_sync_interval_seconds), NIE tylko raz zaraz po
        połączeniu - patrz obszerny komentarz w connect() wyżej. Zadanie
        samo się kończy, gdy to konkretne połączenie (`client`, przechwycone
        na starcie) zostanie zastąpione nowym w wyniku (re)połączenia -
        wtedy `self._client is client` przestaje być prawdą, a nowe
        wywołanie connect() uruchamia już WŁASNE zadanie dla nowego
        połączenia."""
        await asyncio.sleep(self._initial_status_delay_seconds)
        client = self._client
        while client is not None and client is self._client and not getattr(client, "closed", True):
            try:
                if self.monitored_zones:
                    await client._send_data(generate_query(b'\x00'))
                if self.monitored_outputs:
                    await client._send_data(generate_query(b'\x17'))
            except Exception:
                logger.exception("Nie udało się dopytać o stan wejść/wyjść Satela")
                return
            await asyncio.sleep(self._status_sync_interval_seconds)

    async def set_output(self, output_number: int, state: bool) -> None:
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony - wywołaj connect() najpierw")
        await self._client.set_output(self.integration_key, output_number, state)
        logger.info("Ustawiono wyjście %s na %s", output_number, state)

    async def arm(self, mode: int = 0, partitions: list[int] | None = None) -> None:
        """Uzbraja jedną lub kilka stref. `partitions` domyślnie = skonfigurowana
        strefa główna (`satel.partition`) - podaj listę, żeby uzbroić kilka
        naraz, np. [1, 2]. UWAGA: dokładny format argumentu stref w bibliotece
        satel_integra (lista vs. pojedynczy numer) może się różnić między
        wersjami pakietu - zweryfikuj przy pierwszym użyciu z realną centralą."""
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony")
        partitions = partitions or [self.partition]
        await self._client.arm(self.integration_key, partitions, mode)
        logger.info("Wysłano komendę uzbrojenia stref %s (tryb %s)", partitions, mode)

    async def disarm(self, partitions: list[int] | None = None) -> None:
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony")
        partitions = partitions or [self.partition]
        await self._client.disarm(self.integration_key, partitions)
        logger.info("Wysłano komendę rozbrojenia stref %s", partitions)

    def register_zone_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Rejestruje callback wywoływany przy zmianie stanu wejść (np. do
        reakcji 'ruch z Satela -> włącz gniazdko'). Zapisany na obiekcie
        klienta (nie tylko na chwilowym AsyncSatel), więc przetrwa
        automatyczne rozłączenia i ponowne łączenie w run_forever().

        UWAGA: nazwa atrybutu callbacku (`_zone_changed_callback`) jest
        prywatna w bibliotece - jeśli po aktualizacji pakietu przestanie
        działać, sprawdź aktualne API w satel_integra/satel_integra.py."""
        self._zone_cb = callback
        if self._client is not None:
            self._client._zone_changed_callback = callback

    def register_output_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Analogicznie do register_zone_callback, ale dla wyjść."""
        self._output_cb = callback
        if self._client is not None:
            self._client._output_changed_callback = callback

    def register_alarm_status_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Callback wywoływany przy zmianie stanu uzbrojenia stref (widoczny
        w panelu jako 'Strefy Satel') - oraz przy rozłączeniu (biblioteka
        używa go też jako powiadomienia "coś się zmieniło, sprawdź stan
        połączenia", patrz _read_data()/_update_status() w satel_integra.py).

        UWAGA: w odróżnieniu od register_zone_callback/register_output_callback
        callback NIE dostaje żadnego argumentu - biblioteka woła go jako
        `self._alarm_status_callback()` (patrz _armed()/_read_data()/
        _update_status() w satel_integra.py), samo bieżące partition_states
        trzeba odczytać z armed_partition_numbers niżej. Nazwa atrybutu jak
        wyżej - prywatna w bibliotece, zweryfikuj przy aktualizacji pakietu."""
        self._alarm_status_cb = callback
        if self._client is not None:
            self._client._alarm_status_callback = callback

    @property
    def violated_zones(self) -> list[int]:
        return list(self._client.violated_zones) if self._client else []

    @property
    def violated_outputs(self) -> list[int]:
        return list(self._client.violated_outputs) if self._client else []

    @property
    def armed_partition_numbers(self) -> set[int]:
        """Numery stref aktualnie uzbrojonych (w DOWOLNYM trybie - pełny,
        nocny/dzienny, z zawieszonymi czujkami, czas na wejście/wyjście,
        alarm - wszystkie to warianty "uzbrojona" z punktu widzenia panelu).
        AsyncSatel.partition_states to {AlarmState: [numery_stref]} -
        nadpisywane na bieżąco przy każdym komunikacie 0x0A/0x2A/... (patrz
        _armed() w satel_integra.py), więc suma po wszystkich trybach zawsze
        odzwierciedla aktualny stan - callback rejestrowany przez
        register_alarm_status_callback dostaje tylko powiadomienie "coś się
        zmieniło", nie sam stan."""
        if self._client is None:
            return set()
        numbers: set[int] = set()
        for partitions in self._client.partition_states.values():
            numbers.update(partitions)
        return numbers

    @property
    def is_connected(self) -> bool:
        return self._client is not None and not getattr(self._client, "closed", True)

    async def run_forever(self) -> None:
        """Utrzymuje połączenie z centralą z automatycznym ponawianiem po
        zerwaniu (exponential backoff, patrz bridge/resilience.py). Status
        'zdrowia' połączenia trafia do panelu (kategoria system_health) i
        do dziennika zdarzeń przy każdej zmianie stanu."""

        async def _connect_and_monitor(on_connected) -> None:
            await self.connect()
            await on_connected()
            # ETHM-1 Plus rozłącza połączenie integracji po ok. 25s
            # bezczynności (znana cecha centrali, nie błąd biblioteki -
            # patrz AsyncSatel.keep_alive() w satel_integra.py: "A
            # workaround for Satel Integra disconnecting after 25s") - BEZ
            # tego zadania w tle połączenie regularnie padało z komunikatem
            # "0 bytes read... Most likely the other side has
            # disconnected!" w logach, mimo że sama centrala/sieć działały
            # poprawnie. keep_alive() wysyła nieszkodliwe zapytanie o
            # status co ~20s, żeby centrala nie uznała połączenia za martwe.
            keep_alive_task = asyncio.ensure_future(self._client.keep_alive())
            try:
                # WAŻNE: monitor_status() w bibliotece PRZYPISUJE callbacki z
                # własnych argumentów na starcie (self._zone_changed_callback
                # = zone_changed_callback, ...) - wywołanie go BEZ
                # argumentów (jak poprzednio) NADPISYWAŁO wartością None to,
                # co connect() dopiero co ustawił przez register_*_callback()
                # (patrz wyżej), więc _on_zone_change/_on_output_change/
                # _on_alarm_status_change w RulesEngine NIGDY się nie
                # wywoływały - stąd zgłoszony brak stanów Satela na MQTT i w
                # panelu (poza wartościami zaszytymi na starcie). Trzeba
                # przekazać je jawnie przy każdym wywołaniu.
                #
                # monitor_status blokuje dopóki połączenie żyje; jego powrót
                # (normalny albo przez wyjątek) oznacza utratę połączenia
                await self._client.monitor_status(
                    alarm_status_callback=self._alarm_status_cb,
                    zone_changed_callback=self._zone_cb,
                    output_changed_callback=self._output_cb,
                )
            finally:
                keep_alive_task.cancel()
                try:
                    await keep_alive_task
                except (asyncio.CancelledError, Exception):
                    pass

        await self._supervisor.run(_connect_and_monitor)
