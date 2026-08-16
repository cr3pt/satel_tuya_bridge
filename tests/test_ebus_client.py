"""Testy dla bridge/ebus_client.py - głównie discover_topics() (podpowiedź
dostępnych pól eBUS w panelu /config), bez prawdziwego brokera MQTT."""
from types import SimpleNamespace

import pytest

from bridge.ebus_client import discover_topics


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
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda: fake)

    topics = discover_topics("127.0.0.1", 1883)

    assert topics == ["ebusd/bai/FlowTemp", "ebusd/hwc/Temp"]


def test_discover_topics_subscribes_to_configured_prefix(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda: fake)

    discover_topics("127.0.0.1", 1883, topic_prefix="custom_prefix")

    assert fake.subscribed_topic == "custom_prefix/#"


def test_discover_topics_connects_to_given_host_and_port(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda: fake)

    discover_topics("192.168.1.20", 1884)

    assert fake.connected_to == ("192.168.1.20", 1884)


def test_discover_topics_stops_and_disconnects_afterwards(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda: fake)

    discover_topics("127.0.0.1", 1883)

    assert fake.stopped is True
    assert fake.disconnected is True


def test_discover_topics_no_messages_returns_empty_list(monkeypatch):
    fake = FakeMqttClient([])
    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda: fake)

    assert discover_topics("127.0.0.1", 1883) == []


def test_discover_topics_raises_clear_error_on_connection_failure(monkeypatch):
    class FailingClient:
        def connect(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr("bridge.ebus_client.mqtt.Client", lambda: FailingClient())

    with pytest.raises(ConnectionError, match="127.0.0.1"):
        discover_topics("127.0.0.1", 1883)
