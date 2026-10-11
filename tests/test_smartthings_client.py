# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import pytest

from bridge import smartthings_client as st


def _fake_device(device_id="dev-1", label="Lampka", name="switch", type_="OCF", device_type_name="V1"):
    return type("FakeDevice", (), {
        "device_id": device_id, "label": label, "name": name,
        "type": type_, "device_type_name": device_type_name,
    })()


class _FakeSmartThings:
    """Podmienia bridge.smartthings_client.SmartThings w testach - zapisuje
    wywołania i zwraca z góry ustawioną odpowiedź (albo zgłasza z góry
    ustawiony wyjątek) zamiast łączyć się z prawdziwą chmurą SmartThings."""

    instances = []
    _devices = []
    _status_by_device = {}
    _exception = None

    def __init__(self):
        self.token = None
        self.closed = False
        _FakeSmartThings.instances.append(self)

    def authenticate(self, token):
        self.token = token

    async def get_devices(self):
        if _FakeSmartThings._exception is not None:
            raise _FakeSmartThings._exception
        return _FakeSmartThings._devices

    async def get_raw_device_status(self, device_id):
        if _FakeSmartThings._exception is not None:
            raise _FakeSmartThings._exception
        return _FakeSmartThings._status_by_device.get(device_id, {"components": {"main": {}}})

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    _FakeSmartThings.instances = []
    _FakeSmartThings._devices = []
    _FakeSmartThings._status_by_device = {}
    _FakeSmartThings._exception = None
    monkeypatch.setattr(st, "SmartThings", _FakeSmartThings)
    yield
    _FakeSmartThings.instances = []
    _FakeSmartThings._exception = None


class TestExtractAttributeValue:

    def test_extracts_nested_value(self):
        raw = {"components": {"main": {"switch": {"switch": {"value": "on"}}}}}
        assert st.extract_attribute_value(raw, "switch", "switch") == "on"

    def test_missing_path_returns_none(self):
        assert st.extract_attribute_value({"components": {}}, "switch", "switch") is None
        assert st.extract_attribute_value(None, "switch", "switch") is None
        assert st.extract_attribute_value({"components": {"main": {}}}, "contactSensor", "contact") is None

    def test_custom_component(self):
        raw = {"components": {"outlet1": {"switch": {"switch": {"value": "off"}}}}}
        assert st.extract_attribute_value(raw, "switch", "switch", component="outlet1") == "off"


class TestFriendlyStatusValue:

    def test_translates_switch_values(self):
        assert st.friendly_status_value("switch", "on") == "włączone"
        assert st.friendly_status_value("switch", "off") == "wyłączone"
        assert st.friendly_status_value("switch", "ON") == "włączone"

    def test_other_capabilities_pass_through_as_text(self):
        assert st.friendly_status_value("contactSensor", "open") == "open"
        assert st.friendly_status_value("temperatureMeasurement", 21.5) == "21.5"

    def test_none_returns_none(self):
        assert st.friendly_status_value("switch", None) is None


class TestListDevices:

    async def test_parses_device_fields(self):
        _FakeSmartThings._devices = [_fake_device(device_id="d1", label="Lampka salon", type_="OCF", device_type_name="V1")]

        devices = await st.list_devices("pat")

        assert devices == [
            {"device_id": "d1", "alias": "Lampka salon", "device_type": "OCF", "model_name": "V1"},
        ]
        assert _FakeSmartThings.instances[0].token == "pat"
        assert _FakeSmartThings.instances[0].closed is True

    async def test_wraps_api_exception(self):
        from pysmartthings.exceptions import SmartThingsAuthenticationFailedError

        _FakeSmartThings._exception = SmartThingsAuthenticationFailedError("invalid token")

        with pytest.raises(st.SmartThingsError, match="Chmura SmartThings zgłosiła błąd"):
            await st.list_devices("bad-pat")


class TestSmartThingsClientPolling:

    async def test_poll_device_calls_on_state_when_state_changes(self):
        _FakeSmartThings._status_by_device = {
            "dev-1": {"components": {"main": {"switch": {"switch": {"value": "on"}}}}},
        }
        seen = []

        async def on_state(name, state):
            seen.append((name, state))

        client = st.SmartThingsClient(
            {"personal_access_token": "pat", "devices": {"Lampka": {"device_id": "dev-1"}}},
            on_state,
        )
        watch = client._devices["Lampka"]
        fake = _FakeSmartThings()

        await client._poll_device(fake, watch)

        assert seen == [("Lampka", "włączone")]
        assert watch.last_state == "włączone"

    async def test_poll_device_skips_callback_when_state_unchanged(self):
        _FakeSmartThings._status_by_device = {
            "dev-1": {"components": {"main": {"switch": {"switch": {"value": "on"}}}}},
        }

        async def on_state(name, state):
            raise AssertionError("nie powinno się wywołać - stan bez zmian")

        client = st.SmartThingsClient(
            {"personal_access_token": "pat", "devices": {"Lampka": {"device_id": "dev-1"}}},
            on_state,
        )
        watch = client._devices["Lampka"]
        watch.last_state = "włączone"
        fake = _FakeSmartThings()

        await client._poll_device(fake, watch)

    async def test_poll_device_uses_configured_capability_and_attribute(self):
        _FakeSmartThings._status_by_device = {
            "dev-1": {"components": {"main": {"contactSensor": {"contact": {"value": "open"}}}}},
        }
        seen = []

        async def on_state(name, state):
            seen.append((name, state))

        client = st.SmartThingsClient(
            {
                "personal_access_token": "pat",
                "devices": {"Drzwi": {"device_id": "dev-1", "capability": "contactSensor", "attribute": "contact"}},
            },
            on_state,
        )
        watch = client._devices["Drzwi"]
        fake = _FakeSmartThings()

        await client._poll_device(fake, watch)

        assert seen == [("Drzwi", "open")]

    async def test_poll_device_handles_missing_value_gracefully(self):
        _FakeSmartThings._status_by_device = {"dev-1": {"components": {"main": {}}}}

        async def on_state(name, state):
            raise AssertionError("nie powinno się wywołać - brak wartości")

        client = st.SmartThingsClient(
            {"personal_access_token": "pat", "devices": {"Lampka": {"device_id": "dev-1"}}},
            on_state,
        )
        watch = client._devices["Lampka"]
        fake = _FakeSmartThings()

        await client._poll_device(fake, watch)

        assert watch.last_state is None

    async def test_run_forever_skips_when_not_configured(self, caplog):
        async def on_state(name, state):
            raise AssertionError("nie powinno się wywołać - brak urządzeń")

        client = st.SmartThingsClient({"devices": {}}, on_state)
        with caplog.at_level("INFO", logger="bridge.smartthings"):
            await client.run_forever()
        assert "brak skonfigurowanych urządzeń" in caplog.text

    async def test_run_forever_skips_when_missing_credentials(self, caplog):
        async def on_state(name, state):
            raise AssertionError("nie powinno się wywołać - brak poświadczeń")

        client = st.SmartThingsClient({"devices": {"Lampka": {"device_id": "dev-1"}}}, on_state)
        with caplog.at_level("WARNING", logger="bridge.smartthings"):
            await client.run_forever()
        assert "brak tokenu dostępu" in caplog.text
