import time
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.security import generate_password_hash

from bridge.config_store import ConfigStore
from dashboard.auth import AuthGuard, SESSION_COOKIE_NAME


def _fake_request(password: str | None, ip: str = "127.0.0.1", session_token: str | None = None,
                   username: str = "admin", bearer_token: str | None = None):
    req = MagicMock()
    req.remote_addr = ip
    req.cookies = {SESSION_COOKIE_NAME: session_token} if session_token else {}
    req.headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
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
    s = ConfigStore(tmp_path / "config.db")
    s.add_user("admin", generate_password_hash("sekret123"))
    return s


@pytest.fixture
def empty_store(tmp_path):
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
            ok, msg = guard.check_password("drugi", "sekret123")
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

        for _ in range(2):
            with patch("dashboard.auth.request", _fake_request("złe")):
                guard.check()
        with patch("dashboard.auth.request", _fake_request("sekret123")):
            ok, _ = guard.check()
        assert ok is True

    def test_lockout_is_per_ip(self, store):
        guard = AuthGuard(store, {"max_failed_attempts": 2, "lockout_seconds": 300})

        for _ in range(2):
            with patch("dashboard.auth.request", _fake_request("złe", ip="10.0.0.1")):
                guard.check()

        with patch("dashboard.auth.request", _fake_request("sekret123", ip="10.0.0.2")):
            ok, _ = guard.check()
        assert ok is True


class TestBasicAuthToggle:

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

    def test_create_session_tokens_are_valid_and_carry_username(self, store):
        guard = AuthGuard(store, None)
        token = guard.create_session("admin")
        assert guard.check_session(token) is True
        assert guard.session_username(token) == "admin"

    def test_create_session_differs_per_username(self, store):
        store.add_user("drugi", generate_password_hash("inne_haslo"))
        guard = AuthGuard(store, None)
        assert guard.create_session("admin") != guard.create_session("drugi")

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
        guard.destroy_session("nieznany-token")

    def test_expired_session_is_rejected(self, store):
        from dashboard.auth import SESSION_TTL_SECONDS

        guard = AuthGuard(store, None)
        token = guard.create_session("admin")
        assert guard.check_session(token) is True
        with patch("time.time", return_value=time.time() + SESSION_TTL_SECONDS + 1):
            assert guard.check_session(token) is False

    def test_session_survives_new_authguard_with_same_store(self, store):
        token = AuthGuard(store, None).create_session("admin")
        fresh_guard = AuthGuard(store, None)
        assert fresh_guard.check_session(token) is True
        assert fresh_guard.session_username(token) == "admin"

    def test_session_invalidated_when_password_changes(self, store):
        guard = AuthGuard(store, None)
        token = guard.create_session("admin")
        assert guard.check_session(token) is True

        user = store.get_user_by_username("admin")
        store.update_user(user["id"], password_hash=generate_password_hash("nowe_haslo"))
        assert guard.check_session(token) is False

    def test_check_accepts_valid_session_cookie_without_password(self, store):
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


class TestApiTokenAuth:

    def test_valid_bearer_token_succeeds(self, store):
        _id, token = store.create_api_token("home_assistant")
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, bearer_token=token)):
            ok, _ = guard.check()
        assert ok is True

    def test_unknown_bearer_token_fails(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, bearer_token="nieznany-token")):
            ok, _ = guard.check()
        assert ok is False

    def test_no_authorization_header_falls_through_to_basic_auth_required(self, store):
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None)):
            ok, msg = guard.check()
        assert ok is False
        assert "Wymagana autoryzacja" in msg

    def test_revoked_token_stops_working(self, store):
        token_id, token = store.create_api_token("skrypt")
        guard = AuthGuard(store, None)
        with patch("dashboard.auth.request", _fake_request(None, bearer_token=token)):
            assert guard.check()[0] is True
        store.delete_api_token(token_id)
        with patch("dashboard.auth.request", _fake_request(None, bearer_token=token)):
            assert guard.check()[0] is False

    def test_bearer_token_ignores_lockout(self, store):
        guard = AuthGuard(store, {"max_failed_attempts": 1, "lockout_seconds": 300})
        with patch("dashboard.auth.request", _fake_request("złe")):
            guard.check()
        _id, token = store.create_api_token("skrypt")
        with patch("dashboard.auth.request", _fake_request(None, bearer_token=token)):
            ok, _ = guard.check()
        assert ok is True

    def test_bearer_token_works_even_when_basic_auth_disabled(self, store):
        guard = AuthGuard(store, {"allow_basic_auth": False})
        _id, token = store.create_api_token("skrypt")
        with patch("dashboard.auth.request", _fake_request(None, bearer_token=token)):
            ok, _ = guard.check()
        assert ok is True


class TestUpdateConfig:

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
