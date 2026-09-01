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
SESSION_TTL_SECONDS = 12 * 3600
_SESSION_SALT = "stb-session-v1"


def _password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


class AuthGuard:
    def __init__(self, config_store, auth_cfg: dict | None):
        self.config_store = config_store
        self.max_attempts = (auth_cfg or {}).get("max_failed_attempts", 5)
        self.lockout_seconds = (auth_cfg or {}).get("lockout_seconds", 300)
        self.allow_basic_auth = (auth_cfg or {}).get("allow_basic_auth", True)
        self._failed_attempts: dict[str, list[float]] = {}
        self._serializer = itsdangerous.URLSafeTimedSerializer(
            config_store.get_or_create_session_secret(), salt=_SESSION_SALT,
        )
        self._revoked_tokens: set[str] = set()

    @property
    def configured(self) -> bool:
        return self.config_store.user_count() > 0

    def update_config(self, config_store, auth_cfg: dict | None) -> None:
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

    def check_password(self, username: str, password: str) -> tuple[bool, str]:
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
        still_relevant = set()
        for token in self._revoked_tokens:
            try:
                self._serializer.loads(token, max_age=SESSION_TTL_SECONDS)
            except itsdangerous.BadSignature:
                continue
            still_relevant.add(token)
        self._revoked_tokens = still_relevant

    def create_session(self, username: str) -> str:
        user = self.config_store.get_user_by_username(username)
        pwf = _password_fingerprint(user["password_hash"]) if user else ""
        return self._serializer.dumps({"username": username, "pwf": pwf})

    def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        self._prune_revoked_tokens()
        self._revoked_tokens.add(token)

    def _decode_session(self, token: str | None) -> dict | None:
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
        data = self._decode_session(token)
        return data["username"] if data else None

    def check(self) -> tuple[bool, str]:
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
