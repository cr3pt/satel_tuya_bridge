"""Testy dla dashboard/auth.py - logowanie sesyjne (/login) + HTTP Basic Auth
(fallback dla skryptów) + ochrona przed brute-force + wiele kont
administratorskich (patrz bridge/config_store.py: tabela users)."""
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.security import generate_password_hash

from bridge.config_store import ConfigStore
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


@pytest.fixture
def store(tmp_path):
    """ConfigStore z jednym kontem: admin/sekret123."""
    s = ConfigStore(tmp_path / "config.db")
    s.add_user("admin", generate_password_hash("sekret123"))
    return s


@pytest.fixture
def empty_store(tmp_path):
    """ConfigStore BEZ żadnego konta - stan "nieskonfigurowany"."""
    return ConfigStore(tmp_path / "empty.db")


class TestAuthGuardBasics:
    def test_unconfigured_password_always_fails(self, empty_store):
        guard = AuthGuard(empty_store, None)
        with patch("dashboard.auth.request", _fake_request("dowolne")):
            ok, msg = guard.check()
        assert ok is False
        assert "konta administratora" in msg

    def test_correct_password_succeeds(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, msg = guard.check()
        assert ok is True

    def test_wrong_password_fails(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request("złe_hasło")):
            ok, msg = guard.check()
        assert ok is False
        assert "Nieprawidłow" in msg

    def test_missing_auth_header_fails(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None)):
            ok, msg = guard.check()
        assert ok is False


class TestMultipleAccounts:
    """Wiele kont administratorskich - płaski model uprawnień, każde
    zalogowane konto ma pełny dostęp do zapisu."""

    def test_second_account_can_log_in_independently(self, store):
        store.add_user("drugi", generate_password_hash("inne_haslo"))
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, _ = guard.check_password("drugi", "inne_haslo")
        assert ok is True

    def test_wrong_username_fails_even_with_correct_password_of_other_account(self, store):
        store.add_user("drugi", generate_password_hash("inne_haslo"))
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, msg = guard.check_password("drugi", "sekret123")  # hasło NALEŻY do "admin"
        assert ok is False
        assert "Nieprawidłow" in msg

    def test_basic_auth_wrong_username_fails_even_with_correct_password(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request("sekret123", username="ktoinny")):
            ok, msg = guard.check()
        assert ok is False
        assert "Nieprawidłow" in msg

    def test_basic_auth_correct_username_and_password_succeeds(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request("sekret123", username="admin")):
            ok, _ = guard.check()
        assert ok is True

    def test_session_username_reflects_which_account_logged_in(self, store):
        store.add_user("drugi", generate_password_hash("inne_haslo"))
        guard = AuthGuard(store, None)
        token = guard.create_session("drugi")
        assert guard.session_username(token) == "drugi"


class TestBruteForceProtection:
    def test_lockout_after_max_attempts(self, store):
        guard = AuthGuard(store, {"max_failed_attempts": 3, "lockout_seconds": 300})

        for _ in range(3):
            with patch("dashboard.auth.request", _fake_request("złe")):
                ok, _ = guard.check()
                assert ok is False

        # 4. próba, TERAZ Z POPRAWNYM hasłem - powinna być zablokowana mimo to
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, msg = guard.check()
        assert ok is False
        assert "Zbyt wiele" in msg

    def test_successful_login_resets_failure_count(self, store):
        guard = AuthGuard(store, {"max_failed_attempts": 3, "lockout_seconds": 300})

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

    def test_lockout_is_per_ip(self, store):
        guard = AuthGuard(store, {"max_failed_attempts": 2, "lockout_seconds": 300})

        for _ in range(2):
            with patch("dashboard.auth.request", _fake_request("złe", ip="10.0.0.1")):
                guard.check()

        # inny adres IP nie powinien być objęty blokadą
        with patch("dashboard.auth.request", _fake_request("sekret123", ip="10.0.0.2")):
            ok, _ = guard.check()
        assert ok is True


class TestBasicAuthToggle:
    """allow_basic_auth=False - "tryb logowania" wymuszający wyłącznie
    sesję (⚙ Ustawienia ogólne)."""

    def test_basic_auth_disabled_rejects_even_correct_credentials(self, store):
        guard = AuthGuard(store, {"allow_basic_auth": False})
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, msg = guard.check()
        assert ok is False
        assert "Basic Auth" in msg

    def test_basic_auth_disabled_still_allows_session(self, store):
        guard = AuthGuard(store, {"allow_basic_auth": False})
        token = guard.create_session("admin")
        with patch("dashboard.auth.request", _fake_request(None, session_token=token)):
            ok, _ = guard.check()
        assert ok is True

    def test_basic_auth_enabled_by_default(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, _ = guard.check()
        assert ok is True


class TestSessionLogin:
    """Logowanie sesyjne (/login, /api/login, /api/logout) - zastępuje
    prompt()-owy Basic Auth w panelu webowym: hasło wpisywane RAZ w
    zamaskowanym polu formularza, dalsze żądania idą przez losowy token w
    ciasteczku HttpOnly, nie przez ponowne wysyłanie hasła."""

    def test_check_password_succeeds_with_correct_password(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, _ = guard.check_password("admin", "sekret123")
        assert ok is True

    def test_check_password_fails_with_wrong_password(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, msg = guard.check_password("admin", "złe")
        assert ok is False
        assert "Nieprawidłow" in msg

    def test_check_password_respects_lockout(self, store):
        guard = AuthGuard(store, {"max_failed_attempts": 2, "lockout_seconds": 300})
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            guard.check_password("admin", "złe")
            guard.check_password("admin", "złe")
            ok, msg = guard.check_password("admin", "sekret123")
        assert ok is False
        assert "Zbyt wiele" in msg

    def test_check_password_fails_when_unconfigured(self, empty_store):
        guard = AuthGuard(empty_store, None)
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, msg = guard.check_password("admin", "cokolwiek")
        assert ok is False
        assert "konta administratora" in msg

    def test_create_session_returns_unique_tokens(self, store):
        guard = AuthGuard(store, None)
        t1 = guard.create_session("admin")
        t2 = guard.create_session("admin")
        assert t1 != t2
        assert guard.check_session(t1) is True
        assert guard.check_session(t2) is True

    def test_check_session_rejects_unknown_token(self, store):
        guard = AuthGuard(store, None)
        assert guard.check_session("nieznany-token") is False

    def test_check_session_rejects_none(self, store):
        guard = AuthGuard(store, None)
        assert guard.check_session(None) is False

    def test_destroy_session_invalidates_token(self, store):
        guard = AuthGuard(store, None)
        token = guard.create_session("admin")
        guard.destroy_session(token)
        assert guard.check_session(token) is False

    def test_destroy_session_on_unknown_token_is_noop(self, store):
        guard = AuthGuard(store, None)
        guard.destroy_session("nieznany-token")  # nie powinno rzucić wyjątku

    def test_expired_session_is_rejected(self, store):
        guard = AuthGuard(store, None)
        token = guard.create_session("admin")
        guard._sessions[token]["expires"] = 0  # symulacja wygaśnięcia (timestamp w przeszłości)
        assert guard.check_session(token) is False

    def test_check_accepts_valid_session_cookie_without_password(self, store):
        """Kluczowe zachowanie: PO zalogowaniu żądania NIE muszą już wysyłać
        hasła w ogóle - sam ważny token w ciasteczku wystarcza."""
        guard = AuthGuard(store, None)
        token = guard.create_session("admin")
        with patch("dashboard.auth.request", _fake_request(None, session_token=token)):
            ok, _ = guard.check()
        assert ok is True

    def test_check_falls_back_to_basic_auth_without_session(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, _ = guard.check()
        assert ok is True

    def test_check_rejects_invalid_session_token(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, session_token="podrobiony-token")):
            ok, _ = guard.check()
        assert ok is False


class TestUpdateConfig:
    """AuthGuard.update_config() - odświeżenie po create_app() BEZ utraty
    aktywnych sesji/liczników prób (patrz dashboard/app.py: create_app)."""

    def test_preserves_active_sessions(self, store):
        guard = AuthGuard(store, None)
        token = guard.create_session("admin")
        guard.update_config(store, {"max_failed_attempts": 10})
        assert guard.check_session(token) is True

    def test_updates_lockout_settings(self, store):
        guard = AuthGuard(store, {"max_failed_attempts": 5})
        guard.update_config(store, {"max_failed_attempts": 1, "lockout_seconds": 60})
        assert guard.max_attempts == 1
        assert guard.lockout_seconds == 60

    def test_updates_config_store_reference(self, store, tmp_path):
        guard = AuthGuard(store, None)
        new_store = ConfigStore(tmp_path / "other.db")
        new_store.add_user("inny", generate_password_hash("haslo123"))
        guard.update_config(new_store, None)
        with patch("dashboard.auth.request", _fake_request(None, ip="127.0.0.1")):
            ok, _ = guard.check_password("inny", "haslo123")
        assert ok is True
