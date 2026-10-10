"""Obsługa liczników wM-Bus (przez `wmbusmeters`) - w tym liczników
3-fazowych (osobne pola L1/L2/L3).

Architektura (celowo bez własnego dekodera radiowego/AES):

    licznik --radio--> dongle USB (iM871A/AMB8465) --> wmbusmeters
        (odszyfrowuje AES-128 kluczem z pliku licznika, dekoduje sterownikiem)
        --> `shell=mosquitto_pub ...` --> broker MQTT --> ten moduł

Ten moduł robi trzy rzeczy:

1. Walidacja i generowanie plików konfiguracyjnych `wmbusmeters`
   (`wmbusmeters.conf` + po jednym pliku na licznik w `wmbusmeters.d/`),
   z listy liczników zapisanej w konfiguracji (sekcja `wmbus`, wtyczka /plugins/wmbus).
2. Parsowanie telegramów JSON publikowanych przez wmbusmeters na MQTT.
3. Rozpoznawanie faz (L1/L2/L3) i jednostek po nazwach pól
   (np. `voltage_at_phase_2_v` -> faza 2, napięcie, V).

BEZPIECZEŃSTWO KLUCZY AES: klucz licznika to sekret (kto go ma i podsłucha
radio, odczyta zużycie = widzi, kiedy nikogo nie ma w domu). Dlatego:
 - klucze leżą w bazie konfiguracji (config.db, prawa 0600, jak klucze Tuya),
   a wygenerowane pliki wmbusmeters też mają 0600,
 - panel NIGDY nie odsyła klucza do przeglądarki (tylko zamaskowaną końcówkę),
 - puste pole klucza przy zapisie = zachowaj dotychczasowy klucz.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import pathlib
import re
import tempfile
import threading
from typing import Callable

import paho.mqtt.client as mqtt


from .resilience import HealthReporter

logger = logging.getLogger("bridge.wmbus")

KEY_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
ID_RE = re.compile(r"^[0-9A-Fa-f]{1,16}$")
DRIVER_RE = re.compile(r"^[a-z0-9_]{1,40}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_:/.\-]{1,80}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._\-]{1,253}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-/]{1,80}$")

# Pola, które nie są odczytami (nie trafiają do wykresów jako liczby)
_NON_READING_FIELDS = {"timestamp"}

# sufiks nazwy pola (konwencja wmbusmeters: jednostka na końcu) ->
# (jednostka, device_class HA, state_class HA)
_UNITS: dict[str, tuple[str, str | None, str | None]] = {
    "kwh": ("kWh", "energy", "total_increasing"),
    "kw": ("kW", "power", "measurement"),
    "w": ("W", "power", "measurement"),
    "v": ("V", "voltage", "measurement"),
    "a": ("A", "current", "measurement"),
    "hz": ("Hz", "frequency", "measurement"),
    "c": ("°C", "temperature", "measurement"),
    "m3": ("m³", None, "total_increasing"),
    "m3h": ("m³/h", None, "measurement"),
    "dbm": ("dBm", "signal_strength", "measurement"),
}

# "phase_1", "phase1", "ph2", "l3" jako samodzielny człon nazwy pola
_PHASE_RE = re.compile(r"(?:^|_)(?:phase|ph|l)_?([123])(?:_|$)")


def detect_phase(field: str) -> int | None:
    """Numer fazy (1-3) wynikający z nazwy pola albo None. Dopasowuje tylko
    samodzielne człony (`phase_2`, `l3`), więc np. `tariff_1` czy `total_m3`
    nie są mylone z fazą."""
    m = _PHASE_RE.search(field.lower())
    return int(m.group(1)) if m else None


def field_unit(field: str) -> tuple[str, str | None, str | None] | None:
    suffix = field.lower().rsplit("_", 1)[-1]
    return _UNITS.get(suffix)


def is_voltage_field(field: str) -> bool:
    unit = field_unit(field)
    return bool(unit and unit[1] == "voltage")


def mask_key(key: str) -> str:
    """Do wyświetlania w panelu: tylko 4 ostatnie znaki."""
    if not key:
        return ""
    return "•" * 28 + key[-4:]


# ----------------------------------------------------------------------
# Lista liczników (wmbus_meters.yaml)
# ----------------------------------------------------------------------
def validate_meter(meter: dict) -> list[str]:
    errors = []
    name = meter.get("name") or ""
    prefix = f"Licznik '{name}': " if name else "Licznik: "
    if not NAME_RE.match(name):
        errors.append("nazwa: tylko litery, cyfry i _ (max 40 znaków)")
    if not ID_RE.match(str(meter.get("id") or "")):
        errors.append(prefix + "ID: 1-16 znaków szesnastkowych (numer seryjny z tabliczki/telegramu)")
    driver = meter.get("driver") or "auto"
    if not DRIVER_RE.match(driver) and driver != "auto":
        errors.append(prefix + "sterownik: małe litery/cyfry/_ albo 'auto'")
    key = meter.get("key") or ""
    if key and not KEY_RE.match(key):
        errors.append(prefix + "klucz AES musi mieć dokładnie 32 znaki szesnastkowe (0-9, A-F)")
    if int(meter.get("phases", 1) or 1) not in (1, 3):
        errors.append(prefix + "liczba faz: 1 lub 3")
    for opt in ("max_silence_minutes", "min_history_interval_seconds", "voltage_min", "voltage_max"):
        val = meter.get(opt)
        if val not in (None, "") and not _is_number(val):
            errors.append(prefix + f"{opt}: wymagana liczba")
    vmin, vmax = meter.get("voltage_min"), meter.get("voltage_max")
    if _is_number(vmin) and _is_number(vmax) and float(vmin) >= float(vmax):
        errors.append(prefix + "voltage_min musi być mniejsze od voltage_max")
    return errors


def _is_number(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def validate_meters(meters: list[dict]) -> list[str]:
    errors = []
    seen: set[str] = set()
    for m in meters:
        errors += validate_meter(m)
        name = m.get("name")
        if name in seen:
            errors.append(f"Zduplikowana nazwa licznika: '{name}'")
        seen.add(name)
    return errors


def normalize_meter(m: dict) -> dict:
    """Uzupełnia wartości domyślne; zwraca nowy słownik."""
    out = {
        "name": m["name"].strip(),
        "label": (m.get("label") or m["name"]).strip(),
        "id": str(m["id"]).strip(),
        "driver": (m.get("driver") or "auto").strip(),
        "key": (m.get("key") or "").strip(),
        "phases": int(m.get("phases", 1) or 1),
        "max_silence_minutes": float(m["max_silence_minutes"]) if _is_number(m.get("max_silence_minutes")) else 60.0,
        # Licznik potrafi nadawać co kilka sekund - zapis każdego telegramu
        # do SQLite szybko zużywa kartę SD w RPi, więc historia jest
        # ograniczona czasowo (statusy "na żywo" i alarmy mimo to każdy telegram).
        "min_history_interval_seconds": (
            float(m["min_history_interval_seconds"]) if _is_number(m.get("min_history_interval_seconds")) else 60.0
        ),
        "home_assistant": bool(m.get("home_assistant", True)),
    }
    if _is_number(m.get("voltage_min")):
        out["voltage_min"] = float(m["voltage_min"])
    if _is_number(m.get("voltage_max")):
        out["voltage_max"] = float(m["voltage_max"])
    return out


def _write_private(path: pathlib.Path, text: str) -> None:
    """Zapis atomowy z prawami 0600 od samego początku (bez okna, w którym
    plik z kluczem byłby czytelny dla innych)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_keys(new_meters: list[dict], existing: list[dict]) -> list[dict]:
    """Puste pole `key` w zapisie z panelu = zostaw dotychczasowy klucz
    licznika o tej samej nazwie. `clear_key: true` = świadome usunięcie klucza
    (licznik bez szyfrowania)."""
    old_keys = {m.get("name"): m.get("key", "") for m in existing}
    merged = []
    for m in new_meters:
        m = dict(m)
        if m.pop("clear_key", False):
            m["key"] = ""
        elif not (m.get("key") or "").strip():
            m["key"] = old_keys.get(m.get("name"), "")
        merged.append(m)
    return merged


def public_view(meters: list[dict]) -> list[dict]:
    """Wersja do wysłania do przeglądarki - BEZ klucza."""
    out = []
    for m in meters:
        v = {k: val for k, val in m.items() if k != "key"}
        v["has_key"] = bool(m.get("key"))
        v["key_masked"] = mask_key(m.get("key", ""))
        out.append(v)
    return out


# ----------------------------------------------------------------------
# Generator konfiguracji wmbusmeters
# ----------------------------------------------------------------------
def render_meter_file(meter: dict) -> str:
    lines = [
        f"name={meter['name']}",
        f"id={meter['id']}",
        f"driver={meter.get('driver') or 'auto'}",
        f"key={meter.get('key') or 'NOKEY'}",
    ]
    return "\n".join(lines) + "\n"


def render_main_conf(wmbus_cfg: dict, mqtt_host: str, mqtt_port: int) -> str:
    device = wmbus_cfg.get("device", "auto")
    prefix = wmbus_cfg.get("topic_prefix", "wmbusmeters")
    if not DEVICE_RE.match(device):
        raise ValueError(f"Nieprawidłowe urządzenie wmbusmeters: {device!r}")
    if not HOST_RE.match(mqtt_host):
        raise ValueError(f"Nieprawidłowy host MQTT: {mqtt_host!r}")
    if not TOPIC_RE.match(prefix):
        raise ValueError(f"Nieprawidłowy prefiks tematu MQTT: {prefix!r}")
    # $METER_NAME / $METER_JSON podstawia wmbusmeters przez zmienne
    # środowiskowe powłoki - nic z danych telegramu nie jest wklejane
    # do polecenia jako tekst.
    shell = f'mosquitto_pub -h {mqtt_host} -p {int(mqtt_port)} -t {prefix}/$METER_NAME -m "$METER_JSON"'
    return (
        "# Wygenerowane przez bridge (panel /wmbus) - nie edytuj ręcznie, zostanie nadpisane.\n"
        "loglevel=normal\n"
        f"device={device}\n"
        "logtelegrams=false\n"
        "format=json\n"
        f"shell={shell}\n"
    )


def write_wmbusmeters_files(wmbus_cfg: dict, mqtt_host: str, mqtt_port: int,
                            meters: list[dict], out_dir: pathlib.Path | str) -> list[pathlib.Path]:
    """Zapisuje `<out>/etc/wmbusmeters.conf` i `<out>/etc/wmbusmeters.d/<nazwa>`
    (wszystkie 0600). Usuwa pliki liczników, których już nie ma na liście,
    żeby skasowany w panelu licznik (i jego klucz) nie został na dysku."""
    out = pathlib.Path(out_dir)
    errors = validate_meters(meters)
    if errors:
        raise ValueError("; ".join(errors))
    written = []
    conf = out / "etc" / "wmbusmeters.conf"
    _write_private(conf, render_main_conf(wmbus_cfg, mqtt_host, mqtt_port))
    written.append(conf)
    meters_dir = out / "etc" / "wmbusmeters.d"
    meters_dir.mkdir(parents=True, exist_ok=True)
    keep = set()
    for m in meters:
        p = meters_dir / m["name"]
        _write_private(p, render_meter_file(m))
        written.append(p)
        keep.add(p.name)
    for stale in meters_dir.iterdir():
        if stale.is_file() and stale.name not in keep:
            stale.unlink()
    return written


# ----------------------------------------------------------------------
# Telegramy
# ----------------------------------------------------------------------
def parse_telegram(payload: bytes | str) -> dict | None:
    """JSON z wmbusmeters -> słownik albo None, gdy to nie jest poprawny telegram."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def numeric_fields(telegram: dict) -> dict[str, float]:
    out = {}
    for k, v in telegram.items():
        if k in _NON_READING_FIELDS or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and math.isfinite(v):
            out[k] = v
    return out


def phases_in(fields) -> set[int]:
    return {p for p in (detect_phase(f) for f in fields) if p}


class WmbusMqttListener:
    """Subskrybuje `<prefix>/<nazwa_licznika>` na brokerze MQTT. Callbacki
    paho działają w OSOBNYM wątku, dlatego pętla asyncio jest przekazywana
    jawnie (do powiadomień HealthReportera) - patrz resilience.py."""

    def __init__(self, cfg: dict, meters: list[dict], on_telegram: Callable[[dict, dict], None],
                 history=None, notify_cfg: dict | None = None, grace_seconds: float = 120,
                 loop: asyncio.AbstractEventLoop | None = None):
        self.cfg = cfg
        self.on_telegram = on_telegram
        self._meters = {m["name"]: m for m in meters}
        self._lock = threading.Lock()
        self._prefix = cfg.get("topic_prefix", "wmbusmeters")
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        self._health = HealthReporter(
            "wmbus_mqtt", history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds, loop=loop,
        )
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)

    def start(self) -> None:
        host = self.cfg["mqtt_host"]
        port = self.cfg.get("mqtt_port", 1883)
        self._client.connect_async(host, port)
        self._client.loop_start()
        logger.info("Nasłuch wM-Bus (wmbusmeters przez MQTT %s:%s) uruchomiony, liczniki: %s",
                    host, port, ", ".join(self._meters) or "brak")

    def update_meters(self, meters: list[dict]) -> None:
        """Przeładowanie konfiguracji bez restartu (lista liczników)."""
        with self._lock:
            self._meters = {m["name"]: m for m in meters}

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            self._health.report(False, f"kod błędu {reason_code}")
            return
        self._health.report(True)
        client.subscribe(f"{self._prefix}/+")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self._health.report(False, f"kod {reason_code}" if reason_code != 0 else "")

    def _on_message(self, client, userdata, msg) -> None:
        name = msg.topic.rsplit("/", 1)[-1]
        with self._lock:
            meter = self._meters.get(name)
        if meter is None:
            logger.debug("wM-Bus: telegram z nieskonfigurowanego licznika '%s' - pomijam", name)
            return
        telegram = parse_telegram(msg.payload)
        if telegram is None:
            logger.warning("wM-Bus: nieprawidłowy JSON na temacie %s", msg.topic)
            return
        try:
            self.on_telegram(meter, telegram)
        except Exception:
            # wyjątek w callbacku nie może ubić wątku sieciowego paho
            logger.exception("wM-Bus: błąd obsługi telegramu licznika '%s'", name)
