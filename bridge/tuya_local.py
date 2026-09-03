from __future__ import annotations

import asyncio
import base64
import json
import logging

import tinytuya

logger = logging.getLogger("bridge.tuya_local")

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
}

_PHASE_RAW_CODES = {"phase_a", "phase_b", "phase_c"}
_KNOWN_VALUE_SCALES = {
    "forward_energy_total": (2, " kWh"),
    "reverse_energy_total": (2, " kWh"),
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


def scan_network(maxretry: int = 1) -> list[dict]:
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
    try:
        cloud = tinytuya.Cloud(apiRegion=api_region, apiKey=access_id, apiSecret=access_key)
    except Exception as exc:
        raise ConnectionError(f"Błąd logowania do Tuya Cloud: {exc}") from exc
    if getattr(cloud, "error", None):
        message = cloud.error.get("Error") if isinstance(cloud.error, dict) else str(cloud.error)
        raise ConnectionError(f"Błąd logowania do Tuya Cloud: {message}")
    return cloud


def fetch_cloud_devices(api_region: str, access_id: str, access_key: str) -> list[dict]:
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
        lan_devices = {d["id"]: d for d in scan_network(maxretry=1) if d.get("id")}
    except Exception:
        logger.warning("Skan LAN podczas importu z Tuya Cloud nie powiódł się - kontynuuję bez adresów IP", exc_info=True)
        lan_devices = {}

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


class TuyaLocalController:
    def __init__(self, devices_cfg: dict, zigbee_gateways_cfg: dict | None = None,
                 retries: int = 2, retry_delay: float = 1.5):
        self.devices_cfg = devices_cfg
        self.zigbee_gateways_cfg = zigbee_gateways_cfg or {}
        self.retries = retries
        self.retry_delay = retry_delay

        self._gateways: dict[str, tinytuya.Device] = {}
        for name, cfg in self.zigbee_gateways_cfg.items():
            gw = tinytuya.Device(cfg["id"], cfg["ip"], cfg["local_key"])
            gw.set_version(cfg.get("version", 3.4))
            self._gateways[name] = gw

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
                dev = tinytuya.Device(
                    cfg["id"], cid=cfg.get("cid") or cfg["id"], dev_type="zigbee", parent=gateway,
                )
            else:
                dev = tinytuya.OutletDevice(cfg["id"], cfg["ip"], cfg["local_key"])
                dev.set_version(cfg.get("version", 3.4))
            self._devices[name] = dev

    async def _call_with_retry(self, device_name: str, func, *args):
        loop = asyncio.get_event_loop()
        last_error = None
        for attempt in range(self.retries + 1):
            try:
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

    async def turn_on(self, device_name: str, dimmer_level_percent: int | None = None) -> None:
        cfg = self.devices_cfg[device_name]
        dev = self._devices[device_name]

        if cfg.get("type") == "dimmer" and dimmer_level_percent is not None:
            dp = cfg["dimmer_dp"]
            lo, hi = cfg.get("dimmer_range", [10, 1000])
            value = int(lo + (hi - lo) * (dimmer_level_percent / 100))
            await self._call_with_retry(device_name, dev.set_status, True, "1")
            await self._call_with_retry(device_name, dev.set_value, dp, value)
            logger.info("Ustawiono '%s' na %s%% jasności (DP=%s)", device_name, dimmer_level_percent, value)
        else:
            await self._call_with_retry(device_name, dev.turn_on)
            logger.info("Włączono '%s'", device_name)

    async def turn_off(self, device_name: str) -> None:
        dev = self._devices[device_name]
        await self._call_with_retry(device_name, dev.turn_off)
        logger.info("Wyłączono '%s'", device_name)

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
        self, device_name: str, duration_seconds: int, dimmer_level_percent: int | None = None
    ) -> None:
        try:
            await self.turn_on(device_name, dimmer_level_percent)
        except ConnectionError:
            pass
        await asyncio.sleep(duration_seconds)
        try:
            await self.turn_off(device_name)
        except ConnectionError:
            logger.error("Nie udało się wyłączyć '%s' po czasie - sprawdź urządzenie ręcznie", device_name)
