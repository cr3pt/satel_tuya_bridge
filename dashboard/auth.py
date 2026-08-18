"""Autoryzacja panelu webowego - CELOWO wąska: chroni tylko akcje, które
COŚ ZMIENIAJĄ (zapis konfiguracji czujników, uzbrajanie/rozbrajanie stref).
Zwykłe przeglądanie panelu (historia, statusy, dziennik zdarzeń) zostaje
otwarte - to był świadomy wybór, nie przez zapomnienie.

Pojedyncze konto administratora (nazwa użytkownika + hasło, nie ma potrzeby
wielu kont w domowym systemie) - hasło trzymane jako hash (werkzeug - ta
sama biblioteka, której i tak używa Flask, więc zero nowej zależności),
nazwa użytkownika jawnym tekstem w config.yaml (nie jest sekretem, tylko
identyfikatorem - jak login, nie hasło). Oba ustawiane raz w kreatorze
(/setup) przy pierwszej konfiguracji. Dwa sposoby zalogowania:

1. **Sesja przez `/login`** (używana przez panel webowy) - hasło wpisujesz
   RAZ w prawdziwym, zamaskowanym polu formularza (`<input type=password>`,
   NIE `prompt()` - `prompt()` wyświetla wpisywany tekst JAWNIE, bez
   maskowania, co był realny problem we wcześniejszej wersji tego panelu).
   Po zalogowaniu serwer wystawia losowy token sesji w ciasteczku
   `HttpOnly` (niedostępnym dla JS na stronie - ochrona przed XSS) - dalsze
   żądania NIE wysyłają już hasła, tylko ten token, aż do wylogowania
   (`/api/logout`) albo wygaśnięcia sesji.
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
import logging
import secrets
import time

from flask import jsonify, request
from werkzeug.security import check_password_hash

logger = logging.getLogger("bridge.dashboard_auth")

SESSION_COOKIE_NAME = "stb_session"
SESSION_TTL_SECONDS = 12 * 3600  # 12h - wystarczająco długo na jedną "sesję" pracy z panelem


class AuthGuard:
    def __init__(self, auth_cfg: dict | None):
        self.username = (auth_cfg or {}).get("username") or "admin"
        self.password_hash = (auth_cfg or {}).get("password_hash")
        self.max_attempts = (auth_cfg or {}).get("max_failed_attempts", 5)
        self.lockout_seconds = (auth_cfg or {}).get("lockout_seconds", 300)
        # proste liczenie nieudanych prób per adres IP, w pamięci procesu -
        # wystarczające dla domowego użytku, resetuje się przy restarcie
        # panelu (co jest OK - restart panelu to rzadkie, świadome zdarzenie)
        self._failed_attempts: dict[str, list[float]] = {}
        # token sesji -> timestamp wygaśnięcia. W pamięci procesu (jak wyżej)
        # - restart panelu wylogowuje wszystkich, co jest akceptowalne
        # (rzadkie, świadome zdarzenie, użytkownik po prostu loguje się
        # ponownie raz).
        self._sessions: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.password_hash)

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

        Nazwa użytkownika NIE jest sekretem (jak hasło) - porównanie wprost
        (`!=`) wystarcza, timing attack na samą nazwę nic tu nie ujawnia,
        bo i tak trzeba jeszcze trafić hasło (sprawdzane przez
        check_password_hash, które JEST odporne na timing attack)."""
        if not self.configured:
            return False, (
                "Panel nie ma skonfigurowanego hasła administratora "
                "(dashboard.auth.password_hash w config.yaml) - akcje zmieniające "
                "stan są zablokowane, dopóki nie ustawisz hasła. Patrz README."
            )

        ip = request.remote_addr or "unknown"
        if self._is_locked_out(ip):
            logger.warning("Zablokowano próbę logowania z %s - zbyt wiele nieudanych prób", ip)
            return False, f"Zbyt wiele nieudanych prób - spróbuj ponownie za maks. {self.lockout_seconds}s"

        if username != self.username or not password or not check_password_hash(self.password_hash, password):
            self._record_failure(ip)
            logger.warning("Nieudana próba logowania z %s", ip)
            return False, "Nieprawidłowa nazwa użytkownika lub hasło"

        self._record_success(ip)
        return True, ""

    def _prune_expired_sessions(self) -> None:
        now = time.time()
        expired = [token for token, expires in self._sessions.items() if expires < now]
        for token in expired:
            del self._sessions[token]

    def create_session(self) -> str:
        """Zwraca NOWY token sesji - wywołaj TYLKO po udanym check_password().
        `secrets.token_urlsafe` - kryptograficznie bezpieczny generator,
        token nie ma żadnego związku matematycznego z hasłem (w
        odróżnieniu od Basic Auth, gdzie nagłówek to zakodowane hasło)."""
        self._prune_expired_sessions()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def destroy_session(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def check_session(self, token: str | None) -> bool:
        if not token:
            return False
        self._prune_expired_sessions()
        return token in self._sessions

    # -- sprawdzenie dla require_auth() - sesja ALBO Basic Auth ----------
    def check(self) -> tuple[bool, str]:
        """Zwraca (czy_ok, komunikat_błędu). Najpierw sprawdza ciasteczko
        sesji (panel webowy po zalogowaniu przez /login) - dopiero gdy go
        brak, spada do HTTP Basic Auth (skrypty/curl/automatyzacja)."""
        if self.check_session(request.cookies.get(SESSION_COOKIE_NAME)):
            return True, ""

        if not self.configured:
            return False, (
                "Panel nie ma skonfigurowanego hasła administratora "
                "(dashboard.auth.password_hash w config.yaml) - akcje zmieniające "
                "stan są zablokowane, dopóki nie ustawisz hasła. Patrz README."
            )

        ip = request.remote_addr or "unknown"
        if self._is_locked_out(ip):
            logger.warning("Zablokowano próbę logowania z %s - zbyt wiele nieudanych prób", ip)
            return False, f"Zbyt wiele nieudanych prób - spróbuj ponownie za maks. {self.lockout_seconds}s"

        auth = request.authorization
        if not auth or not auth.password:
            return False, "Wymagana autoryzacja"

        if auth.username != self.username or not check_password_hash(self.password_hash, auth.password):
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
