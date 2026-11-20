# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio

import pytest
from aioesphomeapi import (
    APIConnectionError,
    BinarySensorState,
    ButtonInfo,
    LightInfo,
    SensorInfo,
    SensorState,
    SwitchInfo,
    SwitchState,
)

from bridge import esphome_client as esphome


class _FakeReconnectLogic:
    instances = []

    def __init__(self, *, client, on_connect, on_disconnect, on_connect_error=None, name=None):
        self.client = client
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_connect_error = on_connect_error
        self.name = name
        self.started = False
        self.stopped = False
        _FakeReconnectLogic.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeAPIClient:
    instances = []
    entities_to_return: list = []
    list_entities_error: Exception | None = None

    def __init__(self, address, port, password=None, *, noise_psk=None):
        self.address = address
        self.port = port
        self.password = password
        self.noise_psk = noise_psk
        self.disconnected = False
        self.subscribed_cb = None
        self.switch_calls = []
        self.light_calls = []
        self.button_calls = []
        _FakeAPIClient.instances.append(self)

    async def list_entities_services(self):
        if _FakeAPIClient.list_entities_error is not None:
            raise _FakeAPIClient.list_entities_error
        return list(_FakeAPIClient.entities_to_return), []

    def subscribe_states(self, on_state):
        self.subscribed_cb = on_state

    def switch_command(self, key, state, device_id=0):
        self.switch_calls.append((key, state))

    def light_command(self, key, state=None, brightness=None, device_id=0, **_kw):
        self.light_calls.append((key, state, brightness))

    def button_command(self, key, device_id=0):
        self.button_calls.append(key)

    async def disconnect(self, force=False):
        self.disconnected = True


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch):
    _FakeReconnectLogic.instances = []
    _FakeAPIClient.instances = []
    _FakeAPIClient.entities_to_return = []
    _FakeAPIClient.list_entities_error = None
    monkeypatch.setattr(esphome, "ReconnectLogic", _FakeReconnectLogic)
    monkeypatch.setattr(esphome, "APIClient", _FakeAPIClient)
    yield


def _make_client(cfg=None, on_state=None, on_entities=None):
    cfg = {"name": "gama350", "host": "192.168.1.50", **(cfg or {})}

    async def _default_on_state(*_a, **_kw):
        pass

    return esphome.EsphomeDeviceClient(cfg, on_state or _default_on_state, on_entities)


class TestNormalizeDevice:

    def test_defaults(self):
        d = esphome._normalize_device("gama350", {"host": "192.168.1.50"})
        assert d.port == esphome._DEFAULT_PORT
        assert d.label == "gama350"
        assert d.noise_psk is None
        assert d.password is None

    def test_api_key_alias_maps_to_noise_psk(self):
        d = esphome._normalize_device("x", {"host": "1.2.3.4", "api_key": "abc="})
        assert d.noise_psk == "abc="

    def test_explicit_label_kept(self):
        d = esphome._normalize_device("x", {"host": "1.2.3.4", "label": "Licznik kuchnia"})
        assert d.label == "Licznik kuchnia"


class TestEsphomeDeviceClientConnect:

    async def test_on_connect_populates_entities_calls_on_entities_and_reports_health(self):
        switch = SwitchInfo(object_id="relay", key=1, name="Przekaźnik")
        _FakeAPIClient.entities_to_return = [switch]
        seen_entities = []

        async def on_entities(name, entities):
            seen_entities.append((name, list(entities)))

        client = _make_client(on_entities=on_entities)
        await client._on_connect()

        assert client.connected is True
        assert client.entities == [switch]
        assert seen_entities == [("gama350", [switch])]
        assert client._client.subscribed_cb is not None
        assert client._health._last_connected is True

    async def test_on_connect_propagates_list_entities_error(self):
        _FakeAPIClient.list_entities_error = APIConnectionError("boom")
        client = _make_client()
        with pytest.raises(APIConnectionError):
            await client._on_connect()
        assert client.connected is False

    async def test_on_disconnect_reports_unhealthy(self):
        client = _make_client()
        await client._on_connect()
        await client._on_disconnect(expected=False)
        assert client.connected is False
        assert client._health._last_connected is False

    async def test_on_connect_error_reports_unhealthy(self):
        client = _make_client()
        await client._on_connect_error(RuntimeError("no route to host"))
        assert client.connected is False
        assert client._health._last_connected is False

    async def test_start_delegates_to_reconnect_logic(self):
        client = _make_client()
        await client.start()
        assert _FakeReconnectLogic.instances[0].started is True

    async def test_stop_delegates_to_reconnect_logic_and_disconnects(self):
        client = _make_client()
        await client.stop()
        assert _FakeReconnectLogic.instances[0].stopped is True
        assert client._client.disconnected is True


class TestEsphomeStopClearsHealth:
    async def test_intentional_stop_removes_health_status_and_ignores_expected_disconnect(self, history):
        client = esphome.EsphomeDeviceClient(
            {"name": "gama350", "host": "192.168.1.50"}, lambda *a, **k: None, history=history)
        await client._on_connect()
        assert history.get_status_value("health:esphome_gama350") == "połączono"
        await client.stop()
        await client._on_disconnect(expected=True)
        assert history.get_status_value("health:esphome_gama350") is None

    async def test_unexpected_disconnect_still_reported(self, history):
        client = esphome.EsphomeDeviceClient(
            {"name": "gama350", "host": "192.168.1.50"}, lambda *a, **k: None, history=history)
        await client._on_disconnect(expected=False)
        assert history.get_status_value("health:esphome_gama350").startswith("rozłączono")


class TestPruneEsphomeHealth:
    def _engine(self, history, devices):
        from types import SimpleNamespace
        manager = SimpleNamespace(devices=devices) if devices is not None else None
        return SimpleNamespace(history=history, esphome_manager=manager)

    def test_disabled_plugin_removes_stale_esphome_status_only(self, history):
        from bridge.rules_engine import RulesEngine
        history.set_status("health:esphome_gama350", "system_health", "rozłączono: rozłączono (oczekiwane)")
        history.set_status("health:satel", "system_health", "połączono")
        RulesEngine._prune_esphome_health(self._engine(history, None))
        assert history.get_status_value("health:esphome_gama350") is None
        assert history.get_status_value("health:satel") == "połączono"

    def test_active_device_status_kept(self, history):
        from bridge.rules_engine import RulesEngine
        history.set_status("health:esphome_gama350", "system_health", "połączono")
        history.set_status("health:esphome_stary", "system_health", "rozłączono")
        RulesEngine._prune_esphome_health(self._engine(history, {"gama350": object()}))
        assert history.get_status_value("health:esphome_gama350") == "połączono"
        assert history.get_status_value("health:esphome_stary") is None


class TestEsphomeDeviceClientStateDispatch:

    async def test_known_entity_dispatches_value_to_on_state(self):
        sensor = SensorInfo(object_id="power", key=5, name="Moc")
        seen = []

        async def on_state(device, entity, value):
            seen.append((device, entity.object_id, value))

        client = _make_client(on_state=on_state)
        client._entities_by_key = {5: sensor}

        client._on_state_change(SensorState(key=5, state=1234.5))
        await asyncio.sleep(0)

        assert seen == [("gama350", "power", 1234.5)]

    async def test_unknown_key_is_ignored(self):
        calls = []

        async def on_state(*a):
            calls.append(a)

        client = _make_client(on_state=on_state)
        client._on_state_change(SensorState(key=999, state=1.0))
        await asyncio.sleep(0)
        assert calls == []

    async def test_missing_state_flag_is_ignored(self):
        sensor = SensorInfo(object_id="power", key=5, name="Moc")
        calls = []

        async def on_state(*a):
            calls.append(a)

        client = _make_client(on_state=on_state)
        client._entities_by_key = {5: sensor}
        client._on_state_change(SensorState(key=5, state=0.0, missing_state=True))
        await asyncio.sleep(0)
        assert calls == []

    async def test_binary_sensor_bool_state_dispatched(self):
        from aioesphomeapi import BinarySensorInfo

        info = BinarySensorInfo(object_id="ruch", key=9, name="Ruch")
        seen = []

        async def on_state(device, entity, value):
            seen.append(value)

        client = _make_client(on_state=on_state)
        client._entities_by_key = {9: info}
        client._on_state_change(BinarySensorState(key=9, state=True))
        await asyncio.sleep(0)
        assert seen == [True]


class TestEsphomeDeviceClientControl:

    async def test_switch_set_sends_command_when_connected(self):
        switch = SwitchInfo(object_id="relay", key=1, name="Przekaźnik")
        client = _make_client()
        client._entities_by_key = {1: switch}
        client._connected = True

        client.switch_set("relay", True)

        assert client._client.switch_calls == [(1, True)]

    async def test_switch_set_raises_when_not_connected(self):
        switch = SwitchInfo(object_id="relay", key=1, name="Przekaźnik")
        client = _make_client()
        client._entities_by_key = {1: switch}

        with pytest.raises(esphome.EsphomeError, match="połączone"):
            client.switch_set("relay", True)

    async def test_switch_set_raises_for_unknown_object_id(self):
        client = _make_client()
        client._connected = True
        with pytest.raises(esphome.EsphomeError, match="relay"):
            client.switch_set("relay", True)

    async def test_switch_set_raises_for_wrong_entity_type(self):
        light = LightInfo(object_id="lamp", key=2, name="Lampa")
        client = _make_client()
        client._entities_by_key = {2: light}
        client._connected = True
        with pytest.raises(esphome.EsphomeError, match="przełącznika"):
            client.switch_set("lamp", True)

    async def test_light_set_sends_state_and_brightness(self):
        light = LightInfo(object_id="lamp", key=2, name="Lampa")
        client = _make_client()
        client._entities_by_key = {2: light}
        client._connected = True

        client.light_set("lamp", True, brightness=0.8)

        assert client._client.light_calls == [(2, True, 0.8)]

    async def test_button_press_sends_command(self):
        button = ButtonInfo(object_id="restart", key=3, name="Restart")
        client = _make_client()
        client._entities_by_key = {3: button}
        client._connected = True

        client.button_press("restart")

        assert client._client.button_calls == [3]

    async def test_button_press_raises_for_wrong_entity_type(self):
        switch = SwitchInfo(object_id="relay", key=1, name="Przekaźnik")
        client = _make_client()
        client._entities_by_key = {1: switch}
        client._connected = True
        with pytest.raises(esphome.EsphomeError, match="przycisku"):
            client.button_press("relay")


class TestEsphomeManager:

    def test_skips_devices_without_host_or_disabled(self):
        manager = esphome.EsphomeManager(
            {"devices": {
                "a": {"host": "1.2.3.4"},
                "b": {"host": ""},
                "c": {"host": "5.6.7.8", "enabled": False},
            }},
            on_state=lambda *a: None,
        )
        assert set(manager.devices) == {"a"}

    def test_get_device_returns_none_for_unknown_name(self):
        manager = esphome.EsphomeManager({"devices": {}}, on_state=lambda *a: None)
        assert manager.get_device("nope") is None

    async def test_run_forever_is_a_noop_without_devices(self, caplog):
        manager = esphome.EsphomeManager({"devices": {}}, on_state=lambda *a: None)
        with caplog.at_level("INFO", logger="bridge.esphome"):
            await manager.run_forever()
        assert "brak skonfigurowanych urządzeń" in caplog.text

    async def test_run_forever_starts_all_devices_and_stops_them_on_cancel(self):
        async def on_state(*_a):
            pass

        manager = esphome.EsphomeManager(
            {"devices": {"a": {"host": "1.2.3.4"}, "b": {"host": "5.6.7.8"}}}, on_state,
        )
        task = asyncio.create_task(manager.run_forever())
        await asyncio.sleep(0.01)
        assert len(_FakeReconnectLogic.instances) == 2
        assert all(rl.started for rl in _FakeReconnectLogic.instances)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(rl.stopped for rl in _FakeReconnectLogic.instances)
        assert all(c.disconnected for c in _FakeAPIClient.instances)
