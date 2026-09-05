import pytest

from bridge.config_store import ConfigStore


@pytest.fixture
def store(tmp_path):
    return ConfigStore(tmp_path / "config.db")


class TestApiTokens:
    def test_create_returns_id_and_raw_token(self, store):
        token_id, token = store.create_api_token("home_assistant")
        assert isinstance(token_id, int)
        assert len(token) == 64

    def test_token_is_not_stored_in_plaintext(self, store):
        _id, token = store.create_api_token("skrypt")
        with store._lock:
            row = store._conn.execute("SELECT token_hash FROM api_tokens").fetchone()
        assert token not in row[0]

    def test_list_reflects_created_tokens(self, store):
        store.create_api_token("a")
        store.create_api_token("b")
        names = [t["name"] for t in store.list_api_tokens()]
        assert names == ["a", "b"]

    def test_list_never_includes_the_raw_token(self, store):
        store.create_api_token("skrypt")
        entry = store.list_api_tokens()[0]
        assert "token" not in entry
        assert "token_hash" not in entry

    def test_verify_valid_token_returns_metadata(self, store):
        token_id, token = store.create_api_token("skrypt")
        result = store.verify_api_token(token)
        assert result == {"id": token_id, "name": "skrypt"}

    def test_verify_unknown_token_returns_none(self, store):
        assert store.verify_api_token("not-a-real-token") is None

    def test_verify_updates_last_used_ts(self, store):
        _id, token = store.create_api_token("skrypt")
        assert store.list_api_tokens()[0]["last_used_ts"] is None
        store.verify_api_token(token)
        assert store.list_api_tokens()[0]["last_used_ts"] is not None

    def test_delete_revokes_token(self, store):
        token_id, token = store.create_api_token("skrypt")
        store.delete_api_token(token_id)
        assert store.verify_api_token(token) is None
        assert store.list_api_tokens() == []

    def test_delete_unknown_id_is_noop(self, store):
        store.delete_api_token(999)
