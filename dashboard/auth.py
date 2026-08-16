"""Autoryzacja panelu webowego - CELOWO wąska: chroni tylko akcje, które
COŚ ZMIENIAJĄ (zapis konfiguracji czujników, uzbrajanie/rozbrajanie stref).
Zwykłe przeglądanie panelu (historia, statusy, dziennik zdarzeń) zostaje
otwarte - to był świadomy wybór, nie przez zapomnienie.

HTTP Basic Auth z pojedynczym hasłem administratora (nie ma potrzeby
wielu kont w domowym systemie). Hasło trzymane jako hash (werkzeug -
ta sama biblioteka, której i tak używa Flask, więc zero nowej zależności).

WAŻNE ograniczenie, o którym warto wiedzieć: Basic Auth przez zwykłe HTTP
(nie HTTPS) koduje hasło tylko w Base64 - to NIE jest szyfrowanie, każdy
podsłuchujący ruch w tej samej sieci LAN (niezbyt trudne w sieci WiFi ze
słabym szyfrowaniem lub gdy urządzenie jest współdzielone) może je
odczytać. Dla realnego bezpieczeństwa poza zaufaną siecią domową: postaw
reverse proxy z HTTPS (Caddy/nginx) przed panelem, albo używaj VPN
(WireGuard/Tailscale) zamiast wystawiać port bezpośrednio.
"""
from __future__ import annotations

import functools
import logging
import time

from flask import jsonify, request
from werkzeug.security import check_password_hash

logger = logging.getLogger("bridge.dashboard_auth")


class AuthGuard:
    def __init__(self, auth_cfg: dict | None):
        self.password_hash = (auth_cfg or {}).get("password_hash")
        self.max_attempts = (auth_cfg or {}).get("max_failed_attempts", 5)
        self.lockout_seconds = (auth_cfg or {}).get("lockout_seconds", 300)
        # proste liczenie nieudanych prób per adres IP, w pamięci procesu -
        # wystarczające dla domowego użytku, resetuje się przy restarcie
        # panelu (co jest OK - restart panelu to rzadkie, świadome zdarzenie)
        self._failed_attempts: dict[str, list[float]] = {}

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

    def check(self) -> tuple[bool, str]:
        """Zwraca (czy_ok, komunikat_błędu)."""
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

        if not check_password_hash(self.password_hash, auth.password):
            self._record_failure(ip)
            logger.warning("Nieudana próba logowania z %s", ip)
            return False, "Nieprawidłowe hasło"

        self._record_success(ip)
        return True, ""


def require_auth(guard_attr: str = "auth_guard"):
    """Dekorator dla endpointów Flask, które ZMIENIAJĄ stan (config,
    uzbrojenie/rozbrojenie). `guard_attr` to nazwa zmiennej globalnej w
    module wywołującym (dashboard/app.py), przez którą dekorator dostaje
    się do skonfigurowanego AuthGuard.

    CELOWO czyta ją z `fn.__globals__` (globalne zmienne modułu, w którym
    fizycznie zdefiniowana jest dekorowana funkcja), a NIE przez
    `import dashboard.app` - `python -m dashboard.app` uruchamia ten plik
    jako `__main__`, więc dodatkowy `import dashboard.app` w trakcie obsługi
    żądania tworzyłby DRUGĄ, osobną kopię modułu pod inną nazwą w
    sys.modules - z własnymi, nigdy nie zainicjowanymi zmiennymi globalnymi
    (auth_guard zawsze None, mimo poprawnie skonfigurowanego hasła) - stąd
    fn.__globals__ zamiast dodatkowego importu."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            guard: AuthGuard = fn.__globals__[guard_attr]
            ok, message = guard.check()
            if not ok:
                response = jsonify({"error": message})
                response.status_code = 401
                response.headers["WWW-Authenticate"] = 'Basic realm="Panel administracyjny"'
                return response
            return fn(*args, **kwargs)
        return wrapper
    return decorator
