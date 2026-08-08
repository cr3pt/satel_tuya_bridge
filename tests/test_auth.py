"""Testy dla dashboard/auth.py - HTTP Basic Auth + ochrona przed brute-force."""
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash

from dashboard.auth import AuthGuard


def _fake_request(password: str | None, ip: str = "127.0.0.1"):
    req = MagicMock()
    req.remote_addr = ip
    if password is None:
        req.authorization = None
    else:
        auth = MagicMock()
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
        assert "Nieprawidłowe" in msg

    def test_missing_auth_header_fails(self):
        pw_hash = generate_password_hash("sekret123")
        guard = AuthGuard({"password_hash": pw_hash})
        with patch("dashboard.auth.request", _fake_request(None)):
            ok, msg = guard.check()
        assert ok is False


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
