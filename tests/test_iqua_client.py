from dataclasses import dataclass

import pytest

from bridge import iqua_client as iqua


@dataclass
class _FakeState:
    value: str


def _fake_data(state="Online", salt_level_percent=80, today_use=12, out_of_salt_estimated_days=30):
    return type("FakeIquaSoftenerData", (), {
        "model": "ERO-EXP (123)",
        "state": _FakeState(state),
        "salt_level_percent": salt_level_percent,
        "today_use": today_use,
        "out_of_salt_estimated_days": out_of_salt_estimated_days,
    })()


class _FakeIquaSoftener:
    """Podmienia bridge.iqua_client.IquaSoftener w testach - zapisuje
    wywołania konstruktora i zwraca z góry ustawioną odpowiedź (albo zgłasza
    z góry ustawiony wyjątek) zamiast łączyć się z prawdziwą chmurą Ecowater."""

    instances = []
    _data = None
    _exception = None

    def __init__(self, username, password, device_serial_number):
        self.username = username
        self.password = password
        self.device_serial_number = device_serial_number
        _FakeIquaSoftener.instances.append(self)

    def get_data(self):
        if _FakeIquaSoftener._exception is not None:
            raise _FakeIquaSoftener._exception
        return _FakeIquaSoftener._data


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    _FakeIquaSoftener.instances = []
    _FakeIquaSoftener._data = _fake_data()
    _FakeIquaSoftener._exception = None
    monkeypatch.setattr(iqua, "IquaSoftener", _FakeIquaSoftener)
    yield
    _FakeIquaSoftener.instances = []
    _FakeIquaSoftener._exception = None


class TestTestConnection:

    async def test_returns_data_on_success(self):
        _FakeIquaSoftener._data = _fake_data(salt_level_percent=55)

        data = await iqua.test_connection("user@example.com", "haslo", "DSN123")

        assert data.salt_level_percent == 55
        assert _FakeIquaSoftener.instances[0].device_serial_number == "DSN123"

    async def test_wraps_softener_exception(self):
        from iqua_softener import IquaSoftenerException

        _FakeIquaSoftener._exception = IquaSoftenerException("Authentication error")

        with pytest.raises(iqua.IquaError, match="Chmura iQua"):
            await iqua.test_connection("user@example.com", "zle-haslo", "DSN123")


class TestIquaClientPolling:

    async def test_poll_device_always_calls_on_state(self):
        _FakeIquaSoftener._data = _fake_data(state="Online", salt_level_percent=70)
        seen = []

        async def on_state(name, data):
            seen.append((name, data))

        client = iqua.IquaClient(
            {"username": "user@example.com", "password": "haslo",
             "devices": {"Zmiękczacz": {"serial_number": "DSN123"}}},
            on_state,
        )
        watch = client._devices["Zmiękczacz"]
        watch.last_state = "online"
        fake = _FakeIquaSoftener("user@example.com", "haslo", "DSN123")

        import asyncio
        await client._poll_device(asyncio.get_event_loop(), fake, watch)

        assert len(seen) == 1
        assert seen[0][0] == "Zmiękczacz"
        assert seen[0][1].salt_level_percent == 70
        assert watch.last_state == "online"

    async def test_poll_device_handles_errors_gracefully(self):
        from iqua_softener import IquaSoftenerException

        _FakeIquaSoftener._exception = IquaSoftenerException("boom")

        async def on_state(name, data):
            raise AssertionError("nie powinno się wywołać przy błędzie")

        client = iqua.IquaClient(
            {"username": "user@example.com", "password": "haslo",
             "devices": {"Zmiękczacz": {"serial_number": "DSN123"}}},
            on_state,
        )
        watch = client._devices["Zmiękczacz"]
        fake = _FakeIquaSoftener("user@example.com", "haslo", "DSN123")

        import asyncio
        await client._poll_device(asyncio.get_event_loop(), fake, watch)

        assert watch.last_state is None

    async def test_run_forever_skips_when_not_configured(self, caplog):
        async def on_state(name, data):
            raise AssertionError("nie powinno się wywołać - brak urządzeń")

        client = iqua.IquaClient({"devices": {}}, on_state)
        with caplog.at_level("INFO", logger="bridge.iqua"):
            await client.run_forever()
        assert "brak skonfigurowanych urządzeń" in caplog.text

    async def test_run_forever_skips_when_missing_credentials(self, caplog):
        async def on_state(name, data):
            raise AssertionError("nie powinno się wywołać - brak poświadczeń")

        client = iqua.IquaClient({"devices": {"Zmiękczacz": {"serial_number": "DSN123"}}}, on_state)
        with caplog.at_level("WARNING", logger="bridge.iqua"):
            await client.run_forever()
        assert "brak loginu/hasła" in caplog.text
