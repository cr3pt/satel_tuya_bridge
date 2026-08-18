"""Testy dla dashboard/auth.py - logowanie sesyjne (/login) + HTTP Basic Auth
(fallback dla skryptów) + ochrona przed brute-force."""
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash

from dashboard.auth import AuthGuard, SESSION_COOKIE_NAME


def _fake_request(password: str | None, ip: str = "127.0.0.1", session_token: str | None = None,
                   username: str = "admin"):
    req = MagicMock()
    req.remote_addr = ip
    req.cookies = {SESSION_COOKIE_NAME: session_token} if session_token else {}
    if password is None:
        req.authorization = None
    else:
        auth = MagicMock()
        auth.username = username
        auth.password = password
        req.authorization = auth
    return req


class TestAuthGuardBasics:
    def test_unconfigured_password_always_fails(self):
        guard = AuthGuard(None)
        with patch("dashboard.auth.request", _fake_request("dowolne")):
            ok, msg = guard.check()
        assert ok is False
        assert "hasła administratora" in msg

    def test_correct_password_succeeds(self):
        pw_hash = generate_password_hash("sekret123")
        guard = AuthGuard({"password_hash": pw_hash})
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, msg = guard.check()
        assert ok is True

    def test_wrong_password_fails(self):
        pw_hash = generate_password_hash("sekret123")
        guard = AuthGuard({"password_hash": pw_hash})
        with patch("dashboard.auth.request", _fake_request("złe_hasło")):
            ok, msg = guard.check()
        assert ok is False
        assert "Nieprawidłow" in msg

    def test_missing_auth_header_fails(self):
        pw_hash = generate_password_hash("sekret123")
        guard = AuthGuard({"password_hash": pw_hash})
        with patch("dashboard.auth.request", _fake_request(None)):
            ok, msg = guard.check()
        assert ok is False


class TestUsernameHandling:
    """AuthGuard.username - nazwa użytkownika ustawiana raz w kreatorze
    (/setup), przechowywana jawnym tekstem w config.yaml (NIE jest sekretem
    - jak login, nie hasło). Domyślnie "admin" dla configów sprzed tej
    zmiany (brak pola dashboard.auth.username), żeby aktualizacja pakietu
    nie zablokowała istniejących instalacji."""

    def test_defaults_to_admin_when_not_configured(self):
        guard = AuthGuard({"password_hash": generate_password_hash("sekret123")})
        assert guard.username == "admin"

    def test_uses_configured_username(self):
        guard = AuthGuard({"username": "szef", "password_hash": generate_password_hash("sekret123")})
        assert guard.username == "szef"

    def test_basic_auth_wrong_username_fails_even_with_correct_password(self):
        guard = AuthGuard({"username": "szef", "password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request("sekret123", username="ktoinny")):
            ok, msg = guard.check()
        assert ok is False
        assert "Nieprawidłowa nazwa użytkownika" in msg

    def test_basic_auth_correct_username_and_password_succeeds(self):
        guard = AuthGuard({"username": "szef", "password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request("sekret123", username="szef")):
            ok, _ = guard.check()
        assert ok is True

    def test_check_password_wrong_username_fails_even_with_correct_password(self):
        guard = AuthGuard({"username": "szef", "password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, msg = guard.check_password("ktoinny", "sekret123")
        assert ok is False
        assert "Nieprawidłowa nazwa użytkownika" in msg

    def test_check_password_correct_username_and_password_succeeds(self):
        guard = AuthGuard({"username": "szef", "password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, _ = guard.check_password("szef", "sekret123")
        assert ok is True


class TestBruteForceProtection:
    def test_lockout_after_max_attempts(self):
        pw_hash = generate_password_hash("sekret123")
        guard = AuthGuard({"password_hash": pw_hash, "max_failed_attempts": 3, "lockout_seconds": 300})

        for _ in range(3):
            with patch("dashboard.auth.request", _fake_request("złe")):
                ok, _ = guard.check()
                assert ok is False

        # 4. próba, TERAZ Z POPRAWNYM hasłem - powinna być zablokowana mimo to
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, msg = guard.check()
        assert ok is False
        assert "Zbyt wiele" in msg

    def test_successful_login_resets_failure_count(self):
        pw_hash = generate_password_hash("sekret123")
        guard = AuthGuard({"password_hash": pw_hash, "max_failed_attempts": 3, "lockout_seconds": 300})

        with patch("dashboard.auth.request", _fake_request("złe")):
            guard.check()
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, _ = guard.check()
        assert ok is True

        # licznik nieudanych prób powinien być wyzerowany po sukcesie
        for _ in range(2):
            with patch("dashboard.auth.request", _fake_request("złe")):
                guard.check()
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, _ = guard.check()
        assert ok is True  # wciąż OK, bo nie osiągnęliśmy 3 kolejnych błędów po resecie

    def test_lockout_is_per_ip(self):
        pw_hash = generate_password_hash("sekret123")
        guard = AuthGuard({"password_hash": pw_hash, "max_failed_attempts": 2, "lockout_seconds": 300})

        for _ in range(2):
            with patch("dashboard.auth.request", _fake_request("złe", ip="10.0.0.1")):
                guard.check()

        # inny adres IP nie powinien być objęty blokadą
        with patch("dashboard.auth.request", _fake_request("sekret123", ip="10.0.0.2")):
            ok, _ = guard.check()
        assert ok is True


class TestSessionLogin:
    """Logowanie sesyjne (/login, /api/login, /api/logout) - zastępuje
    prompt()-owy Basic Auth w panelu webowym: hasło wpisywane RAZ w
    zamaskowanym polu formularza, dalsze żądania idą przez losowy token w
    ciasteczku HttpOnly, nie przez ponowne wysyłanie hasła."""

    def test_check_password_succeeds_with_correct_password(self):
        guard = AuthGuard({"password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, _ = guard.check_password("admin", "sekret123")
        assert ok is True

    def test_check_password_fails_with_wrong_password(self):
        guard = AuthGuard({"password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, msg = guard.check_password("admin", "złe")
        assert ok is False
        assert "Nieprawidłow" in msg

    def test_check_password_respects_lockout(self):
        guard = AuthGuard({"password_hash": generate_password_hash("sekret123"), "max_failed_attempts": 2, "lockout_seconds": 300})
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            guard.check_password("admin", "złe")
            guard.check_password("admin", "złe")
            ok, msg = guard.check_password("admin", "sekret123")
        assert ok is False
        assert "Zbyt wiele" in msg

    def test_check_password_fails_when_unconfigured(self):
        guard = AuthGuard(None)
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, msg = guard.check_password("admin", "cokolwiek")
        assert ok is False
        assert "hasła administratora" in msg

    def test_create_session_returns_unique_tokens(self):
        guard = AuthGuard({"password_hash": generate_password_hash("x")})
        t1 = guard.create_session()
        t2 = guard.create_session()
        assert t1 != t2
        assert guard.check_session(t1) is True
        assert guard.check_session(t2) is True

    def test_check_session_rejects_unknown_token(self):
        guard = AuthGuard({"password_hash": generate_password_hash("x")})
        assert guard.check_session("nieznany-token") is False

    def test_check_session_rejects_none(self):
        guard = AuthGuard({"password_hash": generate_password_hash("x")})
        assert guard.check_session(None) is False

    def test_destroy_session_invalidates_token(self):
        guard = AuthGuard({"password_hash": generate_password_hash("x")})
        token = guard.create_session()
        guard.destroy_session(token)
        assert guard.check_session(token) is False

    def test_destroy_session_on_unknown_token_is_noop(self):
        guard = AuthGuard({"password_hash": generate_password_hash("x")})
        guard.destroy_session("nieznany-token")  # nie powinno rzucić wyjątku

    def test_expired_session_is_rejected(self):
        guard = AuthGuard({"password_hash": generate_password_hash("x")})
        token = guard.create_session()
        guard._sessions[token] = 0  # symulacja wygaśnięcia (timestamp w przeszłości)
        assert guard.check_session(token) is False

    def test_check_accepts_valid_session_cookie_without_password(self):
        """Kluczowe zachowanie: PO zalogowaniu żądania NIE muszą już wysyłać
        hasła w ogóle - sam ważny token w ciasteczku wystarcza."""
        guard = AuthGuard({"password_hash": generate_password_hash("sekret123")})
        token = guard.create_session()
        with patch("dashboard.auth.request", _fake_request(None, session_token=token)):
            ok, _ = guard.check()
        assert ok is True

    def test_check_falls_back_to_basic_auth_without_session(self):
        guard = AuthGuard({"password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, _ = guard.check()
        assert ok is True

    def test_check_rejects_invalid_session_token(self):
        guard = AuthGuard({"password_hash": generate_password_hash("sekret123")})
        with patch("dashboard.auth.request", _fake_request(None, session_token="podrobiony-token")):
            ok, _ = guard.check()
        assert ok is False
