from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from iqua_softener import IquaSoftener, IquaSoftenerException

logger = logging.getLogger("bridge.iqua")

# Zmiękczacz soli reaguje na zmiany bardzo powoli (poziom soli/zużycie wody
# liczą się w dniach, nie sekundach) - stąd znacznie dłuższy domyślny odstęp
# niż przy AGD LG ThinQ/HomeWhiz (tam odpytywanie wykrywa np. koniec prania).
_DEFAULT_POLL_INTERVAL_SECONDS = 900
_MIN_POLL_INTERVAL_SECONDS = 60


class IquaError(Exception):
    pass


def _wrap_api_errors(exc: Exception, *, context: str) -> IquaError:
    if isinstance(exc, IquaSoftenerException):
        return IquaError(f"Chmura iQua (Ecowater) zgłosiła błąd ({context}): {exc}")
    return IquaError(f"Nieoczekiwany błąd iQua ({context}): {exc}")


async def test_connection(username: str, password: str, serial_number: str):
    """Używane przez stronę wtyczki (przycisk "Testuj urządzenie") - w
    odróżnieniu od LG ThinQ/HomeWhiz to API nie ma osobnego wywołania
    "lista urządzeń konta" (numer seryjny trzeba znać/wpisać ręcznie, patrz
    etykieta DSN# w aplikacji iQua), więc jedynym sposobem sprawdzenia
    poprawności danych PRZED zapisaniem jest realne pobranie danych."""
    loop = asyncio.get_event_loop()
    client = IquaSoftener(username, password, serial_number)
    try:
        return await loop.run_in_executor(None, client.get_data)
    except Exception as exc:
        raise _wrap_api_errors(exc, context="test urządzenia") from exc


@dataclass
class _DeviceWatch:
    name: str
    serial_number: str
    last_state: str | None = field(default=None, repr=False)


class IquaClient:
    """Odpytuje chmurę Ecowater/Viessmann Aquahome (aplikacja "iQua" -
    zmiękczacze wody) o bieżący stan skonfigurowanych urządzeń. Pakiet
    `iqua_softener` jest SYNCHRONICZNY (biblioteka `requests`, logowanie
    użytkownik/hasło + numer seryjny urządzenia, endpoint
    `https://apioem.ecowater.com/v1` - zweryfikowane wprost w kodzie pakietu:
    `IquaSoftener.get_data()` robi `GET system/{serial}/dashboard`) - każde
    wywołanie odpytujemy w osobnym wątku (`run_in_executor`), żeby nie
    blokować event loopa bridge'a, dokładnie tak samo jak lokalny/chmurowy
    klient Tuya (`tuya_local.py`/`tuya_cloud.py`) robi to dla `tinytuya`.

    W odróżnieniu od LG ThinQ/HomeWhiz (jedno pole "stanu pracy" w sam raz na
    pojedynczy string), `IquaSoftenerData` niesie kilka przydatnych liczbowych
    odczytów naraz (poziom soli, zużycie wody, dni do wyczerpania soli) - stąd
    callback dostaje CAŁY obiekt danych, nie gotowy string, i to
    RulesEngine._on_iqua_state (nie ten moduł) decyduje, które pola trafiają
    do historii jako osobne statusy (wzorem sub-odczytów Tuya: `:brightness`,
    `:battery` w `_apply_tuya_device_status`)."""

    def __init__(
        self, cfg: dict, on_state: Callable[[str, object], Awaitable[None]],
        history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
    ):
        self.cfg = cfg
        self.on_state = on_state
        self._devices: dict[str, _DeviceWatch] = {
            name: _DeviceWatch(name=name, serial_number=details["serial_number"])
            for name, details in (cfg.get("devices") or {}).items()
            if details.get("serial_number")
        }
        interval = cfg.get("status_poll_interval_seconds") or _DEFAULT_POLL_INTERVAL_SECONDS
        self._interval = max(_MIN_POLL_INTERVAL_SECONDS, float(interval))

    async def run_forever(self) -> None:
        username = (self.cfg.get("username") or "").strip()
        password = self.cfg.get("password") or ""
        if not self._devices:
            logger.info("iQua: brak skonfigurowanych urządzeń - pomijam odpytywanie")
            return
        if not username or not password:
            logger.warning(
                "iQua: brak loginu/hasła do konta iQua - odpytywanie wyłączone, "
                "zapisz poświadczenia na stronie wtyczki",
            )
            return

        logger.info("iQua: uruchamiam odpytywanie %d urządzeń co %ss", len(self._devices), self._interval)
        loop = asyncio.get_event_loop()
        # Jeden IquaSoftener na urządzenie (biblioteka wiąże sesję/token z
        # konkretnym numerem seryjnym) - logowanie następuje leniwie przy
        # pierwszym get_data(), a token jest odświeżany przez samą bibliotekę.
        clients = {
            watch.name: IquaSoftener(username, password, watch.serial_number)
            for watch in self._devices.values()
        }
        while True:
            for watch in self._devices.values():
                await self._poll_device(loop, clients[watch.name], watch)
            await asyncio.sleep(self._interval)

    async def _poll_device(self, loop: asyncio.AbstractEventLoop, client: IquaSoftener, watch: _DeviceWatch) -> None:
        try:
            data = await loop.run_in_executor(None, client.get_data)
        except Exception as exc:
            logger.error("iQua '%s': błąd odpytywania stanu - %s", watch.name, _wrap_api_errors(exc, context=watch.name))
            return

        state_name = data.state.value.lower() if data.state is not None else None
        logger.debug("iQua '%s': surowa odpowiedź=%r, stan=%r", watch.name, data, state_name)
        # W odróżnieniu od LG ThinQ NIE pomijamy wywołania callbacku, gdy stan
        # (online/offline) się nie zmienił - w tym wywołaniu niesiemy też
        # poziom soli/zużycie wody, które zmieniają się NIEZALEŻNIE od stanu
        # połączenia i praktycznie zawsze pozostaje ono "online" - pominięcie
        # zamroziłoby te odczyty na wartości z pierwszego udanego odpytania.
        # Ewentualne zbędne, powtarzające się odpalanie triggera Flow/reguły
        # dla samego pola stanu i tak filtruje `RulesEngine.handle_device_state`
        # (pamięta ostatnią wartość per urządzenie).
        if state_name != watch.last_state:
            watch.last_state = state_name
            logger.info("iQua '%s': stan -> %s", watch.name, state_name)
        await self.on_state(watch.name, data)
