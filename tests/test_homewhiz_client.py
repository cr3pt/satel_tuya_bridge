# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import hashlib
import hmac
import json

import pytest

from bridge import homewhiz_client as hw

_AWS_ACCESS_KEY = "AKIDEXAMPLE"
_AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_AWS_REGION = "us-east-1"
_AWS_SERVICE = "service"
_AWS_HOST = "example.amazonaws.com"


class _FixedNow:
    pass


@pytest.fixture(autouse=True)
def _fixed_amz_dates(monkeypatch):
    monkeypatch.setattr(hw, "_amz_dates", lambda now=None: ("20150830T123600Z", "20150830"))


class TestSigV4HeaderSigning:

    def test_matches_official_get_vanilla_signature(self):
        headers = hw._sigv4_headers(
            method="GET", host=_AWS_HOST, path="/", query="",
            region=_AWS_REGION, service=_AWS_SERVICE,
            access_key=_AWS_ACCESS_KEY, secret_key=_AWS_SECRET_KEY, session_token=None,
        )
        assert headers["Authorization"] == (
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
        )
        assert headers["X-Amz-Date"] == "20150830T123600Z"
        assert "X-Amz-Security-Token" not in headers
        assert "host" not in headers

    def test_includes_security_token_when_session_based(self):
        headers = hw._sigv4_headers(
            method="GET", host=_AWS_HOST, path="/", query="",
            region=_AWS_REGION, service=_AWS_SERVICE,
            access_key=_AWS_ACCESS_KEY, secret_key=_AWS_SECRET_KEY, session_token="tok123",
        )
        assert headers["X-Amz-Security-Token"] == "tok123"
        assert "SignedHeaders=host;x-amz-date;x-amz-security-token" in headers["Authorization"]


class TestCanonicalQueryString:

    def test_sorts_duplicate_keys_by_value(self):
        assert hw._canonical_query_string("Param1=value2&Param1=Value1") == "Param1=Value1&Param1=value2"

    def test_matches_official_get_vanilla_query_order_key_signature(self):
        headers = hw._sigv4_headers(
            method="GET", host=_AWS_HOST, path="/", query="Param1=value2&Param1=Value1",
            region=_AWS_REGION, service=_AWS_SERVICE,
            access_key=_AWS_ACCESS_KEY, secret_key=_AWS_SECRET_KEY, session_token=None,
        )
        assert headers["Authorization"] == (
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=eedbc4e291e521cf13422ffca22be7d2eb8146eecf653089df300a15b2382bd1"
        )


class TestPresignIotWebsocketUrl:

    def test_url_shape_and_required_params(self):
        url = hw.presign_iot_websocket_url(
            endpoint="abc123-ats.iot.eu-west-1.amazonaws.com", region="eu-west-1",
            access_key="AKID", secret_key="SECRET", session_token="SESSIONTOK",
        )
        assert url.startswith("wss://abc123-ats.iot.eu-west-1.amazonaws.com:443/mqtt?")
        for param in ("X-Amz-Algorithm=AWS4-HMAC-SHA256", "X-Amz-Credential=", "X-Amz-Date=",
                      "X-Amz-Expires=3600", "X-Amz-SignedHeaders=host", "X-Amz-Signature="):
            assert param in url

    def test_security_token_appears_after_signature(self):
        url = hw.presign_iot_websocket_url(
            endpoint="abc.iot.eu-west-1.amazonaws.com", region="eu-west-1",
            access_key="AKID", secret_key="SECRET", session_token="SESSIONTOK",
        )
        assert url.index("X-Amz-Signature=") < url.index("X-Amz-Security-Token=")

    def test_security_token_not_part_of_signed_query(self):
        kwargs = dict(endpoint="abc.iot.eu-west-1.amazonaws.com", region="eu-west-1",
                      access_key="AKID", secret_key="SECRET")
        url_a = hw.presign_iot_websocket_url(session_token="TOKEN-A", **kwargs)
        url_b = hw.presign_iot_websocket_url(session_token="TOKEN-B", **kwargs)
        sig_a = url_a.split("X-Amz-Signature=")[1].split("&")[0]
        sig_b = url_b.split("X-Amz-Signature=")[1].split("&")[0]
        assert sig_a == sig_b

    def test_matches_independently_recomputed_signature(self):
        access_key, secret_key, session_token = "AKID", "SECRET", "SESSIONTOK"
        endpoint, region = "abc.iot.eu-west-1.amazonaws.com", "eu-west-1"
        url = hw.presign_iot_websocket_url(
            endpoint=endpoint, region=region, access_key=access_key,
            secret_key=secret_key, session_token=session_token, expires_seconds=3600,
        )

        amz_date, date_stamp = "20150830T123600Z", "20150830"
        credential_scope = f"{date_stamp}/{region}/iotdevicegateway/aws4_request"
        canonical_querystring = (
            "X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential={access_key}%2F{credential_scope.replace('/', '%2F')}"
            f"&X-Amz-Date={amz_date}&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
        )
        canonical_request = "\n".join([
            "GET", "/mqtt", canonical_querystring, f"host:{endpoint}\n", "host",
            hashlib.sha256(b"").hexdigest(),
        ])
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        expected_signature = hmac.new(
            hw._signing_key(secret_key, date_stamp, region, "iotdevicegateway"),
            string_to_sign.encode(), hashlib.sha256,
        ).hexdigest()

        assert f"X-Amz-Signature={expected_signature}" in url


class TestByteDecoding:
    def test_clamp_byte_passthrough_below_128(self):
        assert hw.clamp_byte(0) == 0
        assert hw.clamp_byte(127) == 127

    def test_clamp_byte_wraps_above_127(self):
        assert hw.clamp_byte(128) == 0
        assert hw.clamp_byte(133) == 5
        assert hw.clamp_byte(255) == 127

    def test_safe_get_clamps_in_range_value(self):
        assert hw.safe_get(bytearray([9, 133]), 1) == 5

    def test_safe_get_out_of_range_returns_zero(self):
        assert hw.safe_get(bytearray([1, 2]), 5) == 0
        assert hw.safe_get(bytearray([1, 2]), -1) == 0

    def test_decode_wifi_array_pads_offset_with_zeros(self):
        result = hw.decode_wifi_array(3, [9, 8, 7])
        assert result == bytearray([0, 0, 0, 9, 8, 7])


class TestFriendlyName:
    def test_strips_dishwasher_state_prefix(self):
        assert hw.to_friendly_name("Dishwasher_State_Running") == "running"

    def test_strips_dishwasher_prefix(self):
        assert hw.to_friendly_name("Dishwasher_Finished") == "finished"

    def test_no_prefix_still_lowercased(self):
        assert hw.to_friendly_name("Idle") == "idle"

    def test_empty_after_stripping_falls_back_to_lowercased_original(self):
        assert hw.to_friendly_name("State_") == "state_".lower()


class TestDecodeState:
    CONFIG = {
        "deviceStates": {
            "wifiArrayReadIndex": 2,
            "states": [
                {"strKey": "Dishwasher_State_Idle", "wifiArrayValue": 0},
                {"strKey": "Dishwasher_State_Running", "wifiArrayValue": 1},
                {"strKey": "Dishwasher_State_Finished", "wifiArrayValue": 2},
            ],
        },
    }

    def test_decodes_matching_state(self):
        assert hw.decode_state(self.CONFIG, 0, [9, 9, 1]) == "running"

    def test_decodes_with_nonzero_offset(self):
        assert hw.decode_state(self.CONFIG, 2, [2]) == "finished"

    def test_unmatched_value_returns_none(self):
        assert hw.decode_state(self.CONFIG, 0, [9, 9, 99]) is None

    def test_missing_read_index_returns_none(self):
        assert hw.decode_state({"deviceStates": {"states": []}}, 0, [1]) is None

    def test_empty_descriptor_returns_none(self):
        assert hw.decode_state({}, 0, [1]) is None

    def test_clamps_wrapped_value_before_matching(self):
        assert hw.decode_state(self.CONFIG, 0, [9, 9, 129]) == "running"


class TestListPossibleStates:
    def test_lists_all_states_in_order(self):
        config = {
            "deviceStates": {
                "wifiArrayReadIndex": 2,
                "states": [
                    {"strKey": "Dishwasher_State_Idle", "wifiArrayValue": 0},
                    {"strKey": "Dishwasher_State_Running", "wifiArrayValue": 1},
                    {"strKey": "Dishwasher_State_Finished", "wifiArrayValue": 2},
                ],
            },
        }
        assert hw.list_possible_states(config) == ["idle", "running", "finished"]

    def test_deduplicates_names(self):
        config = {
            "deviceStates": {
                "states": [
                    {"strKey": "Dishwasher_State_Idle", "wifiArrayValue": 0},
                    {"strKey": "State_Idle", "wifiArrayValue": 5},
                ],
            },
        }
        assert hw.list_possible_states(config) == ["idle"]

    def test_empty_descriptor_returns_empty_list(self):
        assert hw.list_possible_states({}) == []

    def test_missing_states_returns_empty_list(self):
        assert hw.list_possible_states({"deviceStates": {}}) == []


class _FakeResponse:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json_data = json_data

    async def json(self, content_type=None):
        return self._json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:

    def __init__(self, queue: list, calls: list):
        self._queue = queue
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self._calls.append(("GET", url, kwargs))
        return self._queue.pop(0)

    def post(self, url, **kwargs):
        self._calls.append(("POST", url, kwargs))
        return self._queue.pop(0)


def _patch_session(monkeypatch, responses: list):
    calls: list = []
    monkeypatch.setattr(hw.aiohttp, "ClientSession", lambda *a, **kw: _FakeSession(responses, calls))
    return calls


class TestUnwrapEnvelope:

    def test_extracts_data(self):
        assert hw._unwrap_envelope({"success": True, "data": {"x": 1}}, context="t") == {"x": 1}

    def test_data_can_be_a_list(self):
        assert hw._unwrap_envelope({"success": True, "data": [1, 2]}, context="t") == [1, 2]

    def test_success_false_raises_despite_http_200(self):
        with pytest.raises(hw.HomeWhizError):
            hw._unwrap_envelope({"success": False, "data": {}}, context="t")

    def test_missing_data_key_raises(self):
        with pytest.raises(hw.HomeWhizError):
            hw._unwrap_envelope({"success": True}, context="t")

    def test_non_dict_body_raises(self):
        with pytest.raises(hw.HomeWhizError):
            hw._unwrap_envelope("not a dict", context="t")


class TestLogin:
    async def test_successful_login_returns_session(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(200, {
            "success": True,
            "data": {"credentials": {
                "accessKey": "AK", "secretKey": "SK", "sessionToken": "ST", "expiration": 1735689600000,
            }},
        })])
        session = await hw.login("user@example.com", "pw")
        assert session.access_key == "AK"
        assert session.secret_key == "SK"
        assert session.session_token == "ST"
        assert session.expiration_ms == 1735689600000

    async def test_bad_credentials_raises_homewhiz_error(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(401, {})])
        with pytest.raises(hw.HomeWhizError):
            await hw.login("user@example.com", "wrong")

    async def test_success_false_with_http_200_raises(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(200, {"success": False, "message": "invalid credentials"})])
        with pytest.raises(hw.HomeWhizError):
            await hw.login("user@example.com", "wrong")

    async def test_malformed_response_raises_homewhiz_error(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(200, {
            "success": True, "data": {"credentials": {"accessKey": "AK"}},
        })])
        with pytest.raises(hw.HomeWhizError):
            await hw.login("user@example.com", "pw")

    async def test_missing_credentials_key_raises(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(200, {"success": True, "data": {}})])
        with pytest.raises(hw.HomeWhizError):
            await hw.login("user@example.com", "pw")


class TestListAppliances:

    async def test_flattens_appliances_across_homes(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [
            _FakeResponse(200, {"success": True, "data": [{"id": "home1"}, {"id": "home2"}]}),
            _FakeResponse(200, {"success": True, "data": {"appliances": [
                {"applianceId": "a1", "applianceType": 3, "connectivity": "WIFI", "name": "Zmywarka"},
            ]}}),
            _FakeResponse(200, {"success": True, "data": {"appliances": [
                {"applianceId": "a2", "applianceType": 1, "connectivity": "WIFI", "name": "Pralka"},
            ]}}),
        ])
        appliances = await hw.list_appliances(session)
        assert {a["applianceId"] for a in appliances} == {"a1", "a2"}

    async def test_excludes_bluetooth_only_appliances(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [
            _FakeResponse(200, {"success": True, "data": [{"id": "home1"}]}),
            _FakeResponse(200, {"success": True, "data": {"appliances": [
                {"applianceId": "a1", "connectivity": "WIFI"},
                {"applianceId": "a2", "connectivity": "BT"},
                {"applianceId": "a3", "connectivity": "BASICBT"},
            ]}}),
        ])
        appliances = await hw.list_appliances(session)
        assert {a["applianceId"] for a in appliances} == {"a1"}

    async def test_skips_entries_without_appliance_id(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [
            _FakeResponse(200, {"success": True, "data": [{"id": "home1"}]}),
            _FakeResponse(200, {"success": True, "data": {"appliances": [{"connectivity": "WIFI"}]}}),
        ])
        assert await hw.list_appliances(session) == []

    def test_is_dishwasher(self):
        assert hw.is_dishwasher({"applianceType": 3}) is True
        assert hw.is_dishwasher({"applianceType": 1}) is False


class TestFetchConfigDescriptor:

    async def test_fetches_descriptor_from_s3(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        expected = {"deviceStates": {"wifiArrayReadIndex": 0, "states": []}}
        calls = _patch_session(monkeypatch, [
            _FakeResponse(200, {"success": True, "data": {"results": [{"cid": "cfg123", "ver": "2", "lang": "en"}]}}),
            _FakeResponse(200, expected),
        ])
        result = await hw.fetch_config_descriptor(session, "appliance1", language="en")
        assert result == expected
        assert calls[1][1] == "https://s3-eu-west-1.amazonaws.com/procam-contents/CONFIGURATIONS/cfg123/v2/cfg123.en.json"

    async def test_empty_entries_raises_homewhiz_error(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [_FakeResponse(200, {"success": True, "data": {"results": []}})])
        with pytest.raises(hw.HomeWhizError):
            await hw.fetch_config_descriptor(session, "appliance1")


class TestHomeWhizClientMessageHandling:

    def _make_client(self):
        calls = []

        async def on_state(name, state):
            calls.append((name, state))

        cfg = {"username": "u", "password": "p", "appliances": {
            "Zmywarka": {"appliance_id": "abc123"},
        }}
        client = hw.HomeWhizClient(cfg, on_state)
        client._appliances["Zmywarka"].config_descriptor = {
            "deviceStates": {"wifiArrayReadIndex": 0, "states": [
                {"strKey": "Dishwasher_State_Running", "wifiArrayValue": 1},
            ]},
        }
        return client, calls

    async def test_valid_shadow_update_calls_on_state(self):
        client, calls = self._make_client()
        payload = json.dumps({"state": {"reported": {"wfa": [1], "wfaStartOffset": 0}}}).encode()
        await client._handle_message("$aws/things/abc123/shadow/update/accepted", payload)
        assert calls == [("Zmywarka", "running")]

    async def test_string_wfa_start_offset_is_coerced_to_int(self):
        client, calls = self._make_client()
        payload = json.dumps({"state": {"reported": {"wfa": [1], "wfaStartOffset": "0"}}}).encode()
        await client._handle_message("$aws/things/abc123/shadow/update/accepted", payload)
        assert calls == [("Zmywarka", "running")]

    async def test_unknown_appliance_id_ignored(self):
        client, calls = self._make_client()
        payload = json.dumps({"state": {"reported": {"wfa": [1], "wfaStartOffset": 0}}}).encode()
        await client._handle_message("$aws/things/other-device/shadow/update/accepted", payload)
        assert calls == []

    async def test_malformed_payload_does_not_raise(self, caplog):
        client, calls = self._make_client()
        with caplog.at_level("INFO"):
            await client._handle_message("$aws/things/abc123/shadow/update/accepted", b"not json")
        assert calls == []
        assert any("nieoczekiwana struktura wiadomości" in r.message for r in caplog.records)

    async def test_missing_config_descriptor_ignored(self, caplog):
        client, calls = self._make_client()
        client._appliances["Zmywarka"].config_descriptor = None
        payload = json.dumps({"state": {"reported": {"wfa": [1], "wfaStartOffset": 0}}}).encode()
        with caplog.at_level("INFO"):
            await client._handle_message("$aws/things/abc123/shadow/update/accepted", payload)
        assert calls == []
        assert any("brak jeszcze deskryptora konfiguracji" in r.message for r in caplog.records)

    async def test_unresolved_state_value_does_not_call_on_state(self, caplog):
        client, calls = self._make_client()
        payload = json.dumps({"state": {"reported": {"wfa": [99], "wfaStartOffset": 0}}}).encode()
        with caplog.at_level("INFO"):
            await client._handle_message("$aws/things/abc123/shadow/update/accepted", payload)
        assert calls == []
        assert any("brak dopasowanego stanu" in r.message for r in caplog.records)

    async def test_decode_error_is_caught_and_logged_not_raised(self, caplog):
        client, calls = self._make_client()
        payload = json.dumps({"state": {"reported": {"wfa": 5, "wfaStartOffset": 0}}}).encode()
        with caplog.at_level("ERROR"):
            await client._handle_message("$aws/things/abc123/shadow/update/accepted", payload)
        assert calls == []
        assert any("błąd przetwarzania wiadomości" in r.message.lower() for r in caplog.records)

    async def test_run_forever_without_appliances_is_a_noop(self):
        calls = []

        async def on_state(name, state):
            calls.append((name, state))

        client = hw.HomeWhizClient({"username": "u", "password": "p", "appliances": {}}, on_state)
        await client.run_forever()
        assert calls == []


class TestConfigDescriptorRetry:

    def _make_client(self, appliance_ids=("a1",)):
        calls = []

        async def on_state(name, state):
            calls.append((name, state))

        appliances_cfg = {f"App{i}": {"appliance_id": aid} for i, aid in enumerate(appliance_ids)}
        cfg = {"username": "u", "password": "p", "appliances": appliances_cfg}
        client = hw.HomeWhizClient(cfg, on_state)
        client._session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        return client

    async def test_fetch_missing_only_targets_appliances_without_descriptor(self, monkeypatch):
        client = self._make_client(("a1", "a2"))
        names = list(client._appliances)
        client._appliances[names[0]].config_descriptor = {"already": "set"}

        fetched_ids = []

        async def fake_fetch(session, appliance_id, language="en", region=hw.DEFAULT_REGION):
            fetched_ids.append(appliance_id)
            return {"deviceStates": {}}

        monkeypatch.setattr(hw, "fetch_config_descriptor", fake_fetch)
        await client._fetch_missing_config_descriptors()

        assert fetched_ids == ["a2"]
        assert client._appliances[names[0]].config_descriptor == {"already": "set"}
        assert client._appliances[names[1]].config_descriptor == {"deviceStates": {}}

    async def test_fetch_missing_logs_and_leaves_none_on_failure(self, monkeypatch, caplog):
        client = self._make_client(("a1",))

        async def fake_fetch(*a, **kw):
            raise hw.HomeWhizError("boom")

        monkeypatch.setattr(hw, "fetch_config_descriptor", fake_fetch)
        with caplog.at_level("ERROR"):
            await client._fetch_missing_config_descriptors()

        name = next(iter(client._appliances))
        assert client._appliances[name].config_descriptor is None
        assert any("deskryptora konfiguracji" in r.message for r in caplog.records)

    async def test_retry_loop_eventually_recovers_missing_descriptor(self, monkeypatch):
        client = self._make_client(("a1",))

        attempts = []

        async def fake_fetch(session, appliance_id, language="en", region=hw.DEFAULT_REGION):
            attempts.append(appliance_id)
            return {"deviceStates": {}}

        monkeypatch.setattr(hw, "fetch_config_descriptor", fake_fetch)

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr(hw.asyncio, "sleep", fake_sleep)

        await client._retry_missing_config_descriptors_loop()

        assert attempts == ["a1"]
        assert sleep_calls == [hw._CONFIG_DESCRIPTOR_RETRY_SECONDS, hw._CONFIG_DESCRIPTOR_RETRY_SECONDS]
        name = next(iter(client._appliances))
        assert client._appliances[name].config_descriptor == {"deviceStates": {}}

    async def test_retry_loop_returns_immediately_when_nothing_missing(self, monkeypatch):
        client = self._make_client(("a1",))
        name = next(iter(client._appliances))
        client._appliances[name].config_descriptor = {"already": "set"}

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr(hw.asyncio, "sleep", fake_sleep)

        await client._retry_missing_config_descriptors_loop()

        assert sleep_calls == [hw._CONFIG_DESCRIPTOR_RETRY_SECONDS]
