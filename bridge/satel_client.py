"""Integracja z centralą Satel Integra przez moduł ETHM-1 Plus.

Biblioteka: `satel_integra` >=1.4.0 (wymaga Python >=3.12 - patrz
Dockerfile/requirements.txt). Wcześniejsza wersja tego modułu była pisana
pod STARE, prywatne API 0.3.x (AsyncSatel(host, port, loop, ...),
monitor_status()/keep_alive() blokujące w pętli, `_command_status_event`/
`_command_status` do ręcznego czekania na potwierdzenie komendy,
`_writer`/`_reader` do zamykania połączenia) - satel_integra 1.0.0 to
KOMPLETNIE PRZEPISANE API (patrz CHANGELOG biblioteki:
github.com/c-soft/satel_integra/blob/master/CHANGELOG.md), z realnymi
usprawnieniami, które ten moduł teraz wykorzystuje:

- `register_callbacks(...)` + `await start(enable_monitoring=True)`
  zamiast blokującego `monitor_status(...)` - `start()` sam uruchamia i
  utrzymuje odczyt/keepalive/reconnect w tle (PUBLICZNE metody, nie
  prywatne atrybuty jak wcześniej).
- Wbudowany keep-alive (co 15s) i automatyczny reconnect z rozpoznawaniem
  "centrala zajęta" (`SatelPanelBusyError`, rzucany wprost przy connect())
  - usuwa potrzebę własnego watchdoga "sprawdzaj `connected` z zewnątrz co
    5s", który wcześniej odróżniał to od zwykłej awarii sieci heurystyką.
- Kolejka komend zapisu (`SatelMessageQueue`) - komendy przetwarzane są
  sekwencyjnie i CZEKAJĄ na odpowiedź, jeśli się o to poprosi
  (`_send_data_and_wait`) - PUBLICZNE metody komend (arm/disarm/
  set_output/clear_alarm) nadal idą fire-and-forget (`_send_data`), więc
  do potwierdzania komend nadal sięgamy do tej samej, technicznie
  prywatnej metody `_send_data_and_wait` (patrz `_wait_for_command_result`
  niżej) - ale kolejka i tak eliminuje ryzyko pomylenia odpowiedzi między
  RÓWNOCZESNYMI komendami, które musieliśmy wcześniej akceptować jako
  ryzyko przy `_command_status_event` współdzielonym na całym połączeniu.
- Szyfrowana transmisja (AES) - patrz `SatelIntegraClient.encryption_key`
  niżej, opcjonalne, wyłączone domyślnie (cleartext, jak dotychczas).

WAŻNE rozróżnienie dwóch RÓŻNYCH kluczy (łatwe do pomylenia, bo biblioteka
nazwała swój parametr konstruktora `integration_key`, tak samo jak nasze
dawne pole configu):
- `code`/`self.integration_key` w tym module - kod użytkownika ustawiony w
  DLOADX/GUARDX (numeryczny PIN), wysyłany PER KOMENDA zapisu (arm/
  disarm/set_output/clear_alarm) - to jest AUTORYZACJA komendy, dokładnie
  to samo pole `satel.integration_key` co zawsze w configu tego projektu.
- `AsyncSatel(..., integration_key=...)` w SAMEJ bibliotece - to KLUCZ
  SZYFROWANIA transmisji TCP (AES), ustawiany w DLOADX osobno, TYLKO jeśli
  centrala ma włączone szyfrowanie komunikacji integracji. NIEZWIĄZANY z
  kodem użytkownika. W tym module odpowiada mu OSOBNE pole
  `satel.encryption_key` (domyślnie brak = transmisja czystym tekstem,
  jak dotychczas) - podanie tu przez pomyłkę kodu użytkownika zamiast
  prawdziwego klucza szyfrowania ZERWIE połączenie (centrala bez włączonego
  szyfrowania nie rozumie zaszyfrowanych ramek).

"Wirtualne wejście IP" (nazwa w panelu na wywoływanie naruszenia z
automatyzacji, np. z Tuya) ma DWA niezależne sposoby wyzwolenia
(satel.virtual_inputs.mode w configu):

1. "library" (domyślny) - to zwykłe WYJŚCIE (numerowane 1-256, ten sam
   zakres co `monitored_outputs`), które instalator zaprogramował w
   DLOADX tak, żeby jego załączenie symulowało wewnątrz centrali
   naruszenie wejścia/czujki. Wyzwala się je DOKŁADNIE tym samym
   poleceniem co zwykłe wyjście - `SatelIntegraClient.set_output()` -
   przez TO SAMO połączenie integracji co reszta poleceń.

2. "http" (`SatelVirtualInputTrigger` niżej) - funkcja "Wejścia IP" ETHM-1:
   osobne, ZWYKŁE, NIEUWIERZYTELNIONE żądanie HTTP GET na skonfigurowany
   port modułu, `http://{host}:{port}/{nazwa}` - `{nazwa}` to WPROST nazwa
   wirtualnego wejścia (fragment ścieżki URL, NIE parametr zapytania),
   BEZ ŻADNEGO klucza/hasła/kodu DLOADX (potwierdzone przez użytkownika na
   prawdziwym sprzęcie - wcześniejsze wersje tego modułu błędnie zakładały
   klucz per wejście, potem błędnie wspólny klucz całego modułu; obie
   wersje były niepoprawne i zostały usunięte). WAŻNE, dlaczego to osobny
   tryb: centrala Satel Integra obsługuje TYLKO JEDNEGO klienta protokołu
   integracji naraz (port 7094) i kolejkuje na nim wszystkie polecenia
   zapisu - w trybie "library" wyzwolenie wirtualnego wejścia w trakcie
   naruszenia CZEKA w tej samej kolejce co inne polecenia (uzbrojenie,
   sterowanie innymi wyjściami). Tryb "http" idzie przez OSOBNE połączenie,
   niezależne od tej kolejki, kosztem portu HTTP modułu (dostępność samej
   funkcji zależy od wersji firmware). Moduł NIE zwraca standardowego kodu
   HTTP 200 dla tych zapytań (potwierdzone przez Satela - nie planują tego
   poprawiać) - klient poniżej celowo ignoruje kod odpowiedzi i traktuje
   sam brak wyjątku sieciowego jako sukces."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Awaitable

import aiohttp

from .resilience import Supervisor

logger = logging.getLogger("bridge.satel")

DEFAULT_VIRTUAL_INPUT_HTTP_PORT = 8888
DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE = "http://{host}:{port}/{name}"

# Kody błędu ramki odpowiedzi na komendę zapisu (RESULT, 0xEF) traktowane
# jako sukces - 0x00 to oczywiste "OK", 0xFF wygląda myląco (protokół
# Satela normalnie rezerwuje go na "inny błąd"), ALE zarówno satel_integra
# 0.3.x jak i 1.x (`_command_result()` w obu wersjach biblioteki) od
# zawsze traktują OBIE te wartości jako potwierdzenie - zachowujemy tę
# samą, sprawdzoną na prawdziwych centralach interpretację.
_RESULT_OK_CODES = (0x00, 0xFF)
_RESULT_ERROR_NAMES = {0x01: "nieznany kod użytkownika"}


def _describe_result_error(error_byte: int | None) -> str:
    if error_byte is None:
        return "brak danych w odpowiedzi"
    return _RESULT_ERROR_NAMES.get(error_byte, f"kod błędu 0x{error_byte:02X}")


async def _wait_for_command_result(client, action: str, msg, timeout: float = 5.0) -> None:
    """Wysyła komendę zapisu (SatelWriteMessage - arm/disarm/set_output/
    clear_alarm) przez wewnętrzną kolejkę biblioteki i CZEKA na odpowiedź
    (ramka RESULT), zamiast - jak PUBLICZNE metody komend w bibliotece
    (`arm()`/`disarm()`/`set_output()`/`clear_alarm()`) - uznawać samo
    dodanie do kolejki za sukces (patrz obszerny docstring modułu: te
    metody ZAWSZE idą przez `_send_data`, fire-and-forget). BEZ TEGO:
    błędny kod użytkownika (satel.integration_key) albo odrzucenie
    komendy przez centralę kończyło się CISZĄ porażką - w logach/panelu
    komenda wyglądała na wykonaną, a fizycznie nic się nie działo
    (zgłoszone z prawdziwej instalacji: "nie uzbraja stref, ale poprawnie
    wyświetla ich stan" - to drugie działa, bo odczyt stanu w ogóle nie
    przechodzi przez potwierdzanie).

    UWAGA: `client._send_data_and_wait` jest technicznie prywatną metodą
    biblioteki (podobnie jak `_command_status_event`/`_command_status` w
    poprzedniej wersji 0.3.x, na którą był napisany poprzedni wariant tej
    funkcji) - biblioteka NIE udostępnia PUBLICZNEGO sposobu potwierdzania
    komend zapisu. To cienki wrapper na `client._queue.add_message(msg,
    True)` (satel_integra/queue.py) - jeśli po aktualizacji pakietu
    przestanie działać, sprawdź aktualne API tam."""
    try:
        result = await asyncio.wait_for(client._send_data_and_wait(msg), timeout=timeout)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Centrala nie potwierdziła komendy ({action}) w ciągu {timeout:.0f}s - "
            f"sprawdź połączenie z centralą"
        )
    if result is None:
        # add_message() sama zwraca None też przy WEWNĘTRZNYM timeout/
        # anulowaniu (patrz satel_integra/queue.py: MESSAGE_RESPONSE_TIMEOUT
        # = 5s) - w normalnych warunkach nasz zewnętrzny timeout wyżej
        # zdąży się wyzwolić pierwszy, ale traktujemy to tak samo na
        # wszelki wypadek (np. gdyby ktoś skrócił `timeout` poniżej 5s).
        raise ConnectionError(f"Centrala nie potwierdziła komendy ({action}) - brak odpowiedzi")
    error_byte = result.msg_data[0] if result.msg_data else None
    if error_byte not in _RESULT_OK_CODES:
        raise RuntimeError(f"Centrala odrzuciła komendę ({action}): {_describe_result_error(error_byte)}")


async def test_connection(host: str, port: int, timeout_seconds: float = 5.0,
                           encryption_key: str | None = None) -> None:
    """Sprawdza, czy centrala pod podanym adresem/portem w ogóle odpowiada
    (do kroku "Testuj połączenie" w kreatorze/panelu, ZANIM zapiszemy
    config.yaml) - nie zgłasza wyjątku przy sukcesie, rzuca ConnectionError
    z czytelnym komunikatem przy niepowodzeniu.

    satel_integra >=1.0.0 sam wykonuje pełną weryfikację połączenia przy
    connect() (sprawdza, czy centrala nie jest już zajęta przez inny
    klient integracji, i odpowiada na sondę protokołu) - dokładniejszy
    test niż poprzednia wersja (0.3.x, samo otwarcie gniazda TCP), WCIĄŻ
    bez wysyłania kodu użytkownika (`satel.integration_key`, patrz
    docstring modułu) - sonda protokołu to zapytanie tylko do odczytu
    (RTC_AND_STATUS), nieautoryzowane. Błędny kod użytkownika ujawni się
    dopiero przy pierwszej próbie uzbrojenia/rozbrojenia/sterowania
    wyjściem.

    `encryption_key` - opcjonalny klucz szyfrowania transmisji (patrz
    obszerny docstring modułu - to NIE jest kod użytkownika), tylko jeśli
    centrala ma włączone szyfrowanie komunikacji integracji."""
    from satel_integra import AsyncSatel  # type: ignore
    from satel_integra.exceptions import (  # type: ignore
        SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
    )

    client = AsyncSatel(host, port, integration_key=encryption_key)
    try:
        await asyncio.wait_for(client.connect(raise_exceptions=True), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Przekroczono czas oczekiwania ({timeout_seconds:.0f}s) na połączenie z {host}:{port}"
        )
    except SatelPanelBusyError:
        raise ConnectionError(
            f"Centrala {host}:{port} zajęta - inny klient integracji (DLOADX/GUARDX albo "
            "druga instancja bridge'a) jest już podłączony. Centrala Satel Integra obsługuje "
            "TYLKO JEDNEGO klienta integracji naraz."
        )
    except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
        raise ConnectionError(
            f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port "
            f"(jeśli centrala ma włączone szyfrowanie, sprawdź też klucz szyfrowania): {exc}"
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def discover_active_zones_and_outputs(host: str, port: int, timeout_seconds: float = 60.0,
                                             encryption_key: str | None = None) -> dict:
    """Best-effort 'wykrywanie' stref (wejść) i wyjść Satela, do kroku 1
    kreatora/panelu - PO PODANIU SAMEGO adresu IP/portu (kod użytkownika
    NIE jest potrzebny, tak jak w test_connection() wyżej - monitorowanie
    statusu jest częścią protokołu integracji dostępną bez niego; klucz
    szyfrowania JEST potrzebny, jeśli centrala ma je włączone, bo bez
    niego transmisja w ogóle się nie odszyfruje - stąd `encryption_key`
    niżej).

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
    rosnąco. Rzuca ConnectionError przy niepowodzeniu połączenia (w tym
    gdy centrala jest już zajęta - satel_integra >=1.0.0 rozpoznaje to
    JEDNOZNACZNIE i natychmiast przy connect(), patrz SatelPanelBusyError
    niżej - poprzednia wersja 0.3.x nie miała takiego wyjątku, więc trzeba
    było zgadywać po serii rozłączeń podczas nasłuchu)."""
    from satel_integra import AsyncSatel  # type: ignore
    from satel_integra.exceptions import (  # type: ignore
        SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
    )

    client = AsyncSatel(host, port, integration_key=encryption_key)
    seen_zones: set[int] = set()
    seen_outputs: set[int] = set()

    def _on_zone(_status: dict) -> None:
        seen_zones.update(client.violated_zones)

    def _on_output(_status: dict) -> None:
        seen_outputs.update(client.violated_outputs)

    client.register_callbacks(zone_changed_callback=_on_zone, output_changed_callback=_on_output)

    try:
        await asyncio.wait_for(client.connect(raise_exceptions=True), timeout=5.0)
    except asyncio.TimeoutError:
        raise ConnectionError(f"Przekroczono czas oczekiwania na połączenie z {host}:{port}")
    except SatelPanelBusyError:
        raise ConnectionError(
            f"Centrala {host}:{port} zajęta - inny klient integracji już podłączony. Centrala "
            "Satel Integra obsługuje TYLKO JEDNEGO klienta integracji naraz. Zatrzymaj na chwilę "
            "kontener/proces bridge'a (docker compose stop bridge) i spróbuj ponownie."
        )
    except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
        raise ConnectionError(f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port: {exc}")

    try:
        await client.start(enable_monitoring=True)
        await asyncio.sleep(timeout_seconds)
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return {"zones": sorted(seen_zones), "outputs": sorted(seen_outputs)}


class BackgroundZoneOutputScan:
    """Nasłuch aktywnych stref/wyjść (discover_active_zones_and_outputs()
    wyżej) W TLE, w OSOBNYM WĄTKU - zgłoszony problem: wywołanie
    `asyncio.run(discover_active_zones_and_outputs(...))` WPROST w
    widoku Flask blokuje wątek waitressa (domyślnie 8, patrz
    dashboard/app.py) na cały czas nasłuchu (do 120s, patrz `timeout_seconds`
    w /api/plugins/satel/scan/start). Ten sam wzorzec "start/status" co
    `BackgroundTopicDiscovery` (bridge/ebus_client.py) dla nasłuchu MQTT
    eBUS - tu jednak trzeba samemu odpalić WŁASNĄ pętlę asyncio w wątku
    (`asyncio.run()` w `_run()` niżej), bo w odróżnieniu od paho-mqtt
    (wbudowana obsługa wątkowa) satel_integra/AsyncSatel jest czystym
    asyncio bez własnego wątku.

    W odróżnieniu od BackgroundTopicDiscovery: connect() NIE jest
    walidowany synchronicznie w start() (discover_active_zones_and_outputs
    robi connect+nasłuch+close jako JEDNĄ korutynę, więc rozdzielenie
    wymagałoby przebudowy tamtej funkcji) - błędy połączenia (zły host/
    port, centrala zajęta, brak biblioteki) trafiają więc do pola `error`
    dopiero w status(), NIE od razu w odpowiedzi start(). Jedna instancja
    obsługuje JEDEN nasłuch naraz - kolejny start() w trakcie trwającego
    nasłuchu rzuca RuntimeError."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._started_at: float | None = None
        self._duration_seconds: float = 0.0
        self._result: dict | None = None
        self._error: str | None = None

    def start(self, host: str, port: int, timeout_seconds: float = 30.0,
              encryption_key: str | None = None) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("Nasłuch już trwa - poczekaj na koniec i spróbuj ponownie")
            self._running = True
            self._started_at = time.time()
            self._duration_seconds = timeout_seconds
            self._result = None
            self._error = None

        def _run() -> None:
            try:
                result = asyncio.run(discover_active_zones_and_outputs(
                    host, port, timeout_seconds=timeout_seconds, encryption_key=encryption_key,
                ))
            except ImportError:
                error = "Biblioteka satel_integra nie jest zainstalowana na serwerze"
                result = None
            except ConnectionError as exc:
                error = str(exc)
                result = None
            except Exception as exc:
                error = f"Błąd wykrywania: {exc}"
                result = None
            else:
                error = None
            with self._lock:
                self._result = result
                self._error = error
                self._running = False

        threading.Thread(target=_run, daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self._started_at) if self._started_at is not None else 0.0
            return {
                "running": self._running,
                "elapsed_seconds": round(elapsed, 1),
                "duration_seconds": self._duration_seconds,
                "result": self._result,
                "error": self._error,
            }


async def test_output(host: str, port: int, integration_key: str, output_number: int,
                       state: bool = True, timeout_seconds: float = 5.0,
                       encryption_key: str | None = None) -> None:
    """Jednorazowy test wyjścia (m.in. "wirtualnego wejścia IP", które na tej
    instalacji jest zwykłym wyjściem zaprogramowanym w DLOADX do symulacji
    naruszenia - patrz docstring modułu) - do przycisku "TEST" obok pola
    klucza integracji w panelu (⚙ Ustawienia -> Połączenia). Łączy się,
    wysyła set_output i CZEKA na potwierdzenie z centrali, po czym się
    rozłącza - nie zostawia żadnego trwałego połączenia/zadania w tle (w
    odróżnieniu od pełnego `SatelIntegraClient.connect()`, który dodatkowo
    startuje monitorowanie/keepalive przez `client.start()`). Rzuca
    ConnectionError/RuntimeError przy niepowodzeniu (brak połączenia /
    centrala odrzuciła komendę - np. zły kod użytkownika)."""
    from satel_integra import AsyncSatel  # type: ignore
    from satel_integra.commands import SatelWriteCommand  # type: ignore
    from satel_integra.exceptions import (  # type: ignore
        SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
    )
    from satel_integra.messages import SatelWriteMessage  # type: ignore

    client = AsyncSatel(host, port, integration_key=encryption_key)
    try:
        await asyncio.wait_for(client.connect(raise_exceptions=True), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"Przekroczono czas oczekiwania ({timeout_seconds:.0f}s) na połączenie z {host}:{port}"
        )
    except SatelPanelBusyError:
        raise ConnectionError(
            f"Centrala {host}:{port} zajęta - inny klient integracji już podłączony."
        )
    except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
        raise ConnectionError(f"Nie udało się połączyć z {host}:{port} - sprawdź adres IP i port: {exc}")

    try:
        # Kolejka komend (patrz obszerny docstring modułu) startuje dopiero
        # wewnątrz start() - bez tego _send_data_and_wait() w
        # _wait_for_command_result() czekałby w nieskończoność, bo nic nie
        # przetwarzałoby kolejki. `enable_monitoring=False` - jednorazowy
        # test nie potrzebuje monitorowania stanu wejść/wyjść.
        await client.start(enable_monitoring=False)
        mode_command = SatelWriteCommand.OUTPUTS_ON if state else SatelWriteCommand.OUTPUTS_OFF
        msg = SatelWriteMessage(mode_command, code=integration_key, zones_or_outputs=[output_number])
        await _wait_for_command_result(client, f"test wyjścia {output_number} -> {state}", msg,
                                        timeout=timeout_seconds)
        logger.info("Test wyjścia %s -> %s zakończony sukcesem", output_number, state)
    finally:
        try:
            await client.close()
        except Exception:
            pass


class SatelVirtualInputTrigger:
    """Tryb "http" wirtualnych wejść IP (patrz docstring modułu, punkt 2) -
    funkcja "Wejścia IP" ETHM-1, OSOBNE połączenie HTTP, niezależne od
    kolejki poleceń protokołu integracji (SatelIntegraClient). Nie korzysta
    z biblioteki `satel_integra` w ogóle - czysty aiohttp. Zwykły,
    NIEUWIERZYTELNIONY GET - `{name}` w url_template to WPROST nazwa
    wejścia, BEZ żadnego klucza/kodu DLOADX (patrz obszerny docstring
    modułu wyżej)."""

    def __init__(self, host: str, cfg: dict):
        self.host = host
        self.port = cfg.get("port", DEFAULT_VIRTUAL_INPUT_HTTP_PORT)
        # CELOWO stała, NIE cfg.get("url_template", ...) - to szczegół
        # protokołu, żaden formularz nigdy go nie edytował, więc trzymanie
        # go w configu tylko groziło (i groziło naprawdę - zgłoszony błąd)
        # zamrożeniem STAREGO formatu URL (sprzed poprawki protokołu) w
        # instalacjach, które zapisały connection PRZED tą poprawką.
        self.url_template = DEFAULT_VIRTUAL_INPUT_URL_TEMPLATE
        self.inputs = cfg.get("inputs", {})  # nazwa -> numer wejścia (int, nieużywany w trybie http)

    async def trigger(self, input_name: str, retries: int = 1) -> None:
        if input_name not in self.inputs:
            logger.error("Nieznane wirtualne wejście IP (tryb http): %s", input_name)
            return
        url = self.url_template.format(host=self.host, port=self.port, name=input_name)
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        # Celowo nie sprawdzamy resp.status == 200 - ETHM-1
                        # go nie zwraca w standardowej formie. Sam brak wyjątku
                        # przy wysyłce = uznajemy za wysłane.
                        await resp.read()
                logger.info(
                    "Wysłano naruszenie wirtualnego wejścia '%s' (tryb http)",
                    input_name,
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


async def test_virtual_input_http(host: str, port: int, url_template: str, name: str,
                                   timeout_seconds: float = 5.0) -> None:
    """Jednorazowy test trybu "http" wirtualnych wejść IP - do przycisku
    "TEST" w panelu, gdy satel.virtual_inputs.mode == "http". W odróżnieniu
    od test_output() wyżej (tryb "library") NIE czeka na żadne
    potwierdzenie z centrali - ETHM-1 go nie zwraca (patrz docstring
    modułu) - sam brak wyjątku sieciowego = sukces. Rzuca ConnectionError
    przy niepowodzeniu."""
    url = url_template.format(host=host, port=port, name=name)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                await resp.read()
    except asyncio.TimeoutError:
        raise ConnectionError(f"Przekroczono czas oczekiwania ({timeout_seconds:.0f}s) na {host}:{port}")
    except aiohttp.ClientError as exc:
        raise ConnectionError(f"Błąd sieci przy wywołaniu wejścia IP: {exc}")


class SatelIntegraClient:
    """Cienki wrapper na bibliotekę `satel_integra` >=1.0.0 (odczyt stanu,
    sterowanie wyjściami, uzbrajanie/rozbrajanie). Instalacja: pip install
    satel-integra>=1.4.0 (wymaga Python >=3.12).

    Zweryfikowane wg źródeł biblioteki (github.com/c-soft/satel_integra,
    tag 1.4.0):
    - konstruktor: AsyncSatel(host, port, monitored_zones=[],
      monitored_outputs=[], partitions=[], integration_key=None) - BEZ
      `loop` (usunięty w 1.0.0). `integration_key` konstruktora to KLUCZ
      SZYFROWANIA, nie kod użytkownika - patrz obszerny docstring modułu
      i `self.encryption_key` niżej.
    - atrybuty stanu: `violated_zones` (lista naruszonych wejść),
      `violated_outputs` (lista aktywnych wyjść), `partition_states`
      ({AlarmState: [numery_stref]}) - bez zmian względem 0.3.x.
    - terminologia Satela bywa myląca: to, co biblioteka nazywa "zone", to
      fizyczne WEJŚCIE (czujka) w systemie Integra, a "partition" to STREFA.
    """

    def __init__(self, host: str, port: int, integration_key: str, partition: int = 1,
                 monitored_zones: dict | None = None, monitored_outputs: dict | None = None,
                 virtual_inputs: dict | None = None, encryption_key: str | None = None,
                 history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
                 status_poll_interval_seconds: float = 3.0):
        self.host = host
        self.port = port
        # Kod użytkownika (DLOADX/GUARDX) - wysyłany PER KOMENDA zapisu
        # (arm/disarm/set_output/clear_alarm), autoryzuje sterowanie. NIE
        # mylić z encryption_key niżej - patrz obszerny docstring modułu.
        self.integration_key = integration_key
        # Opcjonalny klucz SZYFROWANIA transmisji TCP (AES) - domyślnie
        # None = cleartext, jak dotychczas. Ustaw TYLKO jeśli centrala ma
        # jawnie włączone szyfrowanie komunikacji integracji w DLOADX -
        # podanie błędnego/niepasującego klucza (albo klucza, gdy centrala
        # go nie oczekuje) zrywa połączenie, nie "po prostu nie działa".
        self.encryption_key = encryption_key
        self.partition = partition
        # nazwa -> numer, do etykietowania w panelu (np. {"Hol": 12, "Garaż": 13})
        self.monitored_zones = monitored_zones or {}
        self.monitored_outputs = monitored_outputs or {}
        # nazwa -> numer wyjścia, dla "wirtualnych wejść IP" (patrz docstring
        # modułu) - osobna mapa od monitored_outputs, bo te wyjścia zwykle
        # NIE są jednocześnie monitorowane/wyświetlane jako zwykłe wyjścia.
        self.virtual_inputs = virtual_inputs or {}
        self._client = None  # utworzony leniwie, patrz connect()
        self._zone_cb = None
        self._output_cb = None
        self._alarm_status_cb = None
        self._supervisor = Supervisor("satel", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)
        # Odstęp między kolejnymi dopytaniami o stan wejść/wyjść - patrz
        # _periodic_status_sync(). PUBLICZNY atrybut (nie z podkreślnikiem) -
        # celowo, żeby RulesEngine.reload_config() mogło go podmienić NA
        # ŻYWO (pętla w _periodic_status_sync() czyta go na bieżąco przy
        # każdym obrocie), bez restartu procesu - w odróżnieniu od host/
        # portu/kluczy, których zmiana wciąż wymaga restartu (nowe
        # połączenie TCP).
        self.status_poll_interval_seconds = status_poll_interval_seconds
        # Opóźnienie przed PIERWSZYM dopytaniem po (re)połączeniu - osobne
        # od odstępu między kolejnymi, i celowo NIE konfigurowalne z panelu
        # (to tylko krótki bufor, żeby monitorowanie zdążyło wystartować -
        # w odróżnieniu od 0.3.x, kolejka komend biblioteki 1.x i tak
        # sekwencjonuje poprawnie nawet bez tego, patrz docstring modułu).
        self._initial_status_delay_seconds = 1.0

    async def connect(self) -> None:
        # Import odroczony - żeby moduł dało się zaimportować nawet bez
        # zainstalowanej biblioteki, jeśli ktoś używa tylko wirtualnych wejść.
        from satel_integra import AsyncSatel, AlarmState  # type: ignore
        from satel_integra.exceptions import (  # type: ignore
            SatelConnectFailedError, SatelConnectionInitializationError, SatelPanelBusyError,
        )

        self._AlarmState = AlarmState
        self._client = AsyncSatel(
            self.host,
            self.port,
            monitored_zones=list(self.monitored_zones.values()),
            monitored_outputs=list(self.monitored_outputs.values()),
            integration_key=self.encryption_key,
        )
        # Callbacki rejestrujemy PRZED connect()/start() (jak w oficjalnym
        # przykładzie migracji biblioteki) - trzeba je podpiąć na nowo po
        # KAŻDYM (re)połączeniu, bo tworzymy nowy obiekt AsyncSatel - stare
        # przypisania giną razem z nim. register_callbacks() to PUBLICZNA
        # metoda biblioteki 1.x (w 0.3.x trzeba było przypisywać prywatne
        # atrybuty `_zone_changed_callback` itp. bezpośrednio).
        self._client.register_callbacks(
            alarm_status_callback=self._alarm_status_cb,
            zone_changed_callback=self._zone_cb,
            output_changed_callback=self._output_cb,
        )
        try:
            await self._client.connect(raise_exceptions=True)
        except SatelPanelBusyError as exc:
            raise ConnectionError(
                f"Centrala {self.host}:{self.port} zajęta - najprawdopodobniej inny klient "
                "integracji (DLOADX/GUARDX albo druga instancja bridge'a) jest już podłączony. "
                "Centrala Satel Integra obsługuje TYLKO JEDNEGO klienta integracji naraz."
            ) from exc
        except (SatelConnectFailedError, SatelConnectionInitializationError) as exc:
            raise ConnectionError(
                f"Nie udało się połączyć z Satelem pod {self.host}:{self.port} "
                f"(jeśli centrala ma włączone szyfrowanie, sprawdź też klucz szyfrowania): {exc}"
            ) from exc
        logger.info("Połączono z centralą Satel Integra (%s:%s)", self.host, self.port)

        # start() URUCHAMIA i SAMA UTRZYMUJE odczyt/keepalive/reconnect w
        # tle (patrz obszerny docstring modułu) - w odróżnieniu od 0.3.x,
        # gdzie run_forever() (niżej) musiało to robić ręcznie (osobne
        # zadanie keep_alive(), blokujący monitor_status() z własnym
        # watchdogiem wykrywającym "cichy" wewnętrzny reconnect). Nie
        # blokuje - wraca od razu po wystartowaniu zadań w tle.
        await self._client.start(enable_monitoring=True)

        # Biblioteka wywołuje zone_changed_callback/output_changed_callback
        # TYLKO gdy centrala sama wyśle broadcast o zmianie (funkcje 0x00/
        # 0x17) - na spokojnym systemie, gdzie nic się nie narusza, może to
        # nigdy nie nastąpić, więc panel/MQTT nie dostają ŻADNEGO stanu aż
        # do pierwszej faktycznej zmiany. GORZEJ: zgłoszono z prawdziwej
        # centrali, że zmiana stanu WYJŚCIA (0x17) w ogóle nie dociera jako
        # broadcast (w odróżnieniu od wejść) - część central/wersji
        # firmware ETHM-1 Plus nie wysyła go spontanicznie, tylko w
        # odpowiedzi na zapytanie. Same broadcasty więc NIE WYSTARCZAJĄ do
        # rzetelnie "ciągłej" synchronizacji panelu/MQTT ze stanem centrali
        # - dlatego dopytujemy CYKLICZNIE, patrz _periodic_status_sync().
        asyncio.ensure_future(self._periodic_status_sync())
        return

    async def _periodic_status_sync(self) -> None:
        """Aktywnie dopytuje o stan wejść/wyjść w regularnych odstępach
        (co self.status_poll_interval_seconds - odczytywane NA ŚWIEŻO przy
        każdym obrocie pętli, więc RulesEngine.reload_config() może
        podmienić tę wartość na żywo, bez restartu), NIE tylko raz zaraz po
        połączeniu - patrz obszerny komentarz w connect() wyżej. Zapytania
        idą przez tę samą kolejkę komend biblioteki co reszta poleceń
        (fire-and-forget, `_send_data`) - w odróżnieniu od 0.3.x, kolejka
        w 1.x sama sekwencjonuje je poprawnie względem innych poleceń w
        locie (np. start_monitoring() przy połączeniu), więc nie trzeba
        już ręcznie unikać kolizji odczytu potwierdzeń. Zadanie samo się
        kończy, gdy to konkretne połączenie (`client`, przechwycone na
        starcie) zostanie zastąpione nowym w wyniku (re)połączenia - wtedy
        `self._client is client` przestaje być prawdą, a nowe wywołanie
        connect() uruchamia już WŁASNE zadanie dla nowego połączenia."""
        from satel_integra.commands import SatelReadCommand  # type: ignore
        from satel_integra.messages import SatelWriteMessage  # type: ignore

        await asyncio.sleep(self._initial_status_delay_seconds)
        client = self._client
        while client is not None and client is self._client and not getattr(client, "stopped", True):
            try:
                if self.monitored_zones:
                    await client._send_data(SatelWriteMessage(SatelReadCommand.ZONES_VIOLATED))
                if self.monitored_outputs:
                    await client._send_data(SatelWriteMessage(SatelReadCommand.OUTPUTS_STATE))
            except Exception:
                # ZGŁOSZONY BŁĄD: pojedynczy nieudany zapis (chwilowa
                # zadyszka sieci) wcześniej TRWALE zatrzymywał tę pętlę
                # (return) - połączenie TCP/keep-alive (Supervisor "satel")
                # zostawało "połączono" na zawsze, ale ŻADNE kolejne
                # dopytanie już nigdy by nie poszło, więc panel przestawał
                # widzieć zmiany stanu wyjść/wejść (patrz komentarz w
                # connect() o centralach, które nie wysyłają broadcastu
                # 0x17 spontanicznie) - do następnego (re)połączenia, które
                # mogło nie nastąpić przez bardzo długi czas. Logujemy i
                # próbujemy dalej przy następnym obrocie pętli zamiast
                # poddawać się na stałe po jednym błędzie.
                logger.exception("Nie udało się dopytać o stan wejść/wyjść Satela - ponowię próbę")
            await asyncio.sleep(self.status_poll_interval_seconds)

    async def set_output(self, output_number: int, state: bool) -> None:
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony - wywołaj connect() najpierw")
        if not self.integration_key:
            # Bez tego SatelWriteMessage.__init__ POMIJA całkiem segment kodu
            # w ramce (jego `if code:` jest fałszywe dla pustego stringa),
            # zamiast wypełnić go zerowym/domyślnym kodem - wysłałaby więc
            # do centrali OKALECZONĄ ramkę zamiast czytelnie odrzuconej
            # komendy. Lepiej odmówić tutaj, z czytelnym komunikatem.
            raise RuntimeError(
                "Sterowanie wymaga klucza integracji Satela - ustaw go w zakładce "
                "⚙ Ustawienia -> 🔌 Połączenia (bez klucza panel działa tylko do odczytu)."
            )
        from satel_integra.commands import SatelWriteCommand  # type: ignore
        from satel_integra.messages import SatelWriteMessage  # type: ignore

        mode_command = SatelWriteCommand.OUTPUTS_ON if state else SatelWriteCommand.OUTPUTS_OFF
        msg = SatelWriteMessage(mode_command, code=self.integration_key, zones_or_outputs=[output_number])
        await _wait_for_command_result(self._client, f"wyjście {output_number} -> {state}", msg)
        logger.info("Ustawiono wyjście %s na %s", output_number, state)

    async def arm(self, mode: int = 0, partitions: list[int] | None = None) -> None:
        """Uzbraja jedną lub kilka stref. `partitions` domyślnie = skonfigurowana
        strefa główna (`satel.partition`) - podaj listę, żeby uzbroić kilka
        naraz, np. [1, 2]."""
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony")
        if not self.integration_key:
            # Patrz komentarz w set_output() wyżej - bez klucza SatelWriteMessage
            # wysłałaby okaleczoną ramkę zamiast czytelnie odrzuconej komendy.
            raise RuntimeError(
                "Sterowanie wymaga klucza integracji Satela - ustaw go w zakładce "
                "⚙ Ustawienia -> 🔌 Połączenia (bez klucza panel działa tylko do odczytu)."
            )
        from satel_integra.commands import SatelWriteCommand  # type: ignore
        from satel_integra.messages import SatelWriteMessage  # type: ignore

        partitions = partitions or [self.partition]
        mode_command = SatelWriteCommand(SatelWriteCommand.PARTITIONS_ARM_MODE_0 + mode)
        msg = SatelWriteMessage(mode_command, code=self.integration_key, partitions=partitions)
        await _wait_for_command_result(self._client, f"uzbrojenie stref {partitions} (tryb {mode})", msg)
        logger.info("Uzbrojono strefy %s (tryb %s)", partitions, mode)

    async def disarm(self, partitions: list[int] | None = None) -> None:
        """Rozbraja strefy. Jeśli którakolwiek z nich jest akurat w stanie
        TRIGGERED/TRIGGERED_FIRE (alarm), po rozbrojeniu wysyła też komendę
        'wyczyść pamięć alarmu' - tak samo jak integracja Home Assistant
        (homeassistant/components/satel_integra/alarm_control_panel.py:
        async_alarm_disarm() robi clear_alarm() z 1s opóźnieniem po disarm()).
        BEZ TEGO: centrala Satel trzyma "pamięć alarmu" jako OSOBNĄ flagę od
        stanu uzbrojenia strefy - zwykłe rozbrojenie jej NIE czyści, więc
        partition_states (a za tym armed_partition_numbers/panel/MQTT) mogłoby
        dalej pokazywać TRIGGERED mimo że strefa jest już rozbrojona."""
        if self._client is None:
            raise RuntimeError("Klient Satel nie jest połączony")
        if not self.integration_key:
            # Patrz komentarz w set_output() wyżej - bez klucza SatelWriteMessage
            # wysłałaby okaleczoną ramkę zamiast czytelnie odrzuconej komendy.
            raise RuntimeError(
                "Sterowanie wymaga klucza integracji Satela - ustaw go w zakładce "
                "⚙ Ustawienia -> 🔌 Połączenia (bez klucza panel działa tylko do odczytu)."
            )
        from satel_integra.commands import SatelWriteCommand  # type: ignore
        from satel_integra.messages import SatelWriteMessage  # type: ignore

        partitions = partitions or [self.partition]
        clear_alarm_necessary = bool(self.triggered_partition_numbers & set(partitions))
        msg = SatelWriteMessage(SatelWriteCommand.PARTITIONS_DISARM, code=self.integration_key, partitions=partitions)
        await _wait_for_command_result(self._client, f"rozbrojenie stref {partitions}", msg)
        logger.info("Rozbrojono strefy %s", partitions)
        if clear_alarm_necessary:
            await asyncio.sleep(1)
            msg2 = SatelWriteMessage(
                SatelWriteCommand.PARTITIONS_CLEAR_ALARM, code=self.integration_key, partitions=partitions,
            )
            await _wait_for_command_result(self._client, f"czyszczenie pamięci alarmu stref {partitions}", msg2)
            logger.info("Wyczyszczono pamięć alarmu dla stref %s", partitions)

    def _schedule(self, coro) -> None:
        """Planuje wykonanie korutyny jako osobne zadanie w tle i loguje
        wyjątek, jeśli zadanie się nim zakończy (żeby błąd w callbacku nie
        przepadł bez śladu - domyślny handler asyncio dla nieodebranych
        wyjątków w zadaniach jest łatwo przeoczyć w logach).

        WAŻNE, dlaczego to w ogóle istnieje: biblioteka satel_integra woła
        `_zone_changed_callback`/`_output_changed_callback`/
        `_alarm_status_callback` SYNCHRONICZNIE, jako zwykłe wywołanie
        funkcji - nie czeka na żaden `await` (bez zmian względem 0.3.x).
        Nasze callbacki (_on_zone_change/_on_output_change/
        _handle_alarm_status_change w RulesEngine) są `async def`, bo robią
        I/O (zapis do historii, publikacja MQTT). Wywołanie korutyny BEZ
        await tylko tworzy obiekt korutyny i go odrzuca - CIAŁO FUNKCJI
        NIGDY SIĘ NIE WYKONUJE (żaden wyjątek, tylko ciche "coroutine was
        never awaited" w logach) - stąd rejestrujemy tu SYNCHRONICZNY
        trampolinowy wrapper, który sam planuje korutynę przez
        asyncio.ensure_future(), zamiast przekazywać korutynę bibliotece
        wprost."""
        task = asyncio.ensure_future(coro)

        def _log_exception(t: "asyncio.Task") -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error("Błąd w callbacku Satela", exc_info=t.exception())

        task.add_done_callback(_log_exception)

    def register_zone_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Rejestruje callback wywoływany przy zmianie stanu wejść (np. do
        reakcji 'ruch z Satela -> włącz gniazdko'). Zapisany na obiekcie
        klienta (nie tylko na chwilowym AsyncSatel), więc przetrwa
        automatyczne rozłączenia i ponowne łączenie.

        UWAGA: `callback` dostaje BEZPOŚREDNIO `dict[int, int]` (numer ->
        0/1) - satel_integra >=1.0.0 usunęła wcześniejszy wrapper
        `{"zones": {...}}` z 0.3.x (patrz RulesEngine._on_zone_change,
        gdzie to jest rozpakowywane). Patrz też komentarz w _schedule() -
        callback biblioteki jest SYNCHRONICZNY, więc `callback` (korutyna)
        jest tu opakowana."""
        def _sync_wrapper(status: dict) -> None:
            self._schedule(callback(status))

        self._zone_cb = _sync_wrapper
        if self._client is not None:
            self._client.register_callbacks(zone_changed_callback=_sync_wrapper)

    def register_output_callback(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        """Analogicznie do register_zone_callback, ale dla wyjść."""
        def _sync_wrapper(status: dict) -> None:
            self._schedule(callback(status))

        self._output_cb = _sync_wrapper
        if self._client is not None:
            self._client.register_callbacks(output_changed_callback=_sync_wrapper)

    def register_alarm_status_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Callback wywoływany przy zmianie stanu uzbrojenia stref (widoczny
        w panelu jako 'Strefy Satel') - oraz przy odebraniu dowolnej innej
        ramki statusu (biblioteka używa go też jako powiadomienia "coś się
        zmieniło, sprawdź stan połączenia").

        UWAGA: w odróżnieniu od register_zone_callback/register_output_callback
        callback NIE dostaje żadnego argumentu - biblioteka woła go bez
        parametrów, samo bieżące partition_states trzeba odczytać z
        armed_partition_numbers niżej. Patrz też komentarz w _schedule() -
        synchroniczny wrapper jak wyżej."""
        def _sync_wrapper() -> None:
            self._schedule(callback())

        self._alarm_status_cb = _sync_wrapper
        if self._client is not None:
            self._client.register_callbacks(alarm_status_callback=_sync_wrapper)

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
        nadpisywane na bieżąco przy każdym komunikacie o zmianie stanu
        stref, więc suma po wszystkich trybach zawsze odzwierciedla
        aktualny stan - callback rejestrowany przez
        register_alarm_status_callback dostaje tylko powiadomienie "coś się
        zmieniło", nie sam stan."""
        if self._client is None:
            return set()
        numbers: set[int] = set()
        for partitions in self._client.partition_states.values():
            numbers.update(partitions)
        return numbers

    @property
    def triggered_partition_numbers(self) -> set[int]:
        """Numery stref aktualnie w stanie TRIGGERED/TRIGGERED_FIRE (alarm
        wzbudzony) - patrz disarm() wyżej, gdzie służy do decyzji, czy po
        rozbrojeniu trzeba też wyczyścić pamięć alarmu (clear_alarm).
        Import odroczony jak gdzie indziej w tym pliku - dzięki temu ta
        właściwość działa też, gdy `partition_states` zostało nadpisane
        bezpośrednio w teście, bez przechodzenia przez connect()."""
        if self._client is None:
            return set()
        from satel_integra import AlarmState  # type: ignore
        numbers: set[int] = set()
        for state in (AlarmState.TRIGGERED, AlarmState.TRIGGERED_FIRE):
            numbers.update(self._client.partition_states.get(state, []))
        return numbers

    @property
    def is_connected(self) -> bool:
        return self._client is not None and not getattr(self._client, "stopped", True)

    async def run_forever(self) -> None:
        """Utrzymuje połączenie z centralą z automatycznym ponawianiem po
        NIEODWRACALNEJ awarii (Supervisor, exponential backoff, patrz
        bridge/resilience.py). Status 'zdrowia' połączenia trafia do panelu
        (kategoria system_health) i do dziennika zdarzeń przy każdej zmianie
        stanu.

        ISTOTNIE PROSTSZE niż w wersji dla satel_integra 0.3.x: biblioteka
        1.x SAMA obsługuje keepalive/reconnect/wykrywanie "centrala zajęta"
        wewnątrz `start()` (patrz obszerny docstring modułu) - nie trzeba
        już własnego watchdoga "sprawdzaj `connected` z zewnątrz co 5s",
        który wcześniej odróżniał to od zwykłej, przejściowej awarii sieci
        heurystyką (kilka kolejnych "nie połączono"). Ten Supervisor.run()
        obsługuje TYLKO scenariusz "połączenie wstępne się nie powiodło"
        (connect() rzuca ConnectionError) ORAZ "połączenie zostało
        TERMINALNIE zatrzymane" (`self._client.stopped` - ustawiane przez
        bibliotekę tylko po jawnym close() albo nienaprawialnym błędzie
        wewnętrznym, NIE przy zwykłych, przejściowych zerwaniach sieci,
        które biblioteka naprawia sama w tle)."""

        async def _connect_and_monitor(on_connected) -> None:
            await self.connect()
            await on_connected()
            client = self._client
            while client is not None and client is self._client and not getattr(client, "stopped", True):
                await asyncio.sleep(5)

        await self._supervisor.run(_connect_and_monitor)
