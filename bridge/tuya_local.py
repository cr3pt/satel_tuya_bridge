from __future__ import annotations

import asyncio
import logging

import tinytuya

logger = logging.getLogger("bridge.tuya_local")


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


def fetch_cloud_devices(api_region: str, access_id: str, access_key: str) -> list[dict]:
    try:
        cloud = tinytuya.Cloud(apiRegion=api_region, apiKey=access_id, apiSecret=access_key)
    except Exception as exc:
        raise ConnectionError(f"Błąd logowania do Tuya Cloud: {exc}") from exc

    if getattr(cloud, "error", None):
        message = cloud.error.get("Error") if isinstance(cloud.error, dict) else str(cloud.error)
        raise ConnectionError(f"Błąd logowania do Tuya Cloud: {message}")

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
        devices.append({
            "id": dev_id,
            "name": d.get("name", ""),
            "local_key": d.get("key", ""),
            "ip": lan_match.get("ip", ""),
            "version": lan_match.get("version", ""),
            "category": d.get("category", ""),
            "gateway_id": d.get("gateway_id", ""),
            "node_id": d.get("node_id", ""),
        })
    return devices


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
