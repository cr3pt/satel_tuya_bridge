# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import bridge.weather_client as weather_client
from bridge.weather_client import fetch_forecast


class _FakeResponse:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json_data = json_data

    async def json(self):
        return self._json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def _patch_session(monkeypatch, response):
    session = _FakeSession(response)
    monkeypatch.setattr(weather_client.aiohttp, "ClientSession", lambda *a, **kw: session)
    return session


class TestFetchForecast:
    async def test_returns_probability_and_sum_for_today(self, monkeypatch):
        response = _FakeResponse(200, {
            "daily": {
                "time": ["2026-09-10"],
                "precipitation_probability_max": [80],
                "precipitation_sum": [5.2],
            },
        })
        _patch_session(monkeypatch, response)
        result = await fetch_forecast(52.2, 21.0)
        assert result == {"precipitation_probability_percent": 80, "precipitation_sum_mm": 5.2}

    async def test_passes_latitude_longitude_as_query_params(self, monkeypatch):
        response = _FakeResponse(200, {
            "daily": {"time": ["x"], "precipitation_probability_max": [0], "precipitation_sum": [0]},
        })
        session = _patch_session(monkeypatch, response)
        await fetch_forecast(52.2, 21.0)
        url, kwargs = session.calls[0]
        assert kwargs["params"]["latitude"] == 52.2
        assert kwargs["params"]["longitude"] == 21.0

    async def test_non_200_status_returns_none(self, monkeypatch):
        _patch_session(monkeypatch, _FakeResponse(500, {}))
        assert await fetch_forecast(52.2, 21.0) is None

    async def test_missing_daily_fields_returns_none(self, monkeypatch):
        _patch_session(monkeypatch, _FakeResponse(200, {"daily": {}}))
        assert await fetch_forecast(52.2, 21.0) is None

    async def test_request_exception_returns_none_not_raises(self, monkeypatch):
        class RaisingSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, *a, **kw):
                raise ConnectionError("brak sieci")

        monkeypatch.setattr(weather_client.aiohttp, "ClientSession", lambda *a, **kw: RaisingSession())
        assert await fetch_forecast(52.2, 21.0) is None
