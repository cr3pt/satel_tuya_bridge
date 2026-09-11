from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

import tinytuya

logger = logging.getLogger("bridge.tuya_local")


def _reason_suffix(reason: str | None) -> str:
    return f" - {reason}" if reason else ""


DP_LABEL_TRANSLATIONS = {
    "switch": "włącznik", "switch_1": "włącznik",
    "va_temperature": "temperatura", "temp_current": "temperatura",
    "va_humidity": "wilgotność", "humidity_value": "wilgotność",
    "co2_value": "CO2", "pm25_value": "PM2.5", "ch4_sensor_value": "metan",
    "watersensor_state": "czujnik zalania", "doorcontact_state": "kontaktron",
    "pir": "ruch", "battery_percentage": "bateria", "battery_state": "stan baterii",
    "cur_power": "moc", "cur_voltage": "napięcie", "cur_current": "prąd",
    "bright_value": "jasność", "gas_sensor_state": "czujnik gazu",
    "smoke_sensor_status": "czujnik dymu", "alarm_switch": "alarm",
    "forward_energy_total": "energia pobrana (całkowita)",
    "reverse_energy_total": "energia oddana (całkowita)",
    "phase_a": "faza A", "phase_b": "faza B", "phase_c": "faza C",
    "fault": "kod awarii", "energy_reset": "reset licznika energii",
    "alarm_set_1": "próg alarmowy 1", "alarm_set_2": "próg alarmowy 2",
    "breaker_number": "numer licznika", "supply_frequency": "częstotliwość zasilania",
    "online_state": "stan online",
    "basic_flip": "obrót obrazu", "basic_osd": "znacznik czasu na obrazie",
    "basic_private": "tryb prywatności", "basic_nightvision": "noktowizja",
    "motion_sensitivity": "czułość detekcji ruchu", "motion_switch": "detekcja ruchu",
    "motion_tracking": "śledzenie ruchu", "motion_area_switch": "strefy detekcji ruchu",
    "motion_area": "obszar detekcji ruchu", "humanoid_filter": "filtr sylwetki",
    "sd_storge": "karta SD (całość|wolne|użyte, KB)", "sd_status": "stan karty SD",
    "sd_format": "formatowanie karty SD", "sd_format_state": "stan formatowania karty SD",
    "movement_detect_pic": "zdjęcie z detekcji ruchu",
    "alarm_message": "wiadomość alarmowa",
    "ptz_stop": "PTZ: zatrzymano", "ptz_control": "PTZ: sterowanie",
    "ptz_calibration": "PTZ: kalibracja",
    "decibel_switch": "detekcja dźwięku", "decibel_sensitivity": "czułość detekcji dźwięku",
    "record_switch": "nagrywanie", "record_mode": "tryb nagrywania",
    "siren_switch": "syrena",
    "manual_delay": "opóźnienie ręczne", "countdown": "odliczanie", "auto_manual": "tryb automatyczny",
}

_PHASE_RAW_CODES = {"phase_a", "phase_b", "phase_c"}
_KNOWN_VALUE_SCALES = {
    "forward_energy_total": (2, " kWh"),
    "reverse_energy_total": (2, " kWh"),
    # Fallback dla urządzeń, których dp_schema nie zawiera meta "scale" (typ "Value")
    # - bez tego surowa wartość DP (np. 256) wyświetlałaby się bez podzielenia przez 10
    # (25.6C), zamiast poprawnie przeskalowanej wartości.
    "va_temperature": (1, "°C"),
    "temp_current": (1, "°C"),
}
_MAX_INLINE_VALUE_LENGTH = 60

_CAMERA_INDICATOR_CODES = {
    "sd_status", "sd_storge", "sd_format", "sd_format_state",
    "basic_osd", "basic_flip", "basic_private", "basic_nightvision",
    "basic_indicator", "basic_anti_flicker", "basic_device_volume",
    "motion_area", "motion_area_switch", "humanoid_filter",
    "ptz_stop", "ptz_control", "ptz_calibration",
}


def _looks_like_camera(codes: set[str]) -> bool:
    if any(code.startswith("ipc_") for code in codes):
        return True
    return len(codes & _CAMERA_INDICATOR_CODES) >= 3


def _decode_phase_raw(value: str) -> str | None:
    try:
        raw = base64.b64decode(value)
    except Exception:
        return None
    if len(raw) != 8:
        return None
    voltage = int.from_bytes(raw[0:2], "big") / 10
    current = int.from_bytes(raw[2:5], "big") / 1000
    power = int.from_bytes(raw[5:8], "big")
    return f"{voltage:g}V {current:g}A {power}W"


def scan_network(
    maxretry: int = 18, networks: list[str] | None = None, tuyadevices: list[dict] | None = None,
) -> list[dict]:
    """Skanuje LAN w poszukiwaniu urządzeń Tuya.

    Domyślnie (bez `networks`) słucha tylko broadcastu UDP, tak jak
    dotychczas - to zawodzi, gdy broadcast nie dociera do bridge'a (Docker
    bez trybu `network: host`, VLAN-y, osobna podsieć routera). Podanie
    `networks` (lista adresów IP i/albo zakresów CIDR) włącza aktywne
    skanowanie TCP tych adresów przez tinytuya (`forcescan`) - wymaga
    listy znanych par ID+`local_key` (`tuyadevices`), bo tinytuya identyfikuje
    urządzenie próbą uzgodnienia (handshake) danym kluczem, a nie samym
    nasłuchem broadcastu.

    `maxretry` nazwą myli - to NIE liczba prób, tylko `scantime` tinytuya:
    ile sekund nasłuchiwać broadcastu UDP (Tuya rozgłasza się cyklicznie,
    nie w sposób ciągły, więc zbyt krótki czas nasłuchu regularnie przegapia
    urządzenia, które widać w samym `tinytuya`/`python -m tinytuya scan`
    uruchomionym ręcznie z dłuższym czasem). Domyślne 18s pokrywa się z
    własnym domyślnym `tinytuya.SCANTIME` i z czasem obiecanym w UI ("ok.
    18s") - NIE zaniżać bez zmiany tekstu w UI.
    """
    if networks:
        from tinytuya import scanner as tinytuya_scanner
        raw = tinytuya_scanner.devices(
            scantime=maxretry, poll=False, forcescan=list(networks),
            tuyadevices=list(tuyadevices or []), verbose=False, color=False, discover=True,
        ) or {}
    else:
        raw = tinytuya.deviceScan(verbose=False, maxretry=maxretry, poll=False)
    devices = []
    for ip, info in raw.items():
        devices.append({
            "id": info.get("gwId") or info.get("id") or "",
            "ip": ip,
            "version": info.get("version", ""),
        })
    return devices


TUYA_CLOUD_REGIONS = {
    "eu": "Europa (Centralna)",
    "eu-w": "Europa (Zachodnia)",
    "us": "USA (Zachodnie wybrzeże)",
    "us-e": "USA (Wschodnie wybrzeże)",
    "in": "Indie",
    "sg": "Singapur",
    "cn": "Chiny",
}


def _login_cloud(api_region: str, access_id: str, access_key: str) -> tinytuya.Cloud:
    # .strip() - wklejony Access ID/Secret z konsoli Tuya IoT często niesie ze sobą
    # niewidoczną spację/nową linię, co Tuya Cloud zwraca jako mylące błędy logowania
    # zamiast jasno powiedzieć "sprawdź białe znaki".
    try:
        cloud = tinytuya.Cloud(
            apiRegion=(api_region or "").strip(), apiKey=(access_id or "").strip(), apiSecret=(access_key or "").strip(),
        )
    except Exception as exc:
        raise ConnectionError(f"Błąd logowania do Tuya Cloud: {exc}") from exc
    if getattr(cloud, "error", None):
        message = cloud.error.get("Error") if isinstance(cloud.error, dict) else str(cloud.error)
        raise ConnectionError(f"Błąd logowania do Tuya Cloud: {message}")
    return cloud


def fetch_cloud_devices(
    api_region: str, access_id: str, access_key: str, networks: list[str] | None = None,
) -> list[dict]:
    cloud = _login_cloud(api_region, access_id, access_key)

    try:
        result = cloud.getdevices()
    except Exception as exc:
        raise ConnectionError(f"Błąd pobierania urządzeń z Tuya Cloud: {exc}") from exc

    if isinstance(result, dict) and result.get("Error"):
        raise ConnectionError(f"Błąd pobierania urządzeń z Tuya Cloud: {result['Error']}")
    if not isinstance(result, list):
        raise ConnectionError("Nieoczekiwana odpowiedź z Tuya Cloud")

    if not result:
        raw = getattr(cloud, "getdevices_raw", None)
        if isinstance(raw, dict) and raw.get("success") is False:
            message = raw.get("msg") or f"kod błędu {raw.get('code', '?')}"
            raise ConnectionError(
                f"Tuya Cloud zwrócił błąd zamiast listy urządzeń: {message}. Sprawdź: "
                "(1) Access ID/Access Secret (bez spacji na początku/końcu), "
                "(2) czy wybrany region odpowiada Data Center Twojego konta w apce "
                "Tuya Smart/Smart Life (niedopasowanie regionu to częsta przyczyna "
                "tego błędu mimo poprawnych kluczy), (3) czy w projekcie na "
                "developer.tuya.com wykonano Devices → Link Tuya App Account."
            )

    try:
        tuyadevices_for_scan = [
            {"id": d["id"], "key": d["key"], "name": d.get("name", "")}
            for d in result if d.get("id") and d.get("key")
        ]
        lan_devices = {
            d["id"]: d
            for d in scan_network(networks=networks, tuyadevices=tuyadevices_for_scan)
            if d.get("id")
        }
    except Exception:
        logger.warning("Skan LAN podczas importu z Tuya Cloud nie powiódł się - kontynuuję bez adresów IP", exc_info=True)
        lan_devices = {}

    gateway_ids = {d.get("gateway_id") for d in result if d.get("gateway_id")}

    devices = []
    for d in result:
        dev_id = d.get("id", "")
        lan_match = lan_devices.get(dev_id, {})
        dp_schema = {}
        if dev_id:
            try:
                dp_schema = _parse_dp_schema_result(cloud.getdps(dev_id))
            except Exception:
                logger.warning(
                    "Nie udało się pobrać opisu pól DP dla urządzenia '%s' - status pokaże "
                    "surowe wartości zamiast czytelnych etykiet",
                    d.get("name", dev_id), exc_info=True,
                )
        devices.append({
            "id": dev_id,
            "name": d.get("name", ""),
            "local_key": d.get("key", ""),
            "ip": lan_match.get("ip", ""),
            "version": lan_match.get("version", ""),
            "category": d.get("category", ""),
            "gateway_id": d.get("gateway_id", ""),
            "node_id": d.get("node_id", ""),
            "sub": bool(d.get("sub", False)),
            # Bramka Zigbee - wykryta bez zgadywania kodów kategorii Tuya: to urządzenie,
            # na które w tym samym imporcie wskazuje pole gateway_id innego urządzenia.
            "is_gateway": dev_id in gateway_ids,
            "dp_schema": dp_schema,
        })
    return devices


def _parse_dp_schema_result(result: object) -> dict:
    if not isinstance(result, dict) or not result.get("success"):
        message = result.get("msg", "nieznany błąd") if isinstance(result, dict) else "brak odpowiedzi"
        raise ConnectionError(f"Błąd pobierania opisu pól z Tuya Cloud: {message}")
    data = result.get("result") or {}
    schema: dict[str, dict] = {}
    for group in ("status", "functions"):
        for entry in data.get(group) or []:
            dp_id = entry.get("dp_id")
            if dp_id is None:
                continue
            schema.setdefault(str(dp_id), {
                "code": entry.get("code", ""),
                "type": entry.get("type", ""),
                "values": entry.get("values", ""),
            })
    return schema


def fetch_device_dp_schema(api_region: str, access_id: str, access_key: str, device_id: str) -> dict:
    cloud = _login_cloud(api_region, access_id, access_key)
    try:
        result = cloud.getdps(device_id)
    except Exception as exc:
        raise ConnectionError(f"Błąd pobierania opisu pól z Tuya Cloud: {exc}") from exc
    return _parse_dp_schema_result(result)


def fetch_dp_schemas_for_devices(
    api_region: str, access_id: str, access_key: str, device_ids: list[str],
) -> tuple[dict[str, dict], dict[str, str]]:
    cloud = _login_cloud(api_region, access_id, access_key)
    schemas: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for device_id in device_ids:
        try:
            schemas[device_id] = _parse_dp_schema_result(cloud.getdps(device_id))
        except Exception as exc:
            errors[device_id] = str(exc)
    return schemas, errors


_SWITCH_CODE_PATTERN = re.compile(r"^switch(_\d+)?$")


def is_switch_code(code: str) -> bool:
    return code == "switch_led" or bool(_SWITCH_CODE_PATTERN.match(code))


def find_switch_keys_from_schema(schema: dict) -> list[str]:
    return [
        dp_id for dp_id, spec in schema.items()
        if spec.get("type") == "Boolean" and is_switch_code(spec.get("code") or "")
    ]


def find_switch_codes_from_functions_result(result: object) -> list[str]:
    if not isinstance(result, dict) or not result.get("success"):
        return []
    data = result.get("result") or {}
    codes = []
    for entry in data.get("functions") or []:
        code = entry.get("code") or ""
        if entry.get("type") == "Boolean" and is_switch_code(code):
            codes.append(code)
    return codes


def _dp_code(key: str, spec: dict | None) -> str:
    return (spec or {}).get("code") or key


def _dp_label(code: str) -> str:
    return DP_LABEL_TRANSLATIONS.get(code, code.replace("_", " "))


def _dp_format_value(value: object, spec: dict | None, code: str) -> object:
    spec = spec or {}
    if code in _PHASE_RAW_CODES and isinstance(value, str):
        decoded = _decode_phase_raw(value)
        if decoded is not None:
            return decoded
    if isinstance(value, bool):
        return "tak" if value else "nie"
    if isinstance(value, (int, float)):
        scale, unit = None, ""
        if spec.get("type") == "Value":
            try:
                meta = json.loads(spec.get("values") or "{}")
            except (ValueError, TypeError):
                meta = {}
            scale = meta.get("scale")
            unit = meta.get("unit", "")
        if scale is None and code in _KNOWN_VALUE_SCALES:
            scale, unit = _KNOWN_VALUE_SCALES[code]
        if scale:
            value = round(value / (10 ** scale), 2)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
        return f"{value}{unit}"
    if isinstance(value, str) and len(value) > _MAX_INLINE_VALUE_LENGTH:
        return f"(dane binarne, {len(value)} znaków)"
    return value


def coerce_dp_value(value: object) -> object:
    """Zamienia tekst z formularza na typ oczekiwany przez większość DP Tuya -
    bool dla "true"/"false", int/float dla liczb, w przeciwnym razie zwraca
    oryginalny tekst (np. dla DP typu Enum, gdzie wartością jest sama nazwa)."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    return value


def find_tuya_switch_key(dps: dict, schema: dict | None = None, preferred: str | None = None) -> str | None:
    if preferred and preferred in dps:
        return preferred
    schema = schema or {}
    for key, spec in schema.items():
        if key in dps and spec.get("type") == "Boolean" and str(spec.get("code") or key).startswith("switch"):
            return key
    for key in dps:
        if str(key).startswith("switch"):
            return key
    return "1" if "1" in dps else None


def format_tuya_dps(dps: dict, schema: dict | None = None, preferred_switch_key: str | None = None) -> tuple[object, str]:
    schema = schema or {}
    switch_key = find_tuya_switch_key(dps, schema, preferred=preferred_switch_key)
    if switch_key is None:
        codes = {_dp_code(key, schema.get(key)) for key in dps}
        if _looks_like_camera(codes):
            return None, "działa"
    parts = []
    for key, value in dps.items():
        if key == switch_key or value is None:
            continue
        spec = schema.get(key)
        code = _dp_code(key, spec)
        parts.append(f"{_dp_label(code)}: {_dp_format_value(value, spec, code)}")
    is_on = dps.get(switch_key) if switch_key else None
    return is_on, " | ".join(parts)


_TEMPERATURE_CODES = ("va_temperature", "temp_current")
_HUMIDITY_CODES = ("va_humidity", "humidity_value")
_LEAK_CODES = ("watersensor_state",)
_CONTACT_CODES = ("doorcontact_state",)
_BATTERY_CODES = ("battery_percentage",)
_TEMP_UNIT_CODES = ("temp_unit_convert",)

_DEFAULT_HOMEKIT_SCALES = {"va_temperature": 1, "temp_current": 1}

_CLOUD_CATEGORY_SENSOR_TYPES = {
    "wsdcg": "temp_humidity_sensor",
    "sj": "leak_sensor",
    "mcs": "contact_sensor",
}


def _dp_numeric_value(value: object, spec: dict | None, code: str) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    spec = spec or {}
    scale = None
    if spec.get("type") == "Value":
        try:
            meta = json.loads(spec.get("values") or "{}")
        except (ValueError, TypeError):
            meta = {}
        scale = meta.get("scale")
    if scale is None:
        scale = _DEFAULT_HOMEKIT_SCALES.get(code, 0)
    return value / (10 ** scale) if scale else float(value)


def extract_tuya_sensor_readings(dps: dict, schema: dict | None = None) -> dict:
    schema = schema or {}
    by_code = {}
    for key, value in dps.items():
        if value is None:
            continue
        spec = schema.get(key)
        by_code[_dp_code(key, spec)] = (value, spec)

    readings: dict = {}

    temp_unit = None
    for code in _TEMP_UNIT_CODES:
        if code in by_code:
            temp_unit = str(by_code[code][0]).strip().lower()

    for code in _TEMPERATURE_CODES:
        if code in by_code:
            value, spec = by_code[code]
            numeric = _dp_numeric_value(value, spec, code)
            if numeric is not None:
                if temp_unit == "f":
                    numeric = (numeric - 32) * 5 / 9
                readings["temperature_celsius"] = round(numeric, 1)
            break

    # Kombinowane czujniki zewnętrzne (np. gleba+powietrze) potrafią wystawić OBIE
    # wartości wilgotności naraz na osobnych DP - pierwsza znaleziona (kolejność
    # _HUMIDITY_CODES: va_humidity, potem humidity_value) trafia do głównego
    # "humidity_percent" (jak dotychczas, dla zwykłych czujników z JEDNĄ wartością),
    # a DRUGA - o ile też jest obecna na tym samym urządzeniu - do
    # "soil_humidity_percent" (na czujnikach kombo humidity_value oznacza
    # wilgotność gleby, w odróżnieniu od va_humidity = wilgotność powietrza).
    humidity_codes_present = [c for c in _HUMIDITY_CODES if c in by_code]
    for i, code in enumerate(humidity_codes_present):
        value, spec = by_code[code]
        numeric = _dp_numeric_value(value, spec, code)
        if numeric is None:
            continue
        numeric = max(0.0, min(100.0, numeric))
        readings["humidity_percent" if i == 0 else "soil_humidity_percent"] = numeric

    for code in _LEAK_CODES:
        if code in by_code:
            value, _spec = by_code[code]
            if isinstance(value, bool):
                readings["leak_detected"] = value
            else:
                readings["leak_detected"] = str(value).strip().lower() not in ("normal", "0", "false", "")
            break

    for code in _CONTACT_CODES:
        if code in by_code:
            value, _spec = by_code[code]
            readings["contact_open"] = bool(value)
            break

    for code in _BATTERY_CODES:
        if code in by_code:
            value, spec = by_code[code]
            numeric = _dp_numeric_value(value, spec, code)
            if numeric is not None:
                readings["battery_percent"] = numeric
            break

    return readings


def detect_tuya_sensor_type(device_cfg: dict) -> str:
    schema = (device_cfg or {}).get("dp_schema") or {}
    codes = {spec.get("code") for spec in schema.values() if spec.get("code")}
    if codes & set(_LEAK_CODES):
        return "leak_sensor"
    if codes & set(_CONTACT_CODES):
        return "contact_sensor"
    has_temp = bool(codes & set(_TEMPERATURE_CODES))
    has_humidity = bool(codes & set(_HUMIDITY_CODES))
    if has_temp and has_humidity:
        return "temp_humidity_sensor"
    if has_temp:
        return "temperature_sensor"
    if has_humidity:
        return "humidity_sensor"
    return _CLOUD_CATEGORY_SENSOR_TYPES.get((device_cfg or {}).get("category"), "")


class TuyaLocalController:
    def __init__(self, devices_cfg: dict, zigbee_gateways_cfg: dict | None = None,
                 retries: int = 2, retry_delay: float = 1.5):
        self.devices_cfg = devices_cfg
        self.zigbee_gateways_cfg = zigbee_gateways_cfg or {}
        self.retries = retries
        self.retry_delay = retry_delay

        self._gateways: dict[str, tinytuya.Device] = {}
        # Jeden asyncio.Lock na bramkę: wszystkie podurządzenia Zigbee pod tą samą
        # bramką dzielą JEDEN obiekt tinytuya.Device (i jego jedno gniazdo TCP) jako
        # `parent` - tinytuya nie jest bezpieczne wątkowo, więc równoległe odpytanie
        # (get_all_statuses robi to przez asyncio.gather na wielu wątkach naraz)
        # dwóch podurządzeń tej samej bramki w tym samym momencie psuje współdzielone
        # gniazdo obu wywołaniom na raz - stąd masowe, pozornie przypadkowe błędy
        # "Check device key or version"/"Network Error" na wszystkich urządzeniach
        # danej bramki jednocześnie. Blokada serializuje ruch per bramka (różne
        # bramki i zwykłe urządzenia WiFi nadal działają w pełni równolegle).
        self._gateway_locks: dict[str, asyncio.Lock] = {}
        for name, cfg in self.zigbee_gateways_cfg.items():
            # .strip() na ID/IP/local_key: config może pochodzić nie tylko z panelu
            # (gdzie pola są już przycięte w JS), ale też z importu paczki YAML albo
            # ręcznej edycji configu - niewidoczna spacja/nowa linia na końcu
            # local_key wygląda identycznie jak poprawny klucz, a tinytuya i tak
            # zgłasza to jako mylące "Check device key or version".
            gw = tinytuya.Device(cfg["id"].strip(), (cfg.get("ip") or "").strip(), cfg["local_key"].strip())
            gw.set_version(cfg.get("version") or 3.4)
            self._gateways[name] = gw
            self._gateway_locks[name] = asyncio.Lock()

        self._devices: dict[str, tinytuya.Device] = {}
        for name, cfg in devices_cfg.items():
            gateway_name = cfg.get("gateway")
            if gateway_name:
                gateway = self._gateways.get(gateway_name)
                if gateway is None:
                    logger.error(
                        "Urządzenie Tuya '%s' wskazuje na nieistniejącą bramkę Zigbee '%s' - "
                        "pomijam je (sprawdź zakładkę ⚙ Ustawienia -> Tuya -> Bramki Zigbee)",
                        name, gateway_name,
                    )
                    continue
                dev_id = cfg["id"].strip()
                dev = tinytuya.Device(
                    dev_id, cid=(cfg.get("cid") or dev_id).strip(), dev_type="zigbee", parent=gateway,
                )
            else:
                dev = tinytuya.OutletDevice(cfg["id"].strip(), cfg["ip"].strip(), cfg["local_key"].strip())
                dev.set_version(cfg.get("version") or 3.4)
            self._devices[name] = dev

    def _gateway_lock_for(self, device_name: str) -> asyncio.Lock | None:
        gateway_name = self.devices_cfg.get(device_name, {}).get("gateway")
        return self._gateway_locks.get(gateway_name) if gateway_name else None

    async def _call_with_retry(self, device_name: str, func, *args):
        loop = asyncio.get_event_loop()
        gateway_lock = self._gateway_lock_for(device_name)
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                if gateway_lock is not None:
                    async with gateway_lock:
                        result = await loop.run_in_executor(None, func, *args)
                else:
                    result = await loop.run_in_executor(None, func, *args)
                if isinstance(result, dict) and "Error" in result:
                    last_error = result["Error"]
                    raise RuntimeError(last_error)
                return result
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    logger.warning(
                        "Tuya '%s': błąd komunikacji (próba %s/%s): %s - ponawiam",
                        device_name, attempt + 1, self.retries + 1, exc,
                    )
                    await asyncio.sleep(self.retry_delay)
        logger.error("Tuya '%s': wyczerpano próby (%s), ostatni błąd: %s", device_name, self.retries + 1, last_error)
        raise ConnectionError(f"Nie udało się skomunikować z urządzeniem Tuya '{device_name}': {last_error}")

    def _switch_keys(self, device_name: str) -> list[str]:
        schema = self.devices_cfg.get(device_name, {}).get("dp_schema") or {}
        return find_switch_keys_from_schema(schema)

    async def _set_switch_value(self, device_name: str, dev, keys: list[str], value: bool) -> None:
        if len(keys) > 1:
            await self._call_with_retry(device_name, dev.set_multiple_values, {k: value for k in keys})
        else:
            await self._call_with_retry(device_name, dev.set_status, value, keys[0])

    async def turn_on(
        self, device_name: str, dimmer_level_percent: int | None = None, reason: str | None = None,
    ) -> None:
        cfg = self.devices_cfg[device_name]
        dev = self._devices[device_name]
        switch_keys = self._switch_keys(device_name)
        suffix = _reason_suffix(reason)

        if cfg.get("type") == "dimmer" and dimmer_level_percent is not None:
            dp = cfg["dimmer_dp"]
            lo, hi = cfg.get("dimmer_range", [10, 1000])
            value = int(lo + (hi - lo) * (dimmer_level_percent / 100))
            if switch_keys:
                await self._set_switch_value(device_name, dev, switch_keys, True)
            else:
                await self._call_with_retry(device_name, dev.set_status, True, "1")
            await self._call_with_retry(device_name, dev.set_value, dp, value)
            logger.info(
                "Ustawiono '%s' na %s%% jasności (DP=%s)%s", device_name, dimmer_level_percent, value, suffix,
            )
        elif switch_keys:
            await self._set_switch_value(device_name, dev, switch_keys, True)
            logger.info("Włączono '%s' (DP: %s)%s", device_name, ", ".join(switch_keys), suffix)
        else:
            await self._call_with_retry(device_name, dev.turn_on)
            logger.info("Włączono '%s'%s", device_name, suffix)

    async def turn_off(self, device_name: str, reason: str | None = None) -> None:
        dev = self._devices[device_name]
        switch_keys = self._switch_keys(device_name)
        suffix = _reason_suffix(reason)
        if switch_keys:
            await self._set_switch_value(device_name, dev, switch_keys, False)
            logger.info("Wyłączono '%s' (DP: %s)%s", device_name, ", ".join(switch_keys), suffix)
        else:
            await self._call_with_retry(device_name, dev.turn_off)
            logger.info("Wyłączono '%s'%s", device_name, suffix)

    async def toggle(self, device_name: str, reason: str | None = None) -> None:
        dev = self._devices[device_name]
        switch_keys = self._switch_keys(device_name)
        dps = await self.get_status(device_name)
        key = switch_keys[0] if switch_keys else "1"
        new_value = not bool(dps.get(key))
        if switch_keys:
            await self._set_switch_value(device_name, dev, switch_keys, new_value)
        elif new_value:
            await self._call_with_retry(device_name, dev.turn_on)
        else:
            await self._call_with_retry(device_name, dev.turn_off)
        logger.info("Zmieniono stan '%s' na %s (DP: %s)%s", device_name, new_value, key, _reason_suffix(reason))

    def _resolve_dp_id(self, device_name: str, dp_code: str) -> str:
        schema = self.devices_cfg.get(device_name, {}).get("dp_schema") or {}
        for dp_id, spec in schema.items():
            if spec.get("code") == dp_code:
                return dp_id
        # Brak opisu pól (dp_schema) albo kod nierozpoznany - zakładamy, że
        # to już surowy numer DP (tak jak generyczna lista pól w panelu robi
        # to samo, gdy schemat jest niepełny).
        return dp_code

    async def set_dp(self, device_name: str, dp_code: str, value: object, reason: str | None = None) -> None:
        dev = self._devices[device_name]
        dp_id = self._resolve_dp_id(device_name, dp_code)
        coerced = coerce_dp_value(value)
        await self._call_with_retry(device_name, dev.set_value, dp_id, coerced)
        logger.info(
            "Ustawiono pole '%s' (DP=%s) urządzenia '%s' na %r%s",
            dp_code, dp_id, device_name, coerced, _reason_suffix(reason),
        )

    async def get_status(self, device_name: str) -> dict:
        dev = self._devices[device_name]
        result = await self._call_with_retry(device_name, dev.status)
        return result.get("dps", {}) if isinstance(result, dict) else {}

    async def get_all_statuses(self) -> dict[str, dict]:
        names = list(self.devices_cfg)
        results_list = await asyncio.gather(
            *(self.get_status(name) for name in names), return_exceptions=True,
        )
        results: dict[str, dict | None] = {}
        for name, result in zip(names, results_list):
            results[name] = None if isinstance(result, Exception) else result
        return results

    async def turn_on_for_duration(
        self, device_name: str, duration_seconds: int, dimmer_level_percent: int | None = None,
        reason: str | None = None,
    ) -> None:
        try:
            await self.turn_on(device_name, dimmer_level_percent, reason=reason)
        except ConnectionError:
            pass
        await asyncio.sleep(duration_seconds)
        try:
            await self.turn_off(device_name, reason=reason)
        except ConnectionError:
            logger.error("Nie udało się wyłączyć '%s' po czasie - sprawdź urządzenie ręcznie", device_name)
