# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import smtplib

import aiohttp
import pytest

from bridge import notifier as nt
from bridge.notifier import Notifier
from tests.test_plugin_routes import _basic_auth_header, client
from tests.test_rules_engine import CONFIG_TEMPLATE, SENSORS_YAML, engine

AUTH = _basic_auth_header("test_haslo_123")
PO_USER = "u" * 30
PO_TOKEN = "t" * 30


class FakeResp:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)


class FakeSession:
    calls: list = []
    status = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        FakeSession.calls.append((url, kw))
        return FakeResp(FakeSession.status)


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    FakeSession.calls = []
    FakeSession.status = 200
    monkeypatch.setattr(nt.aiohttp, "ClientSession", FakeSession)
    return FakeSession


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.sent, self.log = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(nt.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(nt.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


FULL = {
    "ntfy_topic_url": "https://ntfy.sh/tajny",
    "email": {"enabled": True, "host": "smtp.example.com", "port": 587, "security": "starttls",
              "username": "u@example.com", "password": "haslo", "from": "alarm@example.com",
              "to": ["a@icloud.com", "b@example.com"]},
    "pushover": {"enabled": True, "user_key": PO_USER, "app_token": PO_TOKEN},
    "bark": {"enabled": True, "server": "https://bark.example.com/", "device_key": "abcdef123456"},
    "pushcut": {"enabled": True, "api_key": "pckey", "notification_name": "Alarm dom"},
}


class TestChannels:
    def test_nothing_configured(self):
        assert not Notifier(None).enabled and not Notifier({}).enabled

    def test_all_detected(self):
        assert Notifier(FULL).channels == ["ntfy", "email", "pushover", "bark", "pushcut"]

    def test_disabled_or_incomplete_skipped(self):
        cfg = {**FULL, "email": {**FULL["email"], "enabled": False}, "pushover": {"enabled": True, "user_key": PO_USER},
               "bark": {"enabled": True}}
        assert Notifier(cfg).channels == ["ntfy", "pushcut"]

    async def test_ntfy_payload(self, fake_http):
        await Notifier({"ntfy_topic_url": "https://ntfy.sh/x"}).send("T", "msg", priority="urgent")
        url, kw = fake_http.calls[0]
        assert url == "https://ntfy.sh/x" and kw["headers"]["Priority"] == "urgent" and kw["data"] == b"msg"

    async def test_pushover_urgent_is_emergency(self, fake_http):
        await Notifier({"pushover": FULL["pushover"]}).send("T", "m", priority="urgent")
        url, kw = fake_http.calls[0]
        assert url.endswith("/1/messages.json")
        assert kw["data"]["priority"] == 2 and kw["data"]["retry"] == 60 and kw["data"]["expire"] == 3600
        assert kw["data"]["token"] == PO_TOKEN and kw["data"]["user"] == PO_USER

    async def test_pushover_default_has_no_retry(self, fake_http):
        await Notifier({"pushover": FULL["pushover"]}).send("T", "m")
        assert fake_http.calls[0][1]["data"]["priority"] == 0 and "retry" not in fake_http.calls[0][1]["data"]

    async def test_bark_levels(self, fake_http):
        n = Notifier({"bark": FULL["bark"]})
        await n.send("T", "m", priority="urgent")
        await n.send("T", "m", priority="high")
        await n.send("T", "m")
        urls = {c[0] for c in fake_http.calls}
        assert urls == {"https://bark.example.com/push"}
        assert [c[1]["json"]["level"] for c in fake_http.calls] == ["critical", "timeSensitive", "active"]
        assert fake_http.calls[0][1]["json"]["device_key"] == "abcdef123456"

    async def test_pushcut_url_and_key(self, fake_http):
        await Notifier({"pushcut": FULL["pushcut"]}).send("T", "m", priority="urgent")
        url, kw = fake_http.calls[0]
        assert url == "https://api.pushcut.io/v1/notifications/Alarm%20dom"
        assert kw["headers"]["API-Key"] == "pckey" and kw["json"]["isTimeSensitive"] is True

    async def test_email_starttls_login_and_priority(self, fake_smtp):
        await Notifier({"email": FULL["email"]}).send("🚨 ALARM", "Strefa Dom", priority="urgent")
        smtp = fake_smtp.instances[0]
        assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
        assert smtp.log == ["starttls", ("login", "u@example.com", "haslo")]
        msg = smtp.sent[0]
        assert msg["Subject"] == "🚨 ALARM" and msg["To"] == "a@icloud.com, b@example.com"
        assert msg["X-Priority"] == "1" and "Strefa Dom" in msg.get_content()

    async def test_email_ssl_default_port_and_no_login(self, fake_smtp):
        cfg = {"email": {**FULL["email"], "security": "ssl", "port": None, "username": ""}}
        await Notifier(cfg).send("T", "m")
        smtp = fake_smtp.instances[0]
        assert smtp.port == 465 and smtp.log == []

    async def test_failure_of_one_channel_does_not_block_others(self, fake_http, fake_smtp, monkeypatch):
        def boom(*a, **k):
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        monkeypatch.setattr(nt, "_smtp_send_blocking", boom)
        results = await Notifier(FULL).send_detailed("T", "m", priority="high")
        assert results["email"].startswith("SMTPAuthenticationError")
        assert results["ntfy"] is None and results["pushover"] is None and results["bark"] is None
        assert len(fake_http.calls) == 4

    async def test_http_error_reported_without_secrets(self, fake_http):
        fake_http.status = 401
        results = await Notifier({"pushover": FULL["pushover"]}).send_detailed("T", "m")
        assert results == {"pushover": "HTTP 401"}
        assert PO_TOKEN not in str(results)

    async def test_send_noop_without_channels(self, fake_http):
        await Notifier(None).send("T", "m")
        assert fake_http.calls == []


class TestValidation:
    def test_keeps_existing_secrets_when_empty(self):
        form = {"pushover": {"enabled": True, "user_key": "", "app_token": ""},
                "bark": {"enabled": True, "device_key": ""}, "email": {**FULL["email"], "password": "", "to": "a@b.pl"},
                "pushcut": {"enabled": True, "api_key": "", "notification_name": "Alarm"}}
        clean, errors = nt.validate_channels(form, FULL)
        assert errors == []
        assert clean["pushover"]["user_key"] == PO_USER and clean["pushover"]["app_token"] == PO_TOKEN
        assert clean["bark"]["device_key"] == "abcdef123456" and clean["email"]["password"] == "haslo"
        assert clean["pushcut"]["api_key"] == "pckey" and clean["email"]["to"] == ["a@b.pl"]

    @pytest.mark.parametrize("form, fragment", [
        ({"email": {"enabled": True, "host": "bad host", "from": "a@b.pl", "to": "c@d.pl"}}, "host"),
        ({"email": {"enabled": True, "host": "h.pl", "from": "zly", "to": "c@d.pl"}}, "nadawcy"),
        ({"email": {"enabled": True, "host": "h.pl", "from": "a@b.pl", "to": ""}}, "odbiorcę"),
        ({"email": {"enabled": False, "port": 99999}}, "port"),
        ({"email": {"enabled": False, "security": "x"}}, "szyfrowanie"),
        ({"pushover": {"enabled": True, "user_key": "krotki", "app_token": "krotki"}}, "30 znaków"),
        ({"bark": {"enabled": True, "device_key": "x y"}}, "klucz"),
        ({"bark": {"enabled": False, "server": "ftp://x"}}, "http"),
        ({"pushcut": {"enabled": True, "api_key": "k", "notification_name": "../x"}}, "nazwa"),
    ])
    def test_rejects(self, form, fragment):
        _clean, errors = nt.validate_channels(form, {})
        assert any(fragment in e for e in errors), errors

    def test_public_view_has_no_secrets(self):
        text = str(nt.public_view(FULL))
        for secret in ("haslo", PO_USER, PO_TOKEN, "abcdef123456", "pckey"):
            assert secret not in text
        view = nt.public_view(FULL)
        assert view["email"]["has_password"] and view["pushover"]["has_app_token"] and view["bark"]["has_device_key"]


class TestRoutes:
    async def test_requires_login(self, client):
        assert (await client.get("/api/plugins/notifications/channels")).status_code == 401
        assert (await client.post("/api/plugins/notifications/channels", json={})).status_code == 401
        assert (await client.post("/api/plugins/notifications/test", json={})).status_code == 401

    async def test_save_roundtrip_never_returns_secrets_and_keeps_them(self, client):
        import dashboard.app as app_module
        body = {"satel_alarm": False, "pushover": {"enabled": True, "user_key": PO_USER, "app_token": PO_TOKEN},
                "email": {**FULL["email"], "to": "a@icloud.com"}}
        resp = await client.post("/api/plugins/notifications/channels", json=body, headers=AUTH)
        assert resp.status_code == 200 and (await resp.get_json())["channels"] == ["email", "pushover"]
        text = await (await client.get("/api/plugins/notifications/channels", headers=AUTH)).get_data(as_text=True)
        assert PO_USER not in text and PO_TOKEN not in text and "haslo" not in text
        again = {"satel_alarm": False, "pushover": {"enabled": True, "user_key": "", "app_token": ""}}
        assert (await client.post("/api/plugins/notifications/channels", json=again, headers=AUTH)).status_code == 200
        saved = app_module.config_store.get("config")["notifications"]
        assert saved["pushover"]["app_token"] == PO_TOKEN and saved["satel_alarm"] is False
        assert saved["email"]["password"] == "haslo"

    async def test_invalid_rejected_and_not_saved(self, client):
        import dashboard.app as app_module
        resp = await client.post("/api/plugins/notifications/channels", headers=AUTH,
                                 json={"pushover": {"enabled": True, "user_key": "x", "app_token": "y"}})
        assert resp.status_code == 400 and (await resp.get_json())["details"]
        assert "pushover" not in app_module.config_store.get("config").get("notifications", {})

    async def test_test_endpoint(self, client, fake_http):
        resp = await client.post("/api/plugins/notifications/test", json={}, headers=AUTH)
        assert resp.status_code == 400
        await client.post("/api/plugins/notifications/channels", headers=AUTH,
                          json={"bark": {"enabled": True, "device_key": "abcdef123456"}})
        resp = await client.post("/api/plugins/notifications/test", json={"priority": "urgent"}, headers=AUTH)
        data = await resp.get_json()
        assert resp.status_code == 200 and data["ok"] and data["results"] == {"bark": None}
        assert fake_http.calls[-1][1]["json"]["level"] == "critical"

    async def test_page_loads(self, client):
        html = await (await client.get("/plugins/notifications", headers=AUTH)).get_data(as_text=True)
        assert "saveChannelsBtn" in html


class TestSatelAlarmTrigger:
    @pytest.fixture
    def sent(self, engine, monkeypatch):
        calls = []

        async def fake_send(title, message, priority="default"):
            calls.append((title, message, priority))
        engine.notifier = type("N", (), {"enabled": True, "send": staticmethod(fake_send)})()
        engine.config.satel["partitions"] = {"Dom": 1, "Garaż": 2}
        return calls

    async def test_alarm_edge_notifies_once_with_urgent_priority(self, engine, sent):
        engine.satel.triggered_partition_numbers = {1}
        engine.history.set_status("satel_input:5", "satel_input", "naruszone", label="Salon")
        engine.history.set_status("satel_input:6", "satel_input", "OK", label="Kuchnia")
        await engine._handle_alarm_status_change()
        await engine._handle_alarm_status_change()
        assert len(sent) == 1
        title, text, priority = sent[0]
        assert priority == "urgent" and "Dom" in text and "Salon" in text and "Kuchnia" not in text
        assert any(e["type"] == "alarm" for e in engine.history.get_events("alarm", 0, 9e12))

    async def test_clear_notifies_with_default_priority(self, engine, sent):
        engine.satel.triggered_partition_numbers = {2}
        await engine._handle_alarm_status_change()
        engine.satel.triggered_partition_numbers = set()
        await engine._handle_alarm_status_change()
        assert [c[2] for c in sent] == ["urgent", "default"] and "Garaż" in sent[1][1]

    async def test_disabled_flag_keeps_event_but_no_push(self, engine, sent):
        engine._satel_alarm_notify = False
        engine.satel.triggered_partition_numbers = {1}
        await engine._handle_alarm_status_change()
        assert sent == [] and engine.history.get_events("alarm", 0, 9e12)

    async def test_second_partition_triggers_separately(self, engine, sent):
        engine.satel.triggered_partition_numbers = {1}
        await engine._handle_alarm_status_change()
        engine.satel.triggered_partition_numbers = {1, 2}
        await engine._handle_alarm_status_change()
        assert len(sent) == 2 and "Garaż" in sent[1][1] and "Dom" not in sent[1][1]
