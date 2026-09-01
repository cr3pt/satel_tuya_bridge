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
import secrets
import sqlite3
import tempfile
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

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_ts REAL NOT NULL
);

-- Sekret do podpisywania ciasteczek sesji panelu (dashboard/auth.py) -
-- WYGENEROWANY RAZ i trzymany trwale w tej samej bazie co reszta configu
-- (przetrwa restart procesu, w odróżnieniu od dawnego trzymania sesji
-- wyłącznie w pamięci procesu). Jeden wiersz (id=1), CELOWO OSOBNA tabela
-- od `documents` - nie ma trafiać do eksportu/importu ZIP (patrz
-- KNOWN_DOCUMENTS niżej), żeby kopia zapasowa configu nie roznosiła
-- klucza podpisującego sesje.
CREATE TABLE IF NOT EXISTS auth_secret (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    secret TEXT NOT NULL
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

    # -- konta użytkowników (patrz dashboard/auth.py: AuthGuard) - wiele
    # kont administratorskich zamiast jednego wpisanego wprost w dokument
    # "config" (dashboard.auth.username/password_hash) - ten pojedynczy
    # wpis jest migrowany do pierwszego wiersza tej tabeli przy pierwszym
    # użyciu, patrz migrate_single_admin_to_users() niżej. Płaski model
    # uprawnień (jak dawniej): każde zalogowane konto ma pełny dostęp do
    # zapisu - nie ma ról/uprawnień, bo nikt o nie nie prosił.
    def add_user(self, username: str, password_hash: str) -> int:
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (username, password_hash, created_ts) VALUES (?, ?, ?)",
                    (username, password_hash, time.time()),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"Użytkownik '{username}' już istnieje") from None
            self._conn.commit()
            return cur.lastrowid

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, created_ts FROM users ORDER BY id"
            ).fetchall()
        return [{"id": r[0], "username": r[1], "created_ts": r[2]} for r in rows]

    def get_user_by_username(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password_hash, created_ts FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2], "created_ts": row[3]}

    def user_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0]

    def update_user(self, user_id: int, *, username: str | None = None, password_hash: str | None = None) -> None:
        """Aktualizuje wybrane pola (None = zostaw bez zmian). Rzuca
        ValueError, jeśli nowa nazwa koliduje z innym istniejącym kontem."""
        fields, params = [], []
        if username is not None:
            fields.append("username = ?")
            params.append(username)
        if password_hash is not None:
            fields.append("password_hash = ?")
            params.append(password_hash)
        if not fields:
            return
        params.append(user_id)
        with self._lock:
            try:
                self._conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
            except sqlite3.IntegrityError:
                raise ValueError(f"Użytkownik '{username}' już istnieje") from None
            self._conn.commit()

    def delete_user(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._conn.commit()

    def migrate_single_admin_to_users(self) -> None:
        """Jednorazowo przenosi dashboard.auth.username/password_hash z
        dokumentu "config" (stary model - jedno konto wpisane wprost w
        config) do tabeli users, jeśli tabela jest jeszcze pusta, a stary
        wpis istnieje. Bezpieczne przy wielokrotnym wywołaniu - nie robi
        nic, gdy jakiekolwiek konto już istnieje."""
        if self.user_count() > 0:
            return
        config = self.get("config")
        auth_cfg = config.get("dashboard", {}).get("auth", {})
        password_hash = auth_cfg.get("password_hash")
        if not password_hash:
            return
        username = auth_cfg.get("username") or "admin"
        self.add_user(username, password_hash)

    def get_or_create_session_secret(self) -> str:
        """Zwraca trwały sekret do podpisywania ciasteczek sesji (patrz
        dashboard/auth.py: AuthGuard) - generowany RAZ, przy pierwszym
        użyciu, i trzymany w tej samej bazie SQLite co reszta configu. Dzięki
        temu podpisane ciasteczka pozostają ważne PO restarcie procesu
        (panel/bridge) - w odróżnieniu od poprzedniego modelu, gdzie sesje
        żyły wyłącznie w pamięci procesu i restart wylogowywał każdego."""
        with self._lock:
            row = self._conn.execute("SELECT secret FROM auth_secret WHERE id = 1").fetchone()
            if row is not None:
                return row[0]
            secret = secrets.token_hex(32)
            self._conn.execute("INSERT INTO auth_secret (id, secret) VALUES (1, ?)", (secret,))
            self._conn.commit()
            return secret

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

    def import_yaml_bundle_validated(self, files: dict[str, str]) -> list[str]:
        """Jak `import_yaml_bundle`, ale z PODWÓJNYM sprawdzeniem PRZED
        zapisem - używane przez import z archiwum ZIP (patrz
        dashboard/app.py: /api/config/import-zip), gdzie błędny/uszkodzony
        plik z zewnątrz (przeniesiony z innej instalacji, ręcznie
        edytowany) nie powinien dać się zapisać "po cichu", a dopiero przy
        najbliższym restarcie bridge'a ujawnić, że proces w ogóle nie
        wstaje:

        1) Składnia - każdy rozpoznany plik musi być poprawnym YAML-em
           reprezentującym słownik.
        2) Semantyka "czy zadziała" - wynikowa konfiguracja (aktualna
           zawartość bazy + nadpisania z importu) musi przejść przez
           PRAWDZIWY `BridgeConfig.load()` na tymczasowej kopii bazy - tę
           samą ścieżkę kodu, którą bridge/panel uruchamiają przy starcie
           procesu (zamiast osobno, ręcznie duplikować tu tę logikę, co
           mogłoby z czasem rozjechać się z rzeczywistym zachowaniem) -
           plus jawne sprawdzenie kilku pól Satela, które
           `BridgeConfig._validate()` NIE sprawdza (bo interesuje ją tylko
           obecność sekcji najwyższego poziomu), a bez których
           `RulesEngine` i tak wywali się `KeyError`-em zaraz po starcie
           (`bridge/rules_engine.py`: `config.satel["host"/"port"]`,
           odczytywane bezpośrednio, bez `.get()`). `integration_key` jest
           CELOWO pominięty tutaj - jest opcjonalny, panel bez niego działa
           tylko do odczytu (patrz bridge/satel_client.py).

        Nic NIE trafia do prawdziwej bazy, dopóki oba sprawdzenia nie
        przejdą - błąd podnosi ValueError z czytelnym opisem, zamiast
        zapisać część dokumentów a resztę pominąć."""
        from .config import BridgeConfig  # lokalny import - unika cyklu (config.py importuje ten moduł)

        parsed: dict[str, dict] = {}
        for filename, content in files.items():
            name = filename[:-5] if filename.endswith(".yaml") else filename
            if name not in KNOWN_DOCUMENTS:
                continue
            try:
                data = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                raise ValueError(f"Błąd parsowania YAML w pliku '{filename}': {exc}") from exc
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError(f"Plik '{filename}' nie zawiera słownika YAML (nieprawidłowy format)")
            parsed[name] = data

        if not parsed:
            raise ValueError(
                "Archiwum nie zawiera żadnego rozpoznawanego pliku konfiguracji "
                f"({', '.join(sorted(n + '.yaml' for n in KNOWN_DOCUMENTS))})"
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_store = ConfigStore(pathlib.Path(tmp_dir) / "config.db")
            for name in KNOWN_DOCUMENTS:
                content = parsed[name] if name in parsed else (self.get(name) if self.has(name) else None)
                if content is not None:
                    tmp_store.set(name, content)

            try:
                simulated = BridgeConfig.load(tmp_store.db_path)
            except Exception as exc:
                raise ValueError(f"Zaimportowana konfiguracja nie przejdzie startu procesu: {exc}") from exc

            missing_satel = [
                key for key in ("host", "port")
                if not simulated.raw.get("satel", {}).get(key)
            ]
            if missing_satel:
                raise ValueError(
                    "Zaimportowana konfiguracja nie przejdzie startu procesu: brak wymaganych pól "
                    f"satel.{'/'.join(missing_satel)}"
                )

        for name, data in parsed.items():
            self.set(name, data)
        return sorted(parsed.keys())


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
