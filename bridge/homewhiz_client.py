"""Zmywarki (i inne AGD) BEQ/Arcelik sterowane przez chmurę HomeWhiz.

Bridge łączy się BEZPOŚREDNIO z chmurą HomeWhiz (Arcelik IoT, AWS) - nie ma
tu żadnego pośredniczącego lokalnego brokera MQTT (w odróżnieniu od eBUS).
Kształt API rozpoznany na podstawie integracji Home Assistant
(https://github.com/home-assistant-HomeWhiz/home-assistant-HomeWhiz,
licencja MIT) - to repo NIE jest zależnością tego projektu, tylko
źródłem wiedzy o kształcie żądań/odpowiedzi.

Cztery kroki:

1. Logowanie (`login`) - POST username/password -> tymczasowe poświadczenia
   AWS (accessKey/secretKey/sessionToken/expiration), używane do
   podpisywania (SigV4) każdego kolejnego żądania do AWS.
2. Lista urządzeń (`list_appliances`) - podpisane żądania GET do
   smarthome.arcelikiot.com. `applianceId` z tej listy to JEST już nazwa
   "rzeczy" (thing) w AWS IoT Core - osobna wymiana identyfikatora
   (`idexchange.arcelikiot.com`) używana przez integrację HA dotyczy
   TYLKO parowania Bluetooth, którego celowo nie obsługujemy (patrz niżej).
3. Deskryptor konfiguracji (`fetch_config_descriptor`) - podpisane GET po
   listę wersji konfiguracji, potem zwykłe (bez podpisu) GET do S3 po
   surowy JSON z listą możliwych stanów urządzenia i offsetem w tablicy
   bajtów stanu ("wifi array").
4. Żywy stan (`HomeWhizClient`) - trwałe połączenie MQTT-over-WebSocket do
   AWS IoT Core, autoryzowane URL-em podpisanym SigV4 (parametry zapytania,
   nie nagłówek - to jedyny sposób na WebSocket). Odbiera "shadow" (cień
   urządzenia w AWS IoT) i dekoduje go do pojedynczej, czytelnej nazwy
   stanu (np. "running"/"finished"/"idle").

CELOWO bez `awsiotsdk`/`awscrt` (używanych przez integrację HA) - to
skompilowana biblioteka C/Rust bez gotowego koła dla części architektur, a
mamy już `paho-mqtt` (transport="websockets") + `aiohttp` jako zależności
dla innych integracji. SigV4 dla WebSocketa to zwykłe podpisywanie przez
parametry zapytania (ten sam mechanizm co presigned URL do S3), z jedną
udokumentowaną przez AWS osobliwością dla tymczasowych poświadczeń:
`X-Amz-Security-Token` dokłada się do URL-a DOPIERO PO policzeniu podpisu
(nie bierze udziału w podpisywanym zapytaniu kanonicznym).

Podstawowe funkcje podpisujące (`_sigv4_headers`/`_canonical_query_string`/
`_signing_key`) są zweryfikowane względem oficjalnego zestawu testowego AWS
Signature Version 4 (https://docs.aws.amazon.com/general/latest/gr/
signature-v4-test-suite.html, przypadki "get-vanilla"/
"get-vanilla-query-order-key") - patrz tests/test_homewhiz_client.py.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import aiohttp

from .resilience import Supervisor

logger = logging.getLogger("bridge.homewhiz")

AUTH_HOST = "api.arcelikiot.com"
SMARTHOME_HOST = "smarthome.arcelikiot.com"
IOT_ENDPOINT = "ajf7v9dcoe69w-ats.iot.eu-west-1.amazonaws.com"
DEFAULT_REGION = "eu-west-1"
_APPLIANCE_TYPE_DISHWASHER = 3
# Urządzenia sparowane WYŁĄCZNIE przez Bluetooth (bez połączenia z chmurą) -
# integracja HA używa dla nich osobnej wymiany identyfikatora
# (idexchange.arcelikiot.com), której celowo nie obsługujemy - taki
# sprzęt i tak nie da się odpytać zdalnie przez bridge (musiałby stać w
# zasięgu BT tego samego urządzenia co telefon, co jest bezcelowe dla
# serwera działającego w Dockerze).
_BLUETOOTH_ONLY_CONNECTIVITY = {"BT", "BASICBT"}


class HomeWhizError(Exception):
    """Błąd czytelny dla użytkownika (logowanie/odkrywanie urządzeń) -
    odróżniany od nieoczekiwanych wyjątków w routingu panelu (patrz
    plugins/homewhiz/routes.py), tak jak ConnectionError w bridge/tuya_local.py."""


# --- SigV4 - podstawowe prymitywy, zweryfikowane względem oficjalnego
# zestawu testowego AWS (patrz docstring modułu wyżej i
# tests/test_homewhiz_client.py) -----------------------------------------

def _amz_dates(now: datetime.datetime | None = None) -> tuple[str, str]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = hmac.new(("AWS4" + secret_key).encode(), date_stamp.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _canonical_query_string(query: str) -> str:
    """Sortuje parametry zapytania (najpierw po kluczu, potem po wartości -
    dokładnie jak w przypadku testowym AWS "get-vanilla-query-order-key",
    gdzie dwa parametry o TEJ SAMEJ nazwie porządkuje się po wartości) i
    re-enkoduje je zgodnie z regułami SigV4 (RFC 3986, `~` bez enkodowania)."""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    pairs.sort()
    return "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in pairs
    )


def _sigv4_headers(
    *, method: str, host: str, path: str, query: str, region: str, service: str,
    access_key: str, secret_key: str, session_token: str | None,
    payload: bytes = b"", extra_signed_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Podpisuje żądanie REST przez nagłówek `Authorization` (używane dla
    zwykłych GET-ów do smarthome.arcelikiot.com/api.arcelikiot.com) - zwraca
    KOMPLET nagłówków do wysłania (Authorization + X-Amz-Date +
    X-Amz-Security-Token, jeśli poświadczenia tymczasowe)."""
    amz_date, date_stamp = _amz_dates()
    headers = {"host": host, "x-amz-date": amz_date}
    if session_token:
        headers["x-amz-security-token"] = session_token
    if extra_signed_headers:
        headers.update({k.lower(): v for k, v in extra_signed_headers.items()})

    signed_headers = ";".join(sorted(headers.keys()))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers.keys()))
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_request = "\n".join([
        method, path or "/", _canonical_query_string(query),
        canonical_headers, signed_headers, payload_hash,
    ])
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode(), hashlib.sha256,
    ).hexdigest()

    result = dict(headers)
    del result["host"]  # aiohttp dokłada Host sam - jawny nagłówek by go zdublował
    result["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    result["X-Amz-Date"] = result.pop("x-amz-date")
    if "x-amz-security-token" in result:
        result["X-Amz-Security-Token"] = result.pop("x-amz-security-token")
    return result


def presign_iot_websocket_url(
    *, endpoint: str, region: str, access_key: str, secret_key: str,
    session_token: str, expires_seconds: int = 3600,
) -> str:
    """Buduje podpisany (SigV4, parametry zapytania) adres `wss://` do
    AWS IoT Core - jedyny sposób autoryzacji WebSocketa przez SigV4 (nie ma
    tu miejsca na nagłówek `Authorization`, przeglądarka/klient WebSocket
    go nie ustawia). Ten sam mechanizm co presigned URL do S3, ze WSPÓLNĄ
    osobliwością dla poświadczeń TYMCZASOWYCH (z sesją) - `X-Amz-Security-
    Token` dokłada się do URL-a DOPIERO PO policzeniu podpisu, nie bierze
    udziału w samym podpisywanym zapytaniu (udokumentowane przez AWS jako
    wyjątek od zwykłej reguły 'wszystko w query string jest podpisywane')."""
    amz_date, date_stamp = _amz_dates()
    service = "iotdevicegateway"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    canonical_uri = "/mqtt"

    query_params = [
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
        ("X-Amz-Credential", f"{access_key}/{credential_scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires_seconds)),
        ("X-Amz-SignedHeaders", "host"),
    ]
    canonical_querystring = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(query_params)
    )
    canonical_headers = f"host:{endpoint}\n"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join([
        "GET", canonical_uri, canonical_querystring, canonical_headers, "host", payload_hash,
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode(), hashlib.sha256,
    ).hexdigest()

    final_query = canonical_querystring + "&X-Amz-Signature=" + signature
    if session_token:
        final_query += "&X-Amz-Security-Token=" + urllib.parse.quote(session_token, safe="")
    return f"wss://{endpoint}:443{canonical_uri}?{final_query}"


# --- Logowanie + odkrywanie urządzeń (REST, aiohttp) ---------------------

@dataclass
class HomeWhizSession:
    access_key: str
    secret_key: str
    session_token: str
    expiration: str = ""


async def login(username: str, password: str) -> HomeWhizSession:
    """Loguje się do chmury HomeWhiz - zwraca tymczasowe poświadczenia AWS
    (ważne zwykle ok. godziny, patrz `expiration`), używane do podpisywania
    (SigV4) każdego kolejnego żądania REST i połączenia MQTT."""
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"https://{AUTH_HOST}/auth/login",
                json={"username": username, "password": password},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    raise HomeWhizError("Nieprawidłowy login lub hasło do konta HomeWhiz")
                if resp.status != 200:
                    raise HomeWhizError(f"Logowanie HomeWhiz nie powiodło się (kod {resp.status})")
                data = await resp.json()
    except aiohttp.ClientError as exc:
        raise HomeWhizError(f"Błąd połączenia z chmurą HomeWhiz: {exc}") from exc

    try:
        return HomeWhizSession(
            access_key=data["accessKey"], secret_key=data["secretKey"],
            session_token=data["sessionToken"], expiration=data.get("expiration", ""),
        )
    except KeyError as exc:
        raise HomeWhizError(f"Nieoczekiwana odpowiedź logowania HomeWhiz (brak pola {exc})") from exc


async def _signed_get_json(session: HomeWhizSession, url: str, region: str = DEFAULT_REGION) -> object:
    parsed = urllib.parse.urlsplit(url)
    headers = _sigv4_headers(
        method="GET", host=parsed.netloc, path=parsed.path, query=parsed.query,
        region=region, service="execute-api",
        access_key=session.access_key, secret_key=session.secret_key,
        session_token=session.session_token,
    )
    async with aiohttp.ClientSession() as http:
        async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise HomeWhizError(f"Zapytanie HomeWhiz {url} nie powiodło się (kod {resp.status})")
            return await resp.json()


async def list_appliances(session: HomeWhizSession, region: str = DEFAULT_REGION) -> list[dict]:
    """Zwraca listę urządzeń przypisanych do konta, WYŁĄCZAJĄC urządzenia
    sparowane tylko przez Bluetooth (patrz docstring modułu) - każdy wpis
    to `{id, applianceId, brand, model, applianceType, connectivity, name}`."""
    homes = await _signed_get_json(session, f"https://{SMARTHOME_HOST}/my-homes", region)
    appliances: list[dict] = []
    for home in homes if isinstance(homes, list) else []:
        home_id = home.get("id") if isinstance(home, dict) else None
        if not home_id:
            continue
        detail = await _signed_get_json(session, f"https://{SMARTHOME_HOST}/my-homes/{home_id}", region)
        for item in detail if isinstance(detail, list) else []:
            if not isinstance(item, dict) or not item.get("applianceId"):
                continue
            if item.get("connectivity") in _BLUETOOTH_ONLY_CONNECTIVITY:
                continue
            appliances.append(item)
    return appliances


def is_dishwasher(appliance: dict) -> bool:
    return appliance.get("applianceType") == _APPLIANCE_TYPE_DISHWASHER


async def fetch_config_descriptor(
    session: HomeWhizSession, appliance_id: str, language: str = "en", region: str = DEFAULT_REGION,
) -> dict:
    """Deskryptor konfiguracji urządzenia - lista możliwych stanów
    (`deviceStates.states`, każdy `{strKey, wifiArrayValue}`) i offset
    (`deviceStates.wifiArrayReadIndex`), pod którym w tablicy bajtów stanu
    ("wifi array") szukać bieżącej wartości - patrz `decode_state` niżej."""
    contents_url = (
        f"https://{AUTH_HOST}/procam/contents"
        f"?applianceId={urllib.parse.quote(appliance_id)}&ctype=CONFIGURATION"
        f"&lang={urllib.parse.quote(language)}&testMode=true"
    )
    entries = await _signed_get_json(session, contents_url, region)
    if not isinstance(entries, list) or not entries:
        raise HomeWhizError(f"Brak deskryptora konfiguracji dla urządzenia {appliance_id}")
    entry = entries[0]
    cid, ver, lang = entry.get("cid"), entry.get("ver"), entry.get("lang", language)
    if not cid or not ver:
        raise HomeWhizError(f"Niekompletny deskryptor konfiguracji dla urządzenia {appliance_id}")

    # Sam plik JSON leży na S3 płasko (nie wymaga podpisu SigV4 - to
    # publiczny bucket z zawartością statyczną, tak samo traktuje go
    # integracja Home Assistant).
    s3_url = f"https://s3-eu-west-1.amazonaws.com/procam-contents/CONFIGURATIONS/{cid}/v{ver}/{cid}.{lang}.json"
    async with aiohttp.ClientSession() as http:
        async with http.get(s3_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise HomeWhizError(f"Nie udało się pobrać deskryptora konfiguracji z {s3_url} (kod {resp.status})")
            return await resp.json(content_type=None)


# --- Dekodowanie stanu ("wifi array") -------------------------------------

def clamp_byte(value: int) -> int:
    """Wartości >= 128 w tablicy bajtów HomeWhiz kodują 'to samo pole, ale
    zmienione od ostatniego odczytu' - dla samego ODCZYTU stanu liczy się
    tylko dolne 128 wartości (patrz `to_friendly_name`/integracja HA)."""
    return value if value < 128 else value - 128


def safe_get(data: bytearray | list[int], index: int) -> int:
    if index < 0 or index >= len(data):
        return 0
    return clamp_byte(data[index])


def decode_wifi_array(offset: int, wfa: list[int]) -> bytearray:
    """Odtwarza pełną tablicę bajtów stanu z payloadu MQTT (`wfa` +
    `wfaStartOffset`, patrz docstring modułu) - pola PRZED offsetem nie są
    obecne w `wfa` (offset to indeks pierwszego bajtu, jaki urządzenie
    faktycznie wysyła), stąd wypełnienie zerami z przodu."""
    return bytearray([0] * offset + list(wfa))


def to_friendly_name(str_key: str) -> str:
    """Sanityzuje klucz stanu (np. 'Dishwasher_State_Running') do krótkiego,
    czytelnego tokenu w stylu innych statusów w tym projekcie (patrz np.
    boiler_output - stan kotła to też krótki tekstowy token). Celowo NIE
    odpytujemy własnego API lokalizacji HomeWhiz o "ładną" nazwę - sam
    klucz (obniżony do małych liter, bez typowych prefiksów) wystarcza jako
    wartość statusu maszynowo czytelna, tak jak dla innych integracji w tym
    projekcie."""
    name = str_key.strip()
    for prefix in ("Dishwasher_State_", "Dishwasher_", "State_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.strip("_").lower() or str_key.lower()


def decode_state(config_descriptor: dict, offset: int, wfa: list[int]) -> str | None:
    """Zwraca aktualną (czytelną) nazwę stanu urządzenia na podstawie
    deskryptora konfiguracji (`fetch_config_descriptor`) i bieżącej
    tablicy bajtów stanu z MQTT - `None`, jeśli deskryptor nie ma
    wystarczających danych (np. nieoczekiwany kształt JSON)."""
    device_states = (config_descriptor or {}).get("deviceStates") or {}
    states = device_states.get("states") or []
    read_index = device_states.get("wifiArrayReadIndex")
    if read_index is None or not states:
        return None
    data = decode_wifi_array(offset, wfa)
    current_value = safe_get(data, read_index)
    for state in states:
        if state.get("wifiArrayValue") == current_value:
            return to_friendly_name(state.get("strKey", ""))
    return None


# --- Żywe połączenie (MQTT-over-WebSocket, AWS IoT Core) ------------------

@dataclass
class ApplianceWatch:
    """Jedno monitorowane urządzenie - nazwa (klucz statusu w panelu) +
    id + deskryptor konfiguracji (zapamiętany PO pierwszym pobraniu, żeby
    nie odpytywać S3 przy każdej wiadomości MQTT)."""
    name: str
    appliance_id: str
    config_descriptor: dict | None = field(default=None, repr=False)


class HomeWhizClient:
    """Utrzymuje JEDNO połączenie MQTT-over-WebSocket do AWS IoT Core,
    monitorujące WSZYSTKIE skonfigurowane urządzenia HomeWhiz naraz (jedna
    para poświadczeń/sesja obsługuje dowolną liczbę rzeczy - subskrypcje są
    per `applianceId`, więc nie trzeba osobnego połączenia na urządzenie).
    """

    def __init__(
        self, cfg: dict, on_state: Callable[[str, str], Awaitable[None]],
        history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
    ):
        self.cfg = cfg
        self.on_state = on_state
        self._appliances: dict[str, ApplianceWatch] = {
            name: ApplianceWatch(name=name, appliance_id=details["appliance_id"])
            for name, details in (cfg.get("appliances") or {}).items()
            if details.get("appliance_id")
        }
        self._session: HomeWhizSession | None = None
        self._client = None
        self._loop = asyncio.get_event_loop()
        self._supervisor = Supervisor("homewhiz", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)

    async def run_forever(self) -> None:
        if not self._appliances:
            logger.info("HomeWhiz: brak skonfigurowanych urządzeń - pomijam połączenie")
            return
        await self._supervisor.run(self._connect_and_run)

    async def _connect_and_run(self, on_connected) -> None:
        import paho.mqtt.client as mqtt

        self._session = await login(self.cfg["username"], self.cfg["password"])
        for watch in self._appliances.values():
            if watch.config_descriptor is None:
                try:
                    watch.config_descriptor = await fetch_config_descriptor(
                        self._session, watch.appliance_id, language=self.cfg.get("language", "en"),
                    )
                except HomeWhizError:
                    logger.exception("HomeWhiz: nie udało się pobrać deskryptora konfiguracji dla '%s'", watch.name)

        url = presign_iot_websocket_url(
            endpoint=IOT_ENDPOINT, region=self.cfg.get("region", DEFAULT_REGION),
            access_key=self._session.access_key, secret_key=self._session.secret_key,
            session_token=self._session.session_token,
        )
        parsed = urllib.parse.urlsplit(url)

        connected_event = asyncio.Event()
        disconnected_future: asyncio.Future = self._loop.create_future()

        def _fail(exc: Exception) -> None:
            def _do() -> None:
                connected_event.set()  # odblokuj ewentualne oczekiwanie na połączenie
                if not disconnected_future.done():
                    disconnected_future.set_exception(exc)
            self._loop.call_soon_threadsafe(_do)

        # reconnect_on_failure=False - paho ma WŁASNY wbudowany auto-reconnect,
        # ale użyłby TEGO SAMEGO (już nieważnego po rozłączeniu) podpisanego
        # URL-a WebSocketa. Reconnect musi przejść przez Supervisor.run()
        # wyżej, żeby dostać ŚWIEŻY presigned URL (i - jeśli trzeba - nową
        # sesję logowania).
        client = mqtt.Client(transport="websockets", reconnect_on_failure=False)
        client.tls_set()
        client.ws_set_options(path=f"{parsed.path}?{parsed.query}", headers={"Host": parsed.hostname})

        def _on_connect(_client, _userdata, _flags, rc) -> None:
            if rc == 0:
                self._loop.call_soon_threadsafe(connected_event.set)
            else:
                _fail(ConnectionError(f"kod połączenia MQTT {rc}"))

        def _on_disconnect(_client, _userdata, rc) -> None:
            _fail(ConnectionError(f"rozłączono (kod {rc})"))

        def _on_message(_client, _userdata, msg) -> None:
            asyncio.run_coroutine_threadsafe(self._handle_message(msg.topic, msg.payload), self._loop)

        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message
        self._client = client

        # connect_async (nie connect()) - nie blokuje pętli asyncio na
        # czasie handshake'u TCP/TLS/WebSocket, patrz ten sam wzorzec w
        # bridge/ebus_client.py.
        client.connect_async(parsed.hostname, 443, keepalive=60)
        client.loop_start()
        try:
            await asyncio.wait_for(connected_event.wait(), timeout=15)
            if disconnected_future.done():
                raise disconnected_future.exception()

            for watch in self._appliances.values():
                topic_prefix = f"$aws/things/{watch.appliance_id}/shadow"
                client.subscribe(f"{topic_prefix}/update/accepted")
                client.subscribe(f"{topic_prefix}/get/accepted")
                client.publish(f"{topic_prefix}/get", payload="")
            logger.info("HomeWhiz: połączono, monitoruję %d urządzeń", len(self._appliances))
            await on_connected()

            await disconnected_future
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        import json

        appliance_id = topic.split("/")[2] if topic.startswith("$aws/things/") else None
        watch = next((w for w in self._appliances.values() if w.appliance_id == appliance_id), None)
        if watch is None or watch.config_descriptor is None:
            return
        try:
            data = json.loads(payload)
            reported = data["state"]["reported"]
            wfa = reported["wfa"]
            offset = reported.get("wfaStartOffset", 26)
        except (ValueError, KeyError, TypeError):
            logger.debug("HomeWhiz: pominięto nieoczekiwaną wiadomość na %s", topic)
            return

        state_name = decode_state(watch.config_descriptor, offset, wfa)
        if state_name is None:
            return
        logger.debug("HomeWhiz '%s': stan -> %s", watch.name, state_name)
        await self.on_state(watch.name, state_name)
