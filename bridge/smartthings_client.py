from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pysmartthings import SmartThings
from pysmartthings.exceptions import SmartThingsError as SmartThingsApiError

logger = logging.getLogger("bridge.smartthings")

_DEFAULT_POLL_INTERVAL_SECONDS = 30
_MIN_POLL_INTERVAL_SECONDS = 10
_DEFAULT_CAPABILITY = "switch"
_DEFAULT_ATTRIBUTE = "switch"
_DEFAULT_COMPONENT = "main"

# Wartości SmartThings są z reguły już czytelne bez tłumaczenia (open/closed,
# active/inactive, liczby...) - tłumaczymy na polski tylko najczęstszy,
# najbardziej "automatyzowalny" przypadek (switch: gniazdka/przełączniki),
# ten sam kompromis co friendly_run_state (LG ThinQ) / to_friendly_name
# (HomeWhiz) - nieznana/nietłumaczona wartość przechodzi bez zmian.
_SWITCH_TRANSLATIONS = {"on": "włączone", "off": "wyłączone"}


class SmartThingsError(Exception):
    pass


def _wrap_api_errors(exc: Exception, *, context: str) -> SmartThingsError:
    if isinstance(exc, SmartThingsApiError):
        return SmartThingsError(f"Chmura SmartThings zgłosiła błąd ({context}): {exc}")
    return SmartThingsError(f"Nieoczekiwany błąd SmartThings ({context}): {exc}")


def friendly_status_value(capability: str, raw_value) -> str | None:
    if raw_value is None:
        return None
    if capability == "switch" and isinstance(raw_value, str):
        return _SWITCH_TRANSLATIONS.get(raw_value.lower(), raw_value.lower())
    return str(raw_value)


def extract_attribute_value(raw_status: dict | None, capability: str, attribute: str, component: str = _DEFAULT_COMPONENT):
    """`raw_status` to surowa odpowiedź `SmartThings.get_raw_device_status()` -
    kształt zweryfikowany wprost w kodzie pakietu pysmartthings
    (models.py: DeviceStatus.components: dict[component_id, ComponentStatus],
    ComponentStatus = dict[capability, dict[attribute, Status]]) - czyli
    `{"components": {"main": {"switch": {"switch": {"value": "on", ...}}}}}`.
    Używamy WARIANTU SUROWEGO (nie typowanych dataclassów biblioteki), bo
    capability/attribute do odczytania są dowolnym tekstem wpisanym przez
    użytkownika (nie każda istnieje w enumach `Capability`/`Attribute` z tej
    biblioteki - nieznane capability tylko loguje ostrzeżenie w typowanej
    ścieżce, więc surowy JSON jest odporniejszy i prostszy)."""
    try:
        return raw_status["components"][component][capability][attribute]["value"]
    except (KeyError, TypeError):
        return None


async def list_devices(pat: str) -> list[dict]:
    st = SmartThings()
    st.authenticate(pat)
    try:
        devices = await st.get_devices()
    except Exception as exc:
        raise _wrap_api_errors(exc, context="lista urządzeń") from exc
    finally:
        await st.close()

    return [
        {
            "device_id": d.device_id,
            "alias": d.label or d.name or d.device_id,
            "device_type": str(d.type),
            "model_name": d.device_type_name or "",
        }
        for d in devices
    ]


@dataclass
class _DeviceWatch:
    name: str
    device_id: str
    capability: str
    attribute: str
    last_state: str | None = field(default=None, repr=False)


class SmartThingsClient:
    """Odpytuje oficjalne REST API SmartThings (chmura Samsung) o bieżący stan
    skonfigurowanych urządzeń - autoryzacja osobistym tokenem dostępu (PAT,
    generowanym na https://account.smartthings.com/tokens), bez pełnego
    przepływu OAuth2/SmartApp. Pakiet `pysmartthings` (używany też przez
    integrację SmartThings w Home Assistant) jest w PEŁNI ASYNCHRONICZNY
    (aiohttp) - w odróżnieniu od Tuya/iQua/LG ThinQ (SDK synchroniczne albo
    wymagające client_id/country_code) nie trzeba tu żadnego run_in_executor
    ani dodatkowych poświadczeń poza samym tokenem.

    Biblioteka udostępnia też PUSH w czasie rzeczywistym (subscribe() przez
    Server-Sent Events) - wymaga jednak zarejestrowanej, zainstalowanej
    SmartApp (własny appId + przepływ autoryzacji OAuth2 + zarządzanie
    subskrypcjami) - zbyt duża złożoność dla samohostowanego mostka bez
    publicznego endpointu; zwykłe odpytywanie w zupełności wystarcza do
    wykrywania zmiany stanu jako trigger Flow/reguł ruchu, dokładnie tak samo
    jak LG ThinQ (który z tego samego powodu też nie używa swojego kanału
    push przez AWS IoT)."""

    def __init__(
        self, cfg: dict, on_state: Callable[[str, str], Awaitable[None]],
        history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
    ):
        self.cfg = cfg
        self.on_state = on_state
        self._devices: dict[str, _DeviceWatch] = {
            name: _DeviceWatch(
                name=name,
                device_id=details["device_id"],
                capability=(details.get("capability") or _DEFAULT_CAPABILITY).strip() or _DEFAULT_CAPABILITY,
                attribute=(details.get("attribute") or _DEFAULT_ATTRIBUTE).strip() or _DEFAULT_ATTRIBUTE,
            )
            for name, details in (cfg.get("devices") or {}).items()
            if details.get("device_id")
        }
        interval = cfg.get("status_poll_interval_seconds") or _DEFAULT_POLL_INTERVAL_SECONDS
        self._interval = max(_MIN_POLL_INTERVAL_SECONDS, float(interval))

    async def run_forever(self) -> None:
        pat = (self.cfg.get("personal_access_token") or "").strip()
        if not self._devices:
            logger.info("SmartThings: brak skonfigurowanych urządzeń - pomijam odpytywanie")
            return
        if not pat:
            logger.warning(
                "SmartThings: brak tokenu dostępu (PAT) - odpytywanie wyłączone, "
                "zapisz go na stronie wtyczki",
            )
            return

        logger.info(
            "SmartThings: uruchamiam odpytywanie %d urządzeń co %ss", len(self._devices), self._interval,
        )
        st = SmartThings()
        st.authenticate(pat)
        try:
            while True:
                for watch in self._devices.values():
                    await self._poll_device(st, watch)
                await asyncio.sleep(self._interval)
        finally:
            await st.close()

    async def _poll_device(self, st: SmartThings, watch: _DeviceWatch) -> None:
        try:
            raw_status = await st.get_raw_device_status(watch.device_id)
        except Exception as exc:
            logger.error(
                "SmartThings '%s': błąd odpytywania stanu - %s",
                watch.name, _wrap_api_errors(exc, context=watch.name),
            )
            return

        raw_value = extract_attribute_value(raw_status, watch.capability, watch.attribute)
        state_name = friendly_status_value(watch.capability, raw_value)
        logger.debug("SmartThings '%s': surowa odpowiedź=%r, stan=%r", watch.name, raw_status, state_name)
        if state_name is None:
            logger.debug(
                "SmartThings '%s': brak wartości %s.%s w odpowiedzi - sprawdź, czy urządzenie "
                "faktycznie udostępnia tę capability/attribute (zakładka wtyczki pozwala je zmienić)",
                watch.name, watch.capability, watch.attribute,
            )
            return
        if state_name == watch.last_state:
            return
        watch.last_state = state_name
        logger.info("SmartThings '%s': stan -> %s", watch.name, state_name)
        await self.on_state(watch.name, state_name)
