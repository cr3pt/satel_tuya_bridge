"""Autoryzacja panelu webowego.

Zalogowanie wymagane jest dla WSZYSTKIEGO - łącznie z samym podglądem
strony głównej (status na żywo, historia, dziennik zdarzeń, alarmy
progowe). Publiczne zostaje TYLKO to, co musi działać przed zalogowaniem
(logowanie, kreator pierwszej konfiguracji - nie ma jeszcze żadnego
konta, więc nie da się go zgate'ować logowaniem, statyka i healthcheck) -
patrz dashboard/app.py: _require_login_for_settings_and_control
(allowlist stron/endpointów publicznych, wszystko inne domyślnie
chronione). To już DRUGIE zaostrzenie tej polityki: najpierw (na wyraźną
prośbę) zablokowano WGLĄD w ustawienia i sterowanie alarmem, nie tylko
faktyczny zapis/komendę; teraz (też na wyraźną prośbę: "przed
zalogowaniem nic nie jest widoczne") zniknęła też ostatnia publiczna
rzecz - sam podgląd stanu na stronie głównej.

WIELE kont administratorskich (patrz bridge/config_store.py: tabela
`users`) - płaski model uprawnień, każde zalogowane konto ma pełny dostęp
do zapisu (nie ma ról, bo nikt o nie nie prosił). Hasła trzymane jako hash
(werkzeug - ta sama biblioteka, której i tak używa Flask). Zarządzanie
kontami - zakładka ⚙ Ustawienia ogólne (patrz dashboard/app.py:
/api/system/users). Dwa sposoby zalogowania:

1. **Sesja przez `/login`** (używana przez panel webowy) - hasło wpisujesz
   RAZ w prawdziwym, zamaskowanym polu formularza (`<input type=password>`,
   NIE `prompt()` - `prompt()` wyświetla wpisywany tekst JAWNIE, bez
   maskowania, co był realny problem we wcześniejszej wersji tego panelu).
   Po zalogowaniu serwer wystawia w ciasteczku `HttpOnly` (niedostępnym dla
   JS na stronie - ochrona przed XSS) PODPISANY token sesji
   (`itsdangerous.URLSafeTimedSerializer`, sekret trwale zapisany w bazie -
   patrz ConfigStore.get_or_create_session_secret) - CAŁA informacja o
   sesji siedzi w samym ciasteczku, serwer nic o niej nie pamięta. Dzięki
   temu restart procesu (panel/bridge, np. po zmianie ustawień połączenia)
   NIE wylogowuje - ciasteczko przeglądarki nadal jest ważne, wystarczy ten
   sam sekret w bazie (a jest, bo baza przetrwała restart). Token niesie też
   odcisk AKTUALNEGO hasha hasła danego konta - zmiana hasła (albo usunięcie
   konta) od razu unieważnia WSZYSTKIE jego sesje, mimo że serwer nie
   pamięta ich listy. Wylogowanie z panelu (`/api/logout`) dodatkowo
   unieważnia POJEDYNCZY token natychmiast (krótka lista w pamięci procesu,
   `destroy_session`) i czyści ciasteczko w przeglądarce - w normalnym
   użyciu ("klikam wyloguj") to więc pełne, natychmiastowe wylogowanie;
   tylko skradziony token, którym nikt świadomie się nie wylogował I który
   akurat przetrwa restart panelu MIĘDZY kradzieżą a wygaśnięciem, uniknie
   tej listy - pozostaje ważny do naturalnego wygaśnięcia
   (`SESSION_TTL_SECONDS`), tak jak każdy inny token sesji w tym modelu.
2. **HTTP Basic Auth** (fallback, dla skryptów/curl/automatyzacji poza
   przeglądarką - np. wywołanie API z crona) - działa jak dotychczas.

WAŻNE ograniczenie, o którym warto wiedzieć: zarówno ciasteczko sesji, jak
i Basic Auth, przez zwykłe HTTP (nie HTTPS) nie są szyfrowane - każdy
podsłuchujący ruch w tej samej sieci LAN (niezbyt trudne w sieci WiFi ze
słabym szyfrowaniem lub gdy urządzenie jest współdzielone) może je
przechwycić. Ciasteczko sesji jest jednak STRICTLY LEPSZE niż wysyłanie
hasła Basic Auth przy KAŻDYM żądaniu: to losowy token bez związku z
hasłem, który wygasa i można go unieważnić przez /api/logout, więc jego
przechwycenie nie ujawnia hasła administratora. Dla realnego bezpieczeństwa
poza zaufaną siecią domową: postaw reverse proxy z HTTPS (Caddy/nginx)
przed panelem, albo używaj VPN (WireGuard/Tailscale) zamiast wystawiać
port bezpośrednio.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import time

import itsdangerous
from flask import jsonify, request
from werkzeug.security import check_password_hash

logger = logging.getLogger("bridge.dashboard_auth")

SESSION_COOKIE_NAME = "stb_session"
SESSION_TTL_SECONDS = 12 * 3600  # 12h - wystarczająco długo na jedną "sesję" pracy z panelem
_SESSION_SALT = "stb-session-v1"  # patrz AuthGuard._serializer niżej


def _password_fingerprint(password_hash: str) -> str:
    """Skrócony odcisk aktualnego hasha hasła - wpisywany do tokena sesji
    (patrz AuthGuard.create_session), żeby zmiana hasła (albo usunięcie
    konta) od razu unieważniała WSZYSTKIE jego sesje, mimo że serwer nie
    trzyma listy wystawionych tokenów. NIE jest to sam hash (nie ma
    potrzeby przenosić całego, dłuższego sekretu do ciasteczka) - tylko
    jego odcisk, wystarczający do wykrycia ZMIANY."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


class AuthGuard:
    def __init__(self, config_store, auth_cfg: dict | None):
        self.config_store = config_store
        self.max_attempts = (auth_cfg or {}).get("max_failed_attempts", 5)
        self.lockout_seconds = (auth_cfg or {}).get("lockout_seconds", 300)
        # "Tryb logowania": Basic Auth jest przydatny dla skryptów/crona, ale
        # wysyła hasło jawnym tekstem (zakodowanym base64, nie zaszyfrowanym)
        # przy KAŻDYM żądaniu - kto woli wyłączyć ten fallback i wymusić
        # wyłącznie logowanie sesyjne, ustawia to na False (⚙ Ustawienia
        # ogólne -> Tryb logowania).
        self.allow_basic_auth = (auth_cfg or {}).get("allow_basic_auth", True)
        # proste liczenie nieudanych prób per adres IP, w pamięci procesu -
        # wystarczające dla domowego użytku, resetuje się przy restarcie
        # panelu (co jest OK - restart panelu to rzadkie, świadome zdarzenie,
        # a nie coś, co dzieje się w kółko przy zwykłym korzystaniu z panelu).
        self._failed_attempts: dict[str, list[float]] = {}
        # Podpisuje/weryfikuje tokeny sesji (patrz obszerny docstring modułu
        # wyżej) - sekret TRWALE zapisany w bazie (ConfigStore.
        # get_or_create_session_secret), więc ten sam sekret wraca po
        # restarcie procesu i podpisane wcześniej ciasteczka NADAL działają.
        self._serializer = itsdangerous.URLSafeTimedSerializer(
            config_store.get_or_create_session_secret(), salt=_SESSION_SALT,
        )
        # Tokeny jawnie wylogowane w TYM procesie (/api/logout) - patrz
        # destroy_session/_decode_session. Proces-lokalna, najlepszy-wysiłek
        # lista (nie musi przetrwać restartu - w normalnym użyciu wylogowanie
        # i tak czyści ciasteczko w przeglądarce, patrz docstring modułu).
        self._revoked_tokens: set[str] = set()

    @property
    def configured(self) -> bool:
        return self.config_store.user_count() > 0

    def update_config(self, config_store, auth_cfg: dict | None) -> None:
        """Odświeża ustawienia (config_store + limity prób/blokady/tryb
        Basic Auth) po przeładowaniu konfiguracji (dashboard/app.py:
        create_app(), wołane po KAŻDYM zapisie z panelu) - CELOWO NIE
        tworzy nowego obiektu AuthGuard, żeby nie zgubić liczników
        nieudanych prób (`_failed_attempts`)/listy wylogowanych tokenów
        (`_revoked_tokens`) trzymanych w pamięci procesu, ani niepotrzebnie
        nie odtwarzać `_serializer` (sekret i tak jest ten sam, trwale
        zapisany w bazie - patrz __init__). Sesje przetrwałyby zresztą
        nawet nowy obiekt AuthGuard (to CAŁA ich odporność na restart
        procesu), ale nie ma powodu robić tu więcej pracy niż trzeba."""
        self.config_store = config_store
        self.max_attempts = (auth_cfg or {}).get("max_failed_attempts", 5)
        self.lockout_seconds = (auth_cfg or {}).get("lockout_seconds", 300)
        self.allow_basic_auth = (auth_cfg or {}).get("allow_basic_auth", True)

    def _is_locked_out(self, ip: str) -> bool:
        attempts = self._failed_attempts.get(ip, [])
        cutoff = time.time() - self.lockout_seconds
        attempts[:] = [t for t in attempts if t >= cutoff]
        return len(attempts) >= self.max_attempts

    def _record_failure(self, ip: str) -> None:
        self._failed_attempts.setdefault(ip, []).append(time.time())

    def _record_success(self, ip: str) -> None:
        self._failed_attempts.pop(ip, None)

    # -- logowanie sesyjne (/login, /api/login, /api/logout) ------------
    def check_password(self, username: str, password: str) -> tuple[bool, str]:
        """Sprawdza login+hasło wprost (nie z nagłówka Basic Auth) - używane
        przez POST /api/login. Ten sam rate-limiting po IP co reszta metod.

        Nazwa użytkownika NIE jest sekretem (jak hasło) - odczyt z bazy po
        nazwie wystarcza, timing attack na samą nazwę nic tu nie ujawnia, bo
        i tak trzeba jeszcze trafić hasło (sprawdzane przez
        check_password_hash, które JEST odporne na timing attack)."""
        if not self.configured:
            return False, (
                "Panel nie ma jeszcze żadnego konta administratora - "
                "akcje zmieniające stan są zablokowane, dopóki nie utworzysz "
                "pierwszego konta w kreatorze (/setup). Patrz README."
            )

        ip = request.remote_addr or "unknown"
        if self._is_locked_out(ip):
            logger.warning("Zablokowano próbę logowania z %s - zbyt wiele nieudanych prób", ip)
            return False, f"Zbyt wiele nieudanych prób - spróbuj ponownie za maks. {self.lockout_seconds}s"

        user = self.config_store.get_user_by_username(username)
        if user is None or not password or not check_password_hash(user["password_hash"], password):
            self._record_failure(ip)
            logger.warning("Nieudana próba logowania z %s", ip)
            return False, "Nieprawidłowa nazwa użytkownika lub hasło"

        self._record_success(ip)
        return True, ""

    def _prune_revoked_tokens(self) -> None:
        """Usuwa z listy wylogowanych tokeny, które i tak już wygasłyby na
        podpisie/znaczniku czasu (patrz _decode_session) - żeby lista nie
        rosła bez końca przy długo działającym procesie z wieloma
        wylogowaniami. Best-effort, nie musi być idealnie na bieżąco."""
        still_relevant = set()
        for token in self._revoked_tokens:
            try:
                self._serializer.loads(token, max_age=SESSION_TTL_SECONDS)
            except itsdangerous.BadSignature:
                continue  # i tak by już nie przeszedł - wpis zbędny
            still_relevant.add(token)
        self._revoked_tokens = still_relevant

    def create_session(self, username: str) -> str:
        """Zwraca NOWY, podpisany token sesji - wywołaj TYLKO po udanym
        check_password(). Niesie username + odcisk AKTUALNEGO hasha hasła
        tego konta (patrz _password_fingerprint wyżej) - CAŁA informacja
        potrzebna do weryfikacji siedzi w tokenie, serwer nic nie zapamiętuje
        (stąd odporność na restart procesu - patrz obszerny docstring
        modułu)."""
        user = self.config_store.get_user_by_username(username)
        pwf = _password_fingerprint(user["password_hash"]) if user else ""
        return self._serializer.dumps({"username": username, "pwf": pwf})

    def destroy_session(self, token: str | None) -> None:
        """Unieważnia POJEDYNCZY token natychmiast, w TYM procesie (patrz
        obszerny docstring modułu - to część "wylogowania z panelu", razem
        z czyszczeniem ciasteczka w dashboard/app.py: /api/logout)."""
        if not token:
            return
        self._prune_revoked_tokens()
        self._revoked_tokens.add(token)

    def _decode_session(self, token: str | None) -> dict | None:
        """Zwraca payload tokena ({"username", "pwf"}), jeśli podpis jest
        poprawny, token nie wygasł (SESSION_TTL_SECONDS), nie został jawnie
        wylogowany w tym procesie, a konto wciąż istnieje z TYM SAMYM
        hashem hasła co w chwili wystawienia tokena (patrz create_session) -
        inaczej None."""
        if not token or token in self._revoked_tokens:
            return None
        try:
            data = self._serializer.loads(token, max_age=SESSION_TTL_SECONDS)
        except itsdangerous.BadSignature:
            return None
        user = self.config_store.get_user_by_username(data.get("username", ""))
        if user is None or _password_fingerprint(user["password_hash"]) != data.get("pwf"):
            return None
        return data

    def check_session(self, token: str | None) -> bool:
        return self._decode_session(token) is not None

    def session_username(self, token: str | None) -> str | None:
        """Nazwa konta zalogowanego w TEJ sesji (do "Zalogowano jako X" w
        nawigacji) - None, jeśli sesja nie istnieje/wygasła/została
        unieważniona (patrz _decode_session)."""
        data = self._decode_session(token)
        return data["username"] if data else None

    # -- sprawdzenie dla require_auth() - sesja ALBO Basic Auth ----------
    def check(self) -> tuple[bool, str]:
        """Zwraca (czy_ok, komunikat_błędu). Najpierw sprawdza ciasteczko
        sesji (panel webowy po zalogowaniu przez /login) - dopiero gdy go
        brak, spada do HTTP Basic Auth (skrypty/curl/automatyzacja), chyba
        że wyłączona w ustawieniach (allow_basic_auth=False)."""
        if self.check_session(request.cookies.get(SESSION_COOKIE_NAME)):
            return True, ""

        if not self.configured:
            return False, (
                "Panel nie ma jeszcze żadnego konta administratora - "
                "akcje zmieniające stan są zablokowane, dopóki nie utworzysz "
                "pierwszego konta w kreatorze (/setup). Patrz README."
            )

        if not self.allow_basic_auth:
            return False, "Wymagane logowanie sesyjne (/login) - Basic Auth jest wyłączony w ustawieniach"

        ip = request.remote_addr or "unknown"
        if self._is_locked_out(ip):
            logger.warning("Zablokowano próbę logowania z %s - zbyt wiele nieudanych prób", ip)
            return False, f"Zbyt wiele nieudanych prób - spróbuj ponownie za maks. {self.lockout_seconds}s"

        auth = request.authorization
        if not auth or not auth.password:
            return False, "Wymagana autoryzacja"

        user = self.config_store.get_user_by_username(auth.username)
        if user is None or not check_password_hash(user["password_hash"], auth.password):
            self._record_failure(ip)
            logger.warning("Nieudana próba logowania z %s", ip)
            return False, "Nieprawidłowa nazwa użytkownika lub hasło"

        self._record_success(ip)
        return True, ""


def require_auth(guard_attr: str = "auth_guard", get_guard=None):
    """Dekorator dla endpointów Flask, które ZMIENIAJĄ stan (config,
    uzbrojenie/rozbrojenie). Domyślnie (`get_guard=None`) `guard_attr` to
    nazwa zmiennej globalnej w module, w którym fizycznie zdefiniowana jest
    dekorowana funkcja (dashboard/app.py), przez którą dekorator dostaje
    się do skonfigurowanego AuthGuard.

    CELOWO czyta ją z `fn.__globals__` (globalne zmienne modułu, w którym
    fizycznie zdefiniowana jest dekorowana funkcja), a NIE przez
    `import dashboard.app` - `python -m dashboard.app` uruchamia ten plik
    jako `__main__`, więc dodatkowy `import dashboard.app` w trakcie obsługi
    żądania tworzyłby DRUGĄ, osobną kopię modułu pod inną nazwą w
    sys.modules - z własnymi, nigdy nie zainicjowanymi zmiennymi globalnymi
    (auth_guard zawsze None, mimo poprawnie skonfigurowanego hasła) - stąd
    fn.__globals__ zamiast dodatkowego importu.

    `get_guard` (callable bez argumentów, zwracające AuthGuard) to WYJŚCIE
    z tego samego problemu dla wtyczek (plugins/<id>/routes.py) - tam
    dekorowana funkcja jest zdefiniowana we WŁASNYM module wtyczki, którego
    `__globals__` nie ma (i nie powinien mieć) zmiennej `auth_guard` -
    dashboard/app.py przekazuje wtedy `get_guard=lambda: auth_guard` przy
    rejestracji (patrz plugins/__init__.py)."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            guard: AuthGuard = get_guard() if get_guard is not None else fn.__globals__[guard_attr]
            ok, message = guard.check()
            if not ok:
                response = jsonify({"error": message})
                response.status_code = 401
                response.headers["WWW-Authenticate"] = 'Basic realm="Panel administracyjny"'
                return response
            return fn(*args, **kwargs)
        return wrapper
    return decorator
