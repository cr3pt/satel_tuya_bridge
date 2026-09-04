from __future__ import annotations

import asyncio
import contextlib
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
_BLUETOOTH_ONLY_CONNECTIVITY = {"BT", "BASICBT"}
_CONFIG_DESCRIPTOR_RETRY_SECONDS = 300


class HomeWhizError(Exception):
    pass


def _amz_dates(now: datetime.datetime | None = None) -> tuple[str, str]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = hmac.new(("AWS4" + secret_key).encode(), date_stamp.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _canonical_query_string(query: str) -> str:
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
    del result["host"]
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


@dataclass
class HomeWhizSession:
    access_key: str
    secret_key: str
    session_token: str
    expiration_ms: int = 0


def _unwrap_envelope(contents: object, *, context: str) -> object:
    if not isinstance(contents, dict):
        raise HomeWhizError(f"Nieoczekiwana odpowiedź HomeWhiz ({context}): {contents!r}")
    if "success" in contents and not contents["success"]:
        raise HomeWhizError(f"Chmura HomeWhiz zgłosiła błąd ({context}): {contents}")
    if "data" not in contents:
        raise HomeWhizError(f"Nieoczekiwana odpowiedź HomeWhiz ({context}, brak pola 'data'): {contents}")
    return contents["data"]


async def login(username: str, password: str) -> HomeWhizSession:
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"https://{AUTH_HOST}/auth/login",
                json={"username": username, "password": password},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
                if resp.status == 401 or resp.status == 403:
                    raise HomeWhizError("Nieprawidłowy login lub hasło do konta HomeWhiz")
                if resp.status != 200:
                    raise HomeWhizError(f"Logowanie HomeWhiz nie powiodło się (kod {resp.status})")
    except aiohttp.ClientError as exc:
        raise HomeWhizError(f"Błąd połączenia z chmurą HomeWhiz: {exc}") from exc

    data = _unwrap_envelope(body, context="logowanie")
    try:
        credentials = data["credentials"]
        return HomeWhizSession(
            access_key=credentials["accessKey"], secret_key=credentials["secretKey"],
            session_token=credentials["sessionToken"], expiration_ms=credentials.get("expiration", 0),
        )
    except KeyError as exc:
        raise HomeWhizError(f"Nieoczekiwana odpowiedź logowania HomeWhiz (brak pola {exc})") from exc


async def _signed_get_json(session: HomeWhizSession, url: str, region: str = DEFAULT_REGION,
                            *, context: str) -> object:
    parsed = urllib.parse.urlsplit(url)
    headers = _sigv4_headers(
        method="GET", host=parsed.netloc, path=parsed.path, query=parsed.query,
        region=region, service="execute-api",
        access_key=session.access_key, secret_key=session.secret_key,
        session_token=session.session_token,
    )
    async with aiohttp.ClientSession() as http:
        async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                raise HomeWhizError(f"Zapytanie HomeWhiz {url} nie powiodło się (kod {resp.status})")
    return _unwrap_envelope(body, context=context)


async def list_appliances(session: HomeWhizSession, region: str = DEFAULT_REGION) -> list[dict]:
    homes = await _signed_get_json(session, f"https://{SMARTHOME_HOST}/my-homes", region, context="lista domów")
    appliances: list[dict] = []
    for home in homes if isinstance(homes, list) else []:
        home_id = home.get("id") if isinstance(home, dict) else None
        if not home_id:
            continue
        detail = await _signed_get_json(
            session, f"https://{SMARTHOME_HOST}/my-homes/{home_id}", region, context="urządzenia domu",
        )
        for item in (detail.get("appliances") or []) if isinstance(detail, dict) else []:
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
    contents_url = (
        f"https://{AUTH_HOST}/procam/contents"
        f"?applianceId={urllib.parse.quote(appliance_id)}&ctype=CONFIGURATION"
        f"&lang={urllib.parse.quote(language)}&testMode=true"
    )
    data = await _signed_get_json(session, contents_url, region, context="deskryptor konfiguracji")
    entries = data.get("results") if isinstance(data, dict) else None
    if not entries:
        raise HomeWhizError(f"Brak deskryptora konfiguracji dla urządzenia {appliance_id}")
    entry = entries[0]
    cid, ver, lang = entry.get("cid"), entry.get("ver"), entry.get("lang", language)
    if not cid or not ver:
        raise HomeWhizError(f"Niekompletny deskryptor konfiguracji dla urządzenia {appliance_id}")

    s3_url = f"https://s3-eu-west-1.amazonaws.com/procam-contents/CONFIGURATIONS/{cid}/v{ver}/{cid}.{lang}.json"
    async with aiohttp.ClientSession() as http:
        async with http.get(s3_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise HomeWhizError(f"Nie udało się pobrać deskryptora konfiguracji z {s3_url} (kod {resp.status})")
            return await resp.json(content_type=None)


def clamp_byte(value: int) -> int:
    return value if value < 128 else value - 128


def safe_get(data: bytearray | list[int], index: int) -> int:
    if index < 0 or index >= len(data):
        return 0
    return clamp_byte(data[index])


def decode_wifi_array(offset: int, wfa: list[int]) -> bytearray:
    return bytearray([0] * offset + list(wfa))


def to_friendly_name(str_key: str) -> str:
    name = str_key.strip()
    for prefix in ("Dishwasher_State_", "Dishwasher_", "State_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.strip("_").lower() or str_key.lower()


def decode_state(config_descriptor: dict, offset: int, wfa: list[int]) -> str | None:
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


def list_possible_states(config_descriptor: dict) -> list[str]:
    device_states = (config_descriptor or {}).get("deviceStates") or {}
    states = device_states.get("states") or []
    names: list[str] = []
    for state in states:
        name = to_friendly_name(state.get("strKey", ""))
        if name and name not in names:
            names.append(name)
    return names


@dataclass
class ApplianceWatch:
    name: str
    appliance_id: str
    config_descriptor: dict | None = field(default=None, repr=False)


class HomeWhizClient:

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
        await self._fetch_missing_config_descriptors()

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
                connected_event.set()
                if not disconnected_future.done():
                    disconnected_future.set_exception(exc)
            self._loop.call_soon_threadsafe(_do)

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            transport="websockets", reconnect_on_failure=False,
        )
        client.tls_set()
        client.ws_set_options(path=f"{parsed.path}?{parsed.query}", headers={"Host": parsed.hostname})

        def _on_connect(_client, _userdata, _connect_flags, reason_code, _properties) -> None:
            if reason_code == 0:
                self._loop.call_soon_threadsafe(connected_event.set)
            else:
                _fail(ConnectionError(f"kod połączenia MQTT {reason_code}"))

        def _on_disconnect(_client, _userdata, _disconnect_flags, reason_code, _properties) -> None:
            _fail(ConnectionError(f"rozłączono (kod {reason_code})"))

        def _on_message(_client, _userdata, msg) -> None:
            asyncio.run_coroutine_threadsafe(self._handle_message(msg.topic, msg.payload), self._loop)

        pending_subscriptions: dict[int, str] = {}

        def _on_subscribe(_client, _userdata, mid, reason_code_list, _properties) -> None:
            topic = pending_subscriptions.pop(mid, "?")
            codes = [rc.value for rc in reason_code_list]
            if any(rc.is_failure for rc in reason_code_list):
                logger.warning(
                    "HomeWhiz: broker odrzucił subskrypcję %s (kody: %s) - sprawdź uprawnienia "
                    "polityki IoT dla tego urządzenia", topic, codes,
                )
            else:
                logger.info("HomeWhiz: subskrypcja %s potwierdzona przez broker (kody: %s)", topic, codes)

        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message
        client.on_subscribe = _on_subscribe
        self._client = client

        client.connect_async(parsed.hostname, 443, keepalive=60)
        client.loop_start()
        try:
            await asyncio.wait_for(connected_event.wait(), timeout=15)
            if disconnected_future.done():
                raise disconnected_future.exception()

            for watch in self._appliances.values():
                topic_prefix = f"$aws/things/{watch.appliance_id}/shadow"
                for topic in (
                    f"{topic_prefix}/update/accepted", f"{topic_prefix}/get/accepted",
                    f"{topic_prefix}/get/rejected", f"$aws/things/{watch.appliance_id}/#",
                ):
                    _, mid = client.subscribe(topic)
                    pending_subscriptions[mid] = topic
                client.publish(f"{topic_prefix}/get", payload="{}")
            logger.info("HomeWhiz: połączono, monitoruję %d urządzeń", len(self._appliances))
            await on_connected()

            retry_task = asyncio.ensure_future(self._retry_missing_config_descriptors_loop())
            try:
                await disconnected_future
            finally:
                retry_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retry_task
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass

    async def _fetch_missing_config_descriptors(self) -> None:
        for watch in self._appliances.values():
            if watch.config_descriptor is not None:
                continue
            try:
                watch.config_descriptor = await fetch_config_descriptor(
                    self._session, watch.appliance_id, language=self.cfg.get("language", "en"),
                )
            except HomeWhizError:
                logger.exception("HomeWhiz: nie udało się pobrać deskryptora konfiguracji dla '%s'", watch.name)

    async def _retry_missing_config_descriptors_loop(self) -> None:
        while True:
            await asyncio.sleep(_CONFIG_DESCRIPTOR_RETRY_SECONDS)
            if not any(w.config_descriptor is None for w in self._appliances.values()):
                return
            logger.info("HomeWhiz: ponawiam pobieranie brakujących deskryptorów konfiguracji")
            await self._fetch_missing_config_descriptors()

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        import json

        appliance_id = topic.split("/")[2] if topic.startswith("$aws/things/") else None
        watch = next((w for w in self._appliances.values() if w.appliance_id == appliance_id), None)
        if watch is None:
            logger.debug("HomeWhiz: wiadomość na %s nie pasuje do żadnego skonfigurowanego urządzenia", topic)
            return
        if watch.config_descriptor is None:
            logger.info("HomeWhiz '%s': pominięto wiadomość na %s - brak jeszcze deskryptora konfiguracji",
                        watch.name, topic)
            return
        try:
            data = json.loads(payload)
            reported = data["state"]["reported"]
            wfa = reported["wfa"]
            offset = reported.get("wfaStartOffset", 26)
        except (ValueError, KeyError, TypeError):
            logger.info("HomeWhiz '%s': nieoczekiwana struktura wiadomości na %s: %s",
                        watch.name, topic, payload.decode("utf-8", "replace")[:500])
            return

        try:
            state_name = decode_state(watch.config_descriptor, offset, wfa)
            if state_name is None:
                logger.info(
                    "HomeWhiz '%s': brak dopasowanego stanu w deskryptorze konfiguracji "
                    "(offset=%s, wfa=%s) - deskryptor może nie pasować do tego urządzenia",
                    watch.name, offset, wfa,
                )
                return
            logger.info("HomeWhiz '%s': stan -> %s", watch.name, state_name)
            await self.on_state(watch.name, state_name)
        except Exception:
            logger.exception("HomeWhiz '%s': błąd przetwarzania wiadomości na %s", watch.name, topic)
