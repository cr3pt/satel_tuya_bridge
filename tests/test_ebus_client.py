"""Testy dla bridge/ebus_client.py - discover_topics() (podpowiedź
dostępnych pól eBUS w panelu /config), EbusMqttListener.update_sensors()
(przeładowanie listy czujników bez zrywania połączenia MQTT, patrz
RulesEngine.reload_config w bridge/rules_engine.py) oraz
BackgroundTopicDiscovery (nasłuch w tle przez kilka minut, patrz
dashboard/app.py: /api/discover/ebus/start), bez prawdziwego brokera MQTT."""
import time
from types import SimpleNamespace

import pytest

from bridge.ebus_client import discover_topics, EbusMqttListener, BackgroundTopicDiscovery


class FakeMqttClient:
    """Symuluje paho.mqtt.client.Client na tyle, ile potrzebuje
    discover_topics() - connect/subscribe/loop_start/loop_stop/disconnect,
    plus "dostarczenie" skonfigurowanych wiadomości synchronicznie w
    loop_start() (żeby test nie musiał czekać na prawdziwy wątek sieciowy)."""

    def __init__(self, topics_to_deliver=()):
        self.on_message = None
        self.subscribed_topic = None
        self.connected_to = None
        self._topics_to_deliver = list(topics_to_deliver)
        self.stopped = False
        self.disconnected = False

    def connect(self, host, port, keepalive=60):
        self.connected_to = (host, port)

    def subscribe(self, topic):
        self.subscribed_topic = topic

    def loop_start(self):
        for topic in self._topics_to_deliver:
            self.on_message(self, None, SimpleNamespace(topic=topic))

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        self.disconnected = True


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # discover_topics() śpi timeout_seconds - w testach nie ma po co czekać,
    # wiadomości i tak "dostarcza" FakeMqttClient.loop_start() synchronicznie.
    monkeypatch.setattr("bridge.ebus_client.time.sleep", lambda seconds: None)


def test_discover_topics_returns_sorted_unique_topics(monkeypatch):
    fake = FakeMqttClient(["ebusd/bai/FlowTemp", "ebusd/bai/FlowTemp", "ebusd/hwc/Temp"])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

    topics = discover_topics("127.0.0.1", 1883)

    assert topics == ["ebusd/bai/FlowTemp", "ebusd/hwc/Temp"]


def test_discover_topics_subscribes_to_configured_prefix(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

    discover_topics("127.0.0.1", 1883, topic_prefix="custom_prefix")

    assert fake.subscribed_topic == "custom_prefix/#"


def test_discover_topics_connects_to_given_host_and_port(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

    discover_topics("192.168.1.20", 1884)

    assert fake.connected_to == ("192.168.1.20", 1884)


def test_discover_topics_stops_and_disconnects_afterwards(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

    discover_topics("127.0.0.1", 1883)

    assert fake.stopped is True
    assert fake.disconnected is True


def test_discover_topics_no_messages_returns_empty_list(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

    assert discover_topics("127.0.0.1", 1883) == []


def test_discover_topics_raises_clear_error_on_connection_failure(monkeypatch):
    class FailingClient:
        def connect(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: FailingClient())

    with pytest.raises(ConnectionError, match="127.0.0.1"):
        discover_topics("127.0.0.1", 1883)


class FakeMqttClientForListener:
    """Symuluje paho.mqtt.client.Client na tyle, ile potrzebuje
    EbusMqttListener - connect_async/loop_start/loop_stop/disconnect/
    subscribe, plus rejestrowanie WYWOŁAŃ subscribe() (żeby sprawdzić, co
    dokładnie zasubskrybowano po update_sensors())."""

    def __init__(self):
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.subscribed: list[str] = []
        self.connected_async_to = None

    def connect_async(self, host, port):
        self.connected_async_to = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        pass


class TestEbusMqttListenerUpdateSensors:
    """update_sensors() - przeładowanie listy czujników w locie (patrz
    RulesEngine.reload_config), bez tworzenia nowego klienta MQTT ani
    zrywania istniejącego połączenia."""

    async def test_update_sensors_replaces_topic_map(self, monkeypatch):
        fake = FakeMqttClientForListener()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)
        cfg = {"mqtt_host": "127.0.0.1", "sensors": [{"name": "a", "mqtt_topic": "ebusd/a"}]}
        listener = EbusMqttListener(cfg, lambda *a: None)

        new_cfg = {"mqtt_host": "127.0.0.1", "sensors": [{"name": "b", "mqtt_topic": "ebusd/b"}]}
        listener.update_sensors(new_cfg)

        assert listener._topic_to_sensor == {"ebusd/b": {"name": "b", "mqtt_topic": "ebusd/b"}}

    async def test_update_sensors_subscribes_only_to_new_topics(self, monkeypatch):
        fake = FakeMqttClientForListener()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)
        cfg = {"mqtt_host": "127.0.0.1", "sensors": [{"name": "a", "mqtt_topic": "ebusd/a"}]}
        listener = EbusMqttListener(cfg, lambda *a: None)
        fake.subscribed.clear()  # __init__ samo nie subskrybuje - to dzieje się w _on_connect

        new_cfg = {"mqtt_host": "127.0.0.1", "sensors": [
            {"name": "a", "mqtt_topic": "ebusd/a"}, {"name": "b", "mqtt_topic": "ebusd/b"},
        ]}
        listener.update_sensors(new_cfg)

        assert fake.subscribed == ["ebusd/b"]

    async def test_update_sensors_with_no_new_topics_subscribes_nothing(self, monkeypatch):
        fake = FakeMqttClientForListener()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)
        cfg = {"mqtt_host": "127.0.0.1", "sensors": [{"name": "a", "mqtt_topic": "ebusd/a"}]}
        listener = EbusMqttListener(cfg, lambda *a: None)
        fake.subscribed.clear()

        listener.update_sensors({"mqtt_host": "127.0.0.1", "sensors": [{"name": "a", "mqtt_topic": "ebusd/a"}]})

        assert fake.subscribed == []


class TestEbusMqttListenerParsePayload:
    """_on_message()/_parse_payload() - obsługa DWÓCH formatów wiadomości
    ebusd: domyślnego (czysty tekst, jedno pole na temat) i --mqttjson
    (obiekt JSON, ewentualnie z kilkoma polami na temat obwodu). Zgłoszony
    błąd: wartości z trybu --mqttjson wyświetlały się jako nieczytelny
    surowy JSON, bo _on_message próbował tylko float()/zostawiał tekst."""

    def _make_listener(self, monkeypatch, sensors):
        fake = FakeMqttClientForListener()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)
        cfg = {"mqtt_host": "127.0.0.1", "sensors": sensors}
        received = []
        listener = EbusMqttListener(cfg, lambda sensor_cfg, value: received.append((sensor_cfg["name"], value)))
        return listener, received

    def _deliver(self, listener, topic, payload):
        listener._on_message(None, None, SimpleNamespace(topic=topic, payload=payload.encode()))

    # --- Format domyślny ebusd (czysty tekst) - zachowanie sprzed zmiany ---

    async def test_plain_numeric_text_becomes_float(self, monkeypatch):
        listener, received = self._make_listener(monkeypatch, [{"name": "a", "mqtt_topic": "ebusd/a"}])
        self._deliver(listener, "ebusd/a", "22.5")
        assert received == [("a", 22.5)]

    async def test_plain_non_numeric_text_stays_string(self, monkeypatch):
        listener, received = self._make_listener(monkeypatch, [{"name": "a", "mqtt_topic": "ebusd/a"}])
        self._deliver(listener, "ebusd/a", "heating")
        assert received == [("a", "heating")]

    # --- Tryb --mqttjson: pojedyncze pole na temat ---

    async def test_single_field_json_with_value_wrapper_is_unwrapped(self, monkeypatch):
        listener, received = self._make_listener(monkeypatch, [{"name": "a", "mqtt_topic": "ebusd/bai/FlowTemp"}])
        self._deliver(listener, "ebusd/bai/FlowTemp", '{"FlowTemp": {"value": 22.5}}')
        assert received == [("a", 22.5)]

    async def test_single_field_json_bare_scalar_is_used_directly(self, monkeypatch):
        listener, received = self._make_listener(monkeypatch, [{"name": "a", "mqtt_topic": "ebusd/bai/FlowTemp"}])
        self._deliver(listener, "ebusd/bai/FlowTemp", '{"FlowTemp": 22.5}')
        assert received == [("a", 22.5)]

    # --- Tryb --mqttjson: kilka pól na temat obwodu (composite) ---

    async def test_multi_field_json_with_mqtt_field_picks_configured_key(self, monkeypatch):
        listener, received = self._make_listener(
            monkeypatch, [{"name": "a", "mqtt_topic": "ebusd/hc1", "mqtt_field": "TempFloor"}],
        )
        self._deliver(
            listener, "ebusd/hc1",
            '{"MixerStatus": {"value": "on"}, "TempFloor": {"value": 21.3}}',
        )
        assert received == [("a", 21.3)]

    async def test_multi_field_json_without_mqtt_field_is_skipped(self, monkeypatch, caplog):
        listener, received = self._make_listener(monkeypatch, [{"name": "a", "mqtt_topic": "ebusd/hc1"}])
        self._deliver(listener, "ebusd/hc1", '{"MixerStatus": {"value": "on"}, "TempFloor": {"value": 21.3}}')
        assert received == []
        assert "mqtt_field" in caplog.text

    async def test_multi_field_json_with_unknown_mqtt_field_is_skipped(self, monkeypatch, caplog):
        listener, received = self._make_listener(
            monkeypatch, [{"name": "a", "mqtt_topic": "ebusd/hc1", "mqtt_field": "NieIstnieje"}],
        )
        self._deliver(listener, "ebusd/hc1", '{"MixerStatus": {"value": "on"}, "TempFloor": {"value": 21.3}}')
        assert received == []
        assert "NieIstnieje" in caplog.text


class FakeMqttClientBg:
    """Symuluje paho.mqtt.client.Client na tyle, ile potrzebuje
    BackgroundTopicDiscovery - connect (synchroniczne, nie connect_async)/
    subscribe/loop_start/loop_stop/disconnect, plus ręczne 'dostarczanie'
    wiadomości przez wywołanie on_message (żeby test nie musiał czekać na
    prawdziwy wątek sieciowy paho)."""

    def __init__(self):
        self.on_message = None
        self.connected_to = None
        self.subscribed_topic = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False

    def connect(self, host, port, keepalive=60):
        self.connected_to = (host, port)

    def subscribe(self, topic):
        self.subscribed_topic = topic

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True


class TestBackgroundTopicDiscovery:
    """Nasłuch tematów MQTT ebusd W TLE (do kilku minut) - patrz
    dashboard/app.py: /api/discover/ebus/start|status|stop."""

    def test_start_connects_and_subscribes(self, monkeypatch):
        fake = FakeMqttClientBg()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

        discovery = BackgroundTopicDiscovery()
        discovery.start("127.0.0.1", 1883, duration_seconds=10)

        assert fake.connected_to == ("127.0.0.1", 1883)
        assert fake.subscribed_topic == "ebusd/#"
        assert fake.loop_started is True
        discovery.stop()

    def test_status_reports_running_immediately_after_start(self, monkeypatch):
        fake = FakeMqttClientBg()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

        discovery = BackgroundTopicDiscovery()
        discovery.start("127.0.0.1", 1883, duration_seconds=10)

        status = discovery.status()
        assert status["running"] is True
        assert status["topics"] == []
        assert status["duration_seconds"] == 10
        discovery.stop()

    def test_incoming_messages_accumulate_in_status(self, monkeypatch):
        fake = FakeMqttClientBg()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

        discovery = BackgroundTopicDiscovery()
        discovery.start("127.0.0.1", 1883, duration_seconds=10)

        fake.on_message(fake, None, SimpleNamespace(topic="ebusd/bm/FlowTemp"))
        fake.on_message(fake, None, SimpleNamespace(topic="ebusd/fha/FlowTemp"))
        fake.on_message(fake, None, SimpleNamespace(topic="ebusd/bm/FlowTemp"))  # duplikat

        status = discovery.status()
        assert status["topics"] == ["ebusd/bm/FlowTemp", "ebusd/fha/FlowTemp"]
        discovery.stop()

    def test_stop_disconnects_and_marks_not_running(self, monkeypatch):
        fake = FakeMqttClientBg()
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: fake)

        discovery = BackgroundTopicDiscovery()
        discovery.start("127.0.0.1", 1883, duration_seconds=10)
        status = discovery.stop()

        assert fake.loop_stopped is True
        assert fake.disconnected is True
        assert status["running"] is False

    def test_starting_twice_while_running_raises(self, monkeypatch):
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: FakeMqttClientBg())

        discovery = BackgroundTopicDiscovery()
        discovery.start("127.0.0.1", 1883, duration_seconds=10)
        with pytest.raises(RuntimeError):
            discovery.start("127.0.0.1", 1883, duration_seconds=10)
        discovery.stop()

    def test_connection_failure_raises_connection_error(self, monkeypatch):
        class FailingClient:
            def connect(self, *args, **kwargs):
                raise OSError("connection refused")

        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: FailingClient())

        discovery = BackgroundTopicDiscovery()
        with pytest.raises(ConnectionError, match="127.0.0.1"):
            discovery.start("127.0.0.1", 1883, duration_seconds=10)
        # nie powinno zostać zablokowane w stanie "running" po nieudanym connect
        assert discovery.status()["running"] is False

    def test_auto_stops_after_duration_elapses(self, monkeypatch):
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: FakeMqttClientBg())

        discovery = BackgroundTopicDiscovery()
        discovery.start("127.0.0.1", 1883, duration_seconds=0.05)
        assert discovery.status()["running"] is True

        # Duży margines celowo (nie testujemy precyzji timingu, tylko że
        # AUTOMATYCZNIE się zatrzymuje) - żeby nie było migotliwe pod
        # obciążeniem (wiele testów w tym pliku tworzy własne wątki tuż
        # wcześniej).
        deadline = time.time() + 5.0
        while time.time() < deadline and discovery.status()["running"]:
            time.sleep(0.05)
        assert discovery.status()["running"] is False

    def test_new_discovery_clears_previous_topics(self, monkeypatch):
        monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda **kw: FakeMqttClientBg())

        discovery = BackgroundTopicDiscovery()
        discovery.start("127.0.0.1", 1883, duration_seconds=10)
        discovery._topics.add("ebusd/stale/Topic")
        discovery.stop()

        discovery.start("127.0.0.1", 1883, duration_seconds=10)
        assert discovery.status()["topics"] == []
        discovery.stop()
