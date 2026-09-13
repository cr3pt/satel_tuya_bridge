from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import aiohttp
from thinqconnect.thinq_api import ThinQApi, ThinQAPIException

logger = logging.getLogger("bridge.lg_thinq")

_DEFAULT_POLL_INTERVAL_SECONDS = 60
_MIN_POLL_INTERVAL_SECONDS = 20

# Tłumaczenie wartości runState.currentState (oficjalny model danych LG ThinQ
# Connect - te same nazwy zasobu/właściwości dla pralek, suszarek, zmywarek i
# części pozostałego AGD, zweryfikowane wprost w kodzie pakietu thinqconnect:
# devices/washer.py itp. mapują property_map={"runState": {"currentState":
# Property.CURRENT_STATE}}). NIE KAŻDY model zna te same wartości - lista
# faktycznie wspieranych stanów jest częścią deskryptora profilu urządzenia
# (patrz list_possible_run_states); tu tłumaczymy tylko najczęstsze/udokumentowane,
# nieznana wartość przechodzi bez zmian (zmniejszona wielkość liter) zamiast
# znikać po cichu - dokładnie ten sam kompromis co HomeWhiz (to_friendly_name).
_RUN_STATE_TRANSLATIONS = {
    "POWER_OFF": "wyłączona",
    "POWER_ON": "włączona",
    "INITIAL": "gotowa",
    "RUNNING": "pranie",
    "PAUSE": "wstrzymane",
    "RINSING": "płukanie",
    "SPINNING": "wirowanie",
    "DRYING": "suszenie",
    "COOLING": "chłodzenie",
    "END": "zakończone",
    "ERROR": "błąd",
    "RESERVE": "zaplanowane",
    "DETECTING": "wykrywanie wsadu",
    "FIRMWARE": "aktualizacja oprogramowania",
    "FIRMWARE_INSTALLING": "aktualizacja oprogramowania",
    "PREWASH": "pranie wstępne",
    "STEAM": "parowanie",
    "ADD_DRAIN": "dodatkowe odpompowanie",
    "WASH": "pranie",
    "DRY": "suszenie",
}


class LgThinqError(Exception):
    pass


def friendly_run_state(raw: str | None) -> str | None:
    if not raw:
        return None
    return _RUN_STATE_TRANSLATIONS.get(raw.upper(), raw.lower())


def decode_run_state(status: dict | None) -> str | None:
    """`status` to surowa odpowiedź `ThinQApi.async_get_device_status()` -
    zweryfikowany kształt `{"runState": {"currentState": "..."}}` (dokładnie
    ta ścieżka, którą sam pakiet thinqconnect mapuje wewnętrznie dla pralek/
    suszarek/zmywarek/piekarników/robotów sprzątających - devices/*.py:
    property_map={"runState": {"currentState": Property.CURRENT_STATE}})."""
    if not isinstance(status, dict):
        return None
    run_state = status.get("runState")
    if not isinstance(run_state, dict):
        return None
    return friendly_run_state(run_state.get("currentState"))


def list_possible_run_states(profile: dict | None) -> list[str]:
    """Surowe (WIELKIMI literami) wartości runState.currentState wspierane przez
    KONKRETNY model urządzenia, odczytane z jego deskryptora profilu
    (`ThinQApi.async_get_device_profile()`) - ścieżka
    `profile["property"]["runState"]["currentState"]["value"]["r"]` to
    zweryfikowany kształt używany przez `ConnectDeviceProfile._get_properties()`
    w pakiecie thinqconnect (stała `PROPERTY_READABLE == "r"`). Różne modele
    (nawet tego samego typu urządzenia) mogą zwracać różny zestaw wartości -
    stąd pobieranie per urządzenie, tak samo jak HomeWhiz pobiera deskryptor
    konfiguracji per urządzenie zamiast zakładać jeden uniwersalny zestaw."""
    try:
        values = profile["property"]["runState"]["currentState"]["value"]["r"]
    except (KeyError, TypeError):
        return []
    return list(values) if isinstance(values, list) else []


def _wrap_api_errors(exc: Exception, *, context: str) -> LgThinqError:
    if isinstance(exc, ThinQAPIException):
        return LgThinqError(f"Chmura LG ThinQ zgłosiła błąd ({context}): {exc}")
    if isinstance(exc, RuntimeError):
        # ThinQApi rzuca zwykły RuntimeError("Not supported country_code.")
        # dla nieznanego kodu kraju - to jedyny taki przypadek w tym SDK.
        return LgThinqError(f"Nieprawidłowy kod kraju LG ThinQ ({context}): {exc}")
    if isinstance(exc, aiohttp.ClientError):
        return LgThinqError(f"Błąd połączenia z chmurą LG ThinQ ({context}): {exc}")
    return LgThinqError(f"Nieoczekiwany błąd LG ThinQ ({context}): {exc}")


async def list_devices(pat: str, country_code: str, client_id: str) -> list[dict]:
    async with aiohttp.ClientSession() as session:
        api = ThinQApi(session=session, access_token=pat, country_code=country_code, client_id=client_id)
        try:
            devices = await api.async_get_device_list()
        except Exception as exc:
            raise _wrap_api_errors(exc, context="lista urządzeń") from exc

    result = []
    for entry in devices or []:
        device_id = entry.get("deviceId") if isinstance(entry, dict) else None
        if not device_id:
            continue
        info = entry.get("deviceInfo") or {}
        result.append({
            "device_id": device_id,
            "alias": info.get("alias") or device_id,
            "device_type": info.get("deviceType", ""),
            "model_name": info.get("modelName", ""),
        })
    return result


async def fetch_possible_states(pat: str, country_code: str, client_id: str, device_id: str) -> list[str]:
    async with aiohttp.ClientSession() as session:
        api = ThinQApi(session=session, access_token=pat, country_code=country_code, client_id=client_id)
        try:
            profile = await api.async_get_device_profile(device_id)
        except Exception as exc:
            raise _wrap_api_errors(exc, context="deskryptor profilu") from exc

    raw_values = list_possible_run_states(profile or {})
    return [friendly_run_state(v) or v.lower() for v in raw_values]


@dataclass
class _DeviceWatch:
    name: str
    device_id: str
    last_state: str | None = field(default=None, repr=False)


class LgThinqClient:
    """Odpytuje chmurę LG ThinQ Connect (oficjalne REST API, autoryzacja PAT -
    patrz plugins/lg_thinq/routes.py) o bieżący stan (`runState.currentState`)
    skonfigurowanych urządzeń (pralki, suszarki, zmywarki i inne AGD LG z tym
    samym zasobem stanu). W odróżnieniu od HomeWhiz (AWS IoT/MQTT, push w czasie
    rzeczywistym) używa zwykłego odpytywania - oficjalne SDK (`thinqconnect`)
    udostępnia też push przez AWS IoT, ale wymaga dodatkowo wystawienia
    certyfikatu klienta (async_post_client_certificate) i utrzymywania osobnego
    połączenia MQTT - odpytywanie jest prostsze i w zupełności wystarcza do
    zgłaszania zmiany stanu jako trigger Flow/reguł ruchu."""

    def __init__(
        self, cfg: dict, on_state: Callable[[str, str], Awaitable[None]],
        history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
    ):
        self.cfg = cfg
        self.on_state = on_state
        self._devices: dict[str, _DeviceWatch] = {
            name: _DeviceWatch(name=name, device_id=details["device_id"])
            for name, details in (cfg.get("devices") or {}).items()
            if details.get("device_id")
        }
        interval = cfg.get("status_poll_interval_seconds") or _DEFAULT_POLL_INTERVAL_SECONDS
        self._interval = max(_MIN_POLL_INTERVAL_SECONDS, float(interval))

    async def run_forever(self) -> None:
        pat = (self.cfg.get("personal_access_token") or "").strip()
        client_id = (self.cfg.get("client_id") or "").strip()
        if not self._devices:
            logger.info("LG ThinQ: brak skonfigurowanych urządzeń - pomijam odpytywanie")
            return
        if not pat or not client_id:
            logger.warning(
                "LG ThinQ: brak tokenu PAT lub client_id - odpytywanie wyłączone, "
                "zapisz poświadczenia na stronie wtyczki",
            )
            return

        country_code = (self.cfg.get("country_code") or "PL").strip() or "PL"
        logger.info(
            "LG ThinQ: uruchamiam odpytywanie %d urządzeń co %ss", len(self._devices), self._interval,
        )
        async with aiohttp.ClientSession() as session:
            api = ThinQApi(session=session, access_token=pat, country_code=country_code, client_id=client_id)
            while True:
                for watch in self._devices.values():
                    await self._poll_device(api, watch)
                await asyncio.sleep(self._interval)

    async def _poll_device(self, api: ThinQApi, watch: _DeviceWatch) -> None:
        try:
            status = await api.async_get_device_status(watch.device_id)
        except Exception as exc:
            logger.error("LG ThinQ '%s': błąd odpytywania stanu - %s", watch.name, _wrap_api_errors(exc, context=watch.name))
            return

        state_name = decode_run_state(status)
        logger.debug("LG ThinQ '%s': surowa odpowiedź=%r, stan=%r", watch.name, status, state_name)
        if state_name is None:
            logger.debug(
                "LG ThinQ '%s': brak pola runState.currentState w odpowiedzi - to urządzenie może nie "
                "udostępniać tego zasobu (np. niektóre typy AGD inne niż pralka/suszarka/zmywarka)",
                watch.name,
            )
            return
        if state_name == watch.last_state:
            return
        watch.last_state = state_name
        logger.info("LG ThinQ '%s': stan -> %s", watch.name, state_name)
        await self.on_state(watch.name, state_name)
