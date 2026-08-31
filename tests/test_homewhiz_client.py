"""Testy bridge/homewhiz_client.py.

Podpisywanie SigV4 (`_sigv4_headers`/`_canonical_query_string`) jest
zweryfikowane względem OFICJALNEGO zestawu testowego AWS Signature Version 4
(https://docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html,
licencja Apache 2.0, lustro: github.com/saibotsivad/aws-sig-v4-test-suite)
- przypadki "get-vanilla" (podstawowe podpisywanie nagłówkiem) i
"get-vanilla-query-order-key" (kanoniczna kolejność parametrów zapytania,
w tym DWA parametry o tej samej nazwie). Stałe (klucze/daty/oczekiwane
skróty) pochodzą wprost z plików tego zestawu testowego, nie są zgadywane."""
import hashlib
import hmac
import json

import pytest

from bridge import homewhiz_client as hw

# --- Stałe z oficjalnego testu "get-vanilla" AWS SigV4 --------------------
_AWS_ACCESS_KEY = "AKIDEXAMPLE"
_AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_AWS_REGION = "us-east-1"
_AWS_SERVICE = "service"
_AWS_HOST = "example.amazonaws.com"


class _FixedNow:
    """`_sigv4_headers` liczy datę SAMO (wewnątrz `_amz_dates()`) - podmieniamy
    `datetime.datetime.now` na stałą wartość z testu AWS, żeby porównać
    wynik z opublikowanym oczekiwanym podpisem."""


@pytest.fixture(autouse=True)
def _fixed_amz_dates(monkeypatch):
    monkeypatch.setattr(hw, "_amz_dates", lambda now=None: ("20150830T123600Z", "20150830"))


class TestSigV4HeaderSigning:
    """`_sigv4_headers` - odpowiednik oficjalnego przypadku "get-vanilla"
    (GET / bez parametrów, bez tokenu sesji)."""

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
        assert "host" not in headers  # aiohttp dokłada Host sam

    def test_includes_security_token_when_session_based(self):
        headers = hw._sigv4_headers(
            method="GET", host=_AWS_HOST, path="/", query="",
            region=_AWS_REGION, service=_AWS_SERVICE,
            access_key=_AWS_ACCESS_KEY, secret_key=_AWS_SECRET_KEY, session_token="tok123",
        )
        assert headers["X-Amz-Security-Token"] == "tok123"
        assert "SignedHeaders=host;x-amz-date;x-amz-security-token" in headers["Authorization"]


class TestCanonicalQueryString:
    """Odpowiednik oficjalnego przypadku "get-vanilla-query-order-key" -
    DWA parametry o tej samej nazwie muszą trafić do kanonicznego zapytania
    posortowane po WARTOŚCI (bajtowo: 'V' < 'v'), nie w kolejności podanej."""

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
    """`presign_iot_websocket_url` używa TYCH SAMYCH, już zweryfikowanych
    prymitywów (_signing_key/canoniczne żądanie) - dla parametrów zapytania
    zamiast nagłówka. Niezależnie odtwarzamy oczekiwany podpis z tych samych
    prymitywów, żeby wyłapać błędy w SAMYM wiązaniu (zła usługa/URI/kolejność
    parametrów), a osobno sprawdzamy udokumentowaną przez AWS osobliwość:
    X-Amz-Security-Token dokłada się PO policzeniu podpisu."""

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
        """Token sesji NIE bierze udziału w podpisywanym zapytaniu
        kanonicznym - dwa wywołania z RÓŻNYMI tokenami, ale tym samym
        `now`, muszą dać TEN SAM podpis."""
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

        # Niezależne odtworzenie oczekiwanego podpisu z JUŻ zweryfikowanych
        # prymitywów (_signing_key), krok po kroku wg dokumentacji AWS -
        # celowo NIE wołając presign_iot_websocket_url samego siebie.
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
        # offset=2 -> wfa zaczyna się od indeksu 2, więc wfa[0] LĄDUJE pod
        # indeksem 2 tablicy (dokładnie tam, gdzie czyta wifiArrayReadIndex).
        assert hw.decode_state(self.CONFIG, 2, [2]) == "finished"

    def test_unmatched_value_returns_none(self):
        assert hw.decode_state(self.CONFIG, 0, [9, 9, 99]) is None

    def test_missing_read_index_returns_none(self):
        assert hw.decode_state({"deviceStates": {"states": []}}, 0, [1]) is None

    def test_empty_descriptor_returns_none(self):
        assert hw.decode_state({}, 0, [1]) is None

    def test_clamps_wrapped_value_before_matching(self):
        # 128 + 1 = wartość "1, ale zmieniona od ostatniego odczytu" - dla
        # samego odczytu stanu liczy się tylko dolne 128 wartości.
        assert hw.decode_state(self.CONFIG, 0, [9, 9, 129]) == "running"


# --- REST (login/list_appliances/fetch_config_descriptor), aiohttp ------

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
    """Kolejka odpowiedzi wyskakiwanych PO KOLEI, w kolejności wołania
    get()/post() - podmienia aiohttp.ClientSession w bridge/homewhiz_client.py
    (ten moduł tworzy nową sesję na KAŻDE żądanie, tak jak bridge/notifier.py)."""

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


class TestLogin:
    async def test_successful_login_returns_session(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(200, {
            "accessKey": "AK", "secretKey": "SK", "sessionToken": "ST", "expiration": "2026-01-01T00:00:00Z",
        })])
        session = await hw.login("user@example.com", "pw")
        assert session.access_key == "AK"
        assert session.secret_key == "SK"
        assert session.session_token == "ST"

    async def test_bad_credentials_raises_homewhiz_error(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(401, {})])
        with pytest.raises(hw.HomeWhizError):
            await hw.login("user@example.com", "wrong")

    async def test_malformed_response_raises_homewhiz_error(self, monkeypatch):
        _patch_session(monkeypatch, [_FakeResponse(200, {"accessKey": "AK"})])  # brak reszty pól
        with pytest.raises(hw.HomeWhizError):
            await hw.login("user@example.com", "pw")


class TestListAppliances:
    async def test_flattens_appliances_across_homes(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [
            _FakeResponse(200, [{"id": "home1"}, {"id": "home2"}]),
            _FakeResponse(200, [{"applianceId": "a1", "applianceType": 3, "connectivity": "WIFI", "name": "Zmywarka"}]),
            _FakeResponse(200, [{"applianceId": "a2", "applianceType": 1, "connectivity": "WIFI", "name": "Pralka"}]),
        ])
        appliances = await hw.list_appliances(session)
        assert {a["applianceId"] for a in appliances} == {"a1", "a2"}

    async def test_excludes_bluetooth_only_appliances(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [
            _FakeResponse(200, [{"id": "home1"}]),
            _FakeResponse(200, [
                {"applianceId": "a1", "connectivity": "WIFI"},
                {"applianceId": "a2", "connectivity": "BT"},
                {"applianceId": "a3", "connectivity": "BASICBT"},
            ]),
        ])
        appliances = await hw.list_appliances(session)
        assert {a["applianceId"] for a in appliances} == {"a1"}

    async def test_skips_entries_without_appliance_id(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [
            _FakeResponse(200, [{"id": "home1"}]),
            _FakeResponse(200, [{"connectivity": "WIFI"}]),
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
            _FakeResponse(200, [{"cid": "cfg123", "ver": "2", "lang": "en"}]),
            _FakeResponse(200, expected),
        ])
        result = await hw.fetch_config_descriptor(session, "appliance1", language="en")
        assert result == expected
        assert calls[1][1] == "https://s3-eu-west-1.amazonaws.com/procam-contents/CONFIGURATIONS/cfg123/v2/cfg123.en.json"

    async def test_empty_entries_raises_homewhiz_error(self, monkeypatch):
        session = hw.HomeWhizSession(access_key="AK", secret_key="SK", session_token="ST")
        _patch_session(monkeypatch, [_FakeResponse(200, [])])
        with pytest.raises(hw.HomeWhizError):
            await hw.fetch_config_descriptor(session, "appliance1")


class TestHomeWhizClientMessageHandling:
    """`_handle_message` - część czysto logiczna klienta (dekodowanie
    payloadu MQTT do wywołania on_state), bez faktycznego łączenia się z
    AWS IoT Core (patrz TestByteDecoding/TestDecodeState po sam algorytm)."""

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

    async def test_unknown_appliance_id_ignored(self):
        client, calls = self._make_client()
        payload = json.dumps({"state": {"reported": {"wfa": [1], "wfaStartOffset": 0}}}).encode()
        await client._handle_message("$aws/things/other-device/shadow/update/accepted", payload)
        assert calls == []

    async def test_malformed_payload_does_not_raise(self):
        client, calls = self._make_client()
        await client._handle_message("$aws/things/abc123/shadow/update/accepted", b"not json")
        assert calls == []

    async def test_missing_config_descriptor_ignored(self):
        client, calls = self._make_client()
        client._appliances["Zmywarka"].config_descriptor = None
        payload = json.dumps({"state": {"reported": {"wfa": [1], "wfaStartOffset": 0}}}).encode()
        await client._handle_message("$aws/things/abc123/shadow/update/accepted", payload)
        assert calls == []

    async def test_unresolved_state_value_does_not_call_on_state(self):
        client, calls = self._make_client()
        payload = json.dumps({"state": {"reported": {"wfa": [99], "wfaStartOffset": 0}}}).encode()
        await client._handle_message("$aws/things/abc123/shadow/update/accepted", payload)
        assert calls == []

    async def test_run_forever_without_appliances_is_a_noop(self):
        calls = []

        async def on_state(name, state):
            calls.append((name, state))

        client = hw.HomeWhizClient({"username": "u", "password": "p", "appliances": {}}, on_state)
        await client.run_forever()
        assert calls == []
