# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import pytest

from bridge import lg_thinq_client as lg


class TestFriendlyRunState:

    def test_translates_known_values_case_insensitively(self):
        assert lg.friendly_run_state("RUNNING") == "pranie"
        assert lg.friendly_run_state("running") == "pranie"
        assert lg.friendly_run_state("END") == "zakończone"

    def test_unknown_value_passes_through_lowercased(self):
        assert lg.friendly_run_state("SOME_NEW_STATE") == "some_new_state"

    def test_empty_or_none_returns_none(self):
        assert lg.friendly_run_state(None) is None
        assert lg.friendly_run_state("") is None


class TestDecodeRunState:

    def test_decodes_verified_status_shape(self):
        assert lg.decode_run_state({"runState": {"currentState": "RUNNING"}}) == "pranie"

    def test_missing_run_state_returns_none(self):
        assert lg.decode_run_state({"someOtherResource": {}}) is None
        assert lg.decode_run_state({}) is None
        assert lg.decode_run_state(None) is None

    def test_non_dict_run_state_returns_none(self):
        assert lg.decode_run_state({"runState": "not-a-dict"}) is None


class TestListPossibleRunStates:

    def test_extracts_readable_enum_values(self):
        profile = {
            "property": {
                "runState": {
                    "currentState": {
                        "type": "enum", "mode": ["r"],
                        "value": {"r": ["POWER_OFF", "RUNNING", "END"]},
                    },
                },
            },
        }
        assert lg.list_possible_run_states(profile) == ["POWER_OFF", "RUNNING", "END"]

    def test_missing_path_returns_empty_list(self):
        assert lg.list_possible_run_states({}) == []
        assert lg.list_possible_run_states({"property": {}}) == []
        assert lg.list_possible_run_states(None) == []


class _FakeThinQApi:
    """Podmienia bridge.lg_thinq_client.ThinQApi w testach - zapisuje wywołania
    i zwraca z góry ustawione odpowiedzi zamiast łączyć się z prawdziwym API."""

    instances = []

    def __init__(self, session, access_token, country_code, client_id, mock_response=False):
        self.session = session
        self.access_token = access_token
        self.country_code = country_code
        self.client_id = client_id
        self.calls = []
        _FakeThinQApi.instances.append(self)

    async def async_get_device_list(self, timeout=15):
        self.calls.append(("list", None))
        return self._device_list

    async def async_get_device_profile(self, device_id, timeout=15):
        self.calls.append(("profile", device_id))
        return self._profile

    async def async_get_device_status(self, device_id, timeout=15):
        self.calls.append(("status", device_id))
        return self._statuses[device_id]


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    _FakeThinQApi.instances = []
    yield
    _FakeThinQApi.instances = []


class TestListDevices:

    async def test_parses_device_id_and_info(self, monkeypatch):
        _FakeThinQApi._device_list = [
            {"deviceId": "dev-1", "deviceInfo": {"alias": "Pralka", "deviceType": "DEVICE_WASHER", "modelName": "F4WV"}},
            {"deviceId": "", "deviceInfo": {"alias": "bez ID"}},
        ]
        monkeypatch.setattr(lg, "ThinQApi", _FakeThinQApi)

        devices = await lg.list_devices("pat", "PL", "client-1")

        assert devices == [
            {"device_id": "dev-1", "alias": "Pralka", "device_type": "DEVICE_WASHER", "model_name": "F4WV"},
        ]

    async def test_wraps_thinq_api_exception(self, monkeypatch):
        from thinqconnect.thinq_api import ThinQAPIException

        class _Failing(_FakeThinQApi):
            async def async_get_device_list(self, timeout=15):
                raise ThinQAPIException(code="1103", message="invalid token", headers={})

        monkeypatch.setattr(lg, "ThinQApi", _Failing)

        with pytest.raises(lg.LgThinqError, match="Chmura LG ThinQ zgłosiła błąd"):
            await lg.list_devices("bad-pat", "PL", "client-1")


class TestFetchPossibleStates:

    async def test_returns_friendly_states(self, monkeypatch):
        _FakeThinQApi._profile = {
            "property": {"runState": {"currentState": {"value": {"r": ["POWER_OFF", "RUNNING"]}}}},
        }
        monkeypatch.setattr(lg, "ThinQApi", _FakeThinQApi)

        states = await lg.fetch_possible_states("pat", "PL", "client-1", "dev-1")

        assert states == ["wyłączona", "pranie"]


class TestLgThinqClientPolling:

    async def test_poll_device_calls_on_state_when_state_changes(self, monkeypatch):
        _FakeThinQApi._statuses = {"dev-1": {"runState": {"currentState": "RUNNING"}}}
        monkeypatch.setattr(lg, "ThinQApi", _FakeThinQApi)

        seen = []

        async def on_state(name, state):
            seen.append((name, state))

        client = lg.LgThinqClient(
            {"personal_access_token": "pat", "client_id": "client-1", "country_code": "PL",
             "devices": {"Pralka": {"device_id": "dev-1"}}},
            on_state,
        )
        watch = client._devices["Pralka"]
        api = _FakeThinQApi(session=None, access_token="pat", country_code="PL", client_id="client-1")

        await client._poll_device(api, watch)

        assert seen == [("Pralka", "pranie")]
        assert watch.last_state == "pranie"

    async def test_poll_device_skips_callback_when_state_unchanged(self, monkeypatch):
        _FakeThinQApi._statuses = {"dev-1": {"runState": {"currentState": "RUNNING"}}}
        monkeypatch.setattr(lg, "ThinQApi", _FakeThinQApi)

        seen = []

        async def on_state(name, state):
            seen.append((name, state))

        client = lg.LgThinqClient(
            {"personal_access_token": "pat", "client_id": "client-1",
             "devices": {"Pralka": {"device_id": "dev-1"}}},
            on_state,
        )
        watch = client._devices["Pralka"]
        watch.last_state = "pranie"
        api = _FakeThinQApi(session=None, access_token="pat", country_code="PL", client_id="client-1")

        await client._poll_device(api, watch)

        assert seen == []

    async def test_poll_device_handles_missing_run_state_gracefully(self, monkeypatch):
        _FakeThinQApi._statuses = {"dev-1": {"someOtherResource": {}}}
        monkeypatch.setattr(lg, "ThinQApi", _FakeThinQApi)

        async def on_state(name, state):
            raise AssertionError("nie powinno się wywołać - brak runState")

        client = lg.LgThinqClient(
            {"personal_access_token": "pat", "client_id": "client-1",
             "devices": {"Pralka": {"device_id": "dev-1"}}},
            on_state,
        )
        watch = client._devices["Pralka"]
        api = _FakeThinQApi(session=None, access_token="pat", country_code="PL", client_id="client-1")

        await client._poll_device(api, watch)

        assert watch.last_state is None

    async def test_run_forever_skips_when_not_configured(self, caplog):
        async def on_state(name, state):
            raise AssertionError("nie powinno się wywołać - brak urządzeń")

        client = lg.LgThinqClient({"devices": {}}, on_state)
        with caplog.at_level("INFO", logger="bridge.lg_thinq"):
            await client.run_forever()
        assert "brak skonfigurowanych urządzeń" in caplog.text

    async def test_run_forever_skips_when_missing_credentials(self, caplog):
        async def on_state(name, state):
            raise AssertionError("nie powinno się wywołać - brak poświadczeń")

        client = lg.LgThinqClient({"devices": {"Pralka": {"device_id": "dev-1"}}}, on_state)
        with caplog.at_level("WARNING", logger="bridge.lg_thinq"):
            await client.run_forever()
        assert "brak tokenu PAT lub client_id" in caplog.text
