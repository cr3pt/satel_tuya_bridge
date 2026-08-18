"""Konfiguracja w SQLite zamiast plików YAML.

Wcześniej config.yaml/satel_fields.yaml/tuya_devices.yaml/automations.yaml/
sensors.yaml były zwykłymi plikami tekstowymi - proste w ręcznej edycji, ale
`local_key` Tuya (klucz lokalny urządzenia) i inne sekrety (hasła MQTT,
Tuya Cloud Access Secret, kod integracji Satela) to dowolne ciągi bajtów,
które w YAML MUSZĄ być poprawnie cytowane, żeby nie zostały błędnie
zinterpretowane jako co innego (np. string wyglądający jak liczba/bool/null)
- `yaml.safe_dump` robi to poprawnie przy zapisie z panelu, ale przy RĘCZNEJ
edycji pliku łatwo to popsuć. SQLite (kolumna TEXT) przechowuje dowolny
tekst bez żadnej interpretacji składniowej - zero takiego ryzyka.

Baza trzyma jedną tabelę `documents`: każdy "dokument" (config/satel_fields/
tuya_devices/automations/sensors) to jeden wiersz z całą swoją zawartością
zserializowaną jako JSON (nie YAML - JSON ma ściśle typowane stringi, bez
niejawnego rzutowania YAML-a). To bezpośredni odpowiednik tego, czym
wcześniej były osobne pliki .yaml - ten sam podział, tylko inny nośnik.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time
from dataclasses import dataclass

import yaml


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    name TEXT PRIMARY KEY,      -- 'config' | 'satel_fields' | 'tuya_devices' | 'automations' | 'sensors'
    data TEXT NOT NULL,         -- JSON
    updated_ts REAL NOT NULL
);
"""

# Kolejność ma znaczenie tylko dla export/import (czytelność zrzutu) - sam
# odczyt/zapis pojedynczego dokumentu działa z dowolną nazwą.
KNOWN_DOCUMENTS = ["config", "sensors", "automations", "satel_fields", "tuya_devices"]


@dataclass
class ConfigStore:
    db_path: pathlib.Path

    def __post_init__(self) -> None:
        self.db_path = pathlib.Path(self.db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def exists(self) -> bool:
        return self.db_path.exists()

    def get(self, name: str, default: dict | None = None) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM documents WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return {} if default is None else default
        return json.loads(row[0])

    def set(self, name: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents (name, data, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET data = excluded.data, updated_ts = excluded.updated_ts",
                (name, json.dumps(data, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def has(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM documents WHERE name = ?", (name,)).fetchone()
        return row is not None

    def all_names(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT name FROM documents ORDER BY name").fetchall()
        return [r[0] for r in rows]

    # -- eksport/import YAML - furtka bezpieczeństwa na wypadek, gdyby ktoś
    # chciał obejrzeć/edytować konfigurację poza panelem (backup, migracja,
    # debugowanie) - baza jest źródłem prawdy, ale nikogo nie zamykamy w niej
    # bez wyjścia. Patrz /api/config/export-yaml i /api/config/import-yaml
    # w dashboard/app.py.
    def export_yaml_bundle(self) -> dict[str, str]:
        bundle = {}
        for name in KNOWN_DOCUMENTS:
            if self.has(name):
                bundle[f"{name}.yaml"] = yaml.safe_dump(self.get(name), allow_unicode=True, sort_keys=False)
        return bundle

    def import_yaml_bundle(self, files: dict[str, str]) -> None:
        """`files`: nazwa_pliku.yaml -> zawartość tekstowa. Nierozpoznane
        nazwy plików są pomijane (nie ma po co wywalać całego importu przez
        jeden literówkowy plik)."""
        for filename, content in files.items():
            name = filename[:-5] if filename.endswith(".yaml") else filename
            if name not in KNOWN_DOCUMENTS:
                continue
            data = yaml.safe_load(content) or {}
            self.set(name, data)


def resolve_db_and_legacy_paths(path: str | pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Normalizuje ścieżkę podaną przez wywołującego (`--config`, testy,
    itd.) do pary (db_path, legacy_yaml_path) w TYM SAMYM katalogu i pod
    tym samym "rdzeniem" nazwy - niezależnie, czy wywołujący podał
    `config.db` (konwencja od tej wersji) czy `config.yaml`/`.yml` (stara
    konwencja, jeszcze niezmigrowana instalacja). Każde miejsce, które
    samo tworzy/otwiera ConfigStore z surowej ścieżki (BridgeConfig.load,
    dashboard/app.py: main()/api_setup_save), MUSI przejść przez tę samą
    funkcję - inaczej `.yaml`-owa ścieżka trafiłaby jako plik bazy wprost
    (SQLite pod nazwą .yaml, myląco i niespójnie z resztą kodu)."""
    path = pathlib.Path(path)
    if path.suffix in (".yaml", ".yml"):
        return path.with_suffix(".db"), path
    return path, path.with_suffix(".yaml")


def migrate_legacy_yaml_dir(store: ConfigStore, config_yaml_path: pathlib.Path) -> bool:
    """Jednorazowa migracja starej instalacji (config.yaml + siostrzane pliki
    w tym samym katalogu) do `store`. Zwraca True, jeśli migracja faktycznie
    coś zaimportowała (czyli config.yaml istniał). Bezpieczna do wywołania
    wielokrotnie - jeśli dokument już jest w bazie, NIE nadpisuje go (żeby
    powtórne uruchomienie z resztkami starych plików na dysku nigdy nie
    cofnęło zmian zrobionych już przez panel po migracji)."""
    if not config_yaml_path.exists():
        return False

    def _load(path: pathlib.Path) -> dict:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    config_data = _load(config_yaml_path)
    config_dir = config_yaml_path.parent

    def _resolve(filename_key: str, section: dict, default_name: str) -> pathlib.Path:
        rel = section.get(filename_key, default_name) if isinstance(section, dict) else default_name
        p = pathlib.Path(rel)
        return p if p.is_absolute() else config_dir / p

    automations_path = _resolve("automations_file", config_data, "automations.yaml")
    satel_fields_path = _resolve("fields_file", config_data.get("satel", {}), "satel_fields.yaml")
    tuya_devices_path = _resolve("devices_file", config_data.get("tuya", {}), "tuya_devices.yaml")
    sensors_path = _resolve("sensors_file", config_data.get("ebus", {}), "sensors.yaml")

    if not store.has("config"):
        store.set("config", config_data)
    if not store.has("automations"):
        store.set("automations", _load(automations_path))
    if not store.has("satel_fields"):
        store.set("satel_fields", _load(satel_fields_path))
    if not store.has("tuya_devices"):
        store.set("tuya_devices", _load(tuya_devices_path))
    if not store.has("sensors"):
        store.set("sensors", _load(sensors_path))

    # Oznacz stare pliki jako nieaktualne (nie kasujemy - to nadal czyjeś
    # dane na dysku, mogłyby się przydać do ręcznej weryfikacji migracji),
    # żeby nikt przypadkiem nie edytował pliku, który już nic nie robi.
    for path in [config_yaml_path, automations_path, satel_fields_path, tuya_devices_path, sensors_path]:
        if path.exists() and not path.name.endswith(".migrated"):
            try:
                path.rename(path.with_name(path.name + ".migrated"))
            except OSError:
                pass  # kosmetyczne - brak uprawnień do rename nie może zablokować migracji

    return True
