# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from bridge.tuya_cloud_events import TuyaCameraMotionListener


class FakeWsApp:
    def __init__(self):
        self.on_error = None


class FakePulsar:
    def __init__(self):
        self.ws_app = FakeWsApp()
        self.error_calls = []

    def _on_error(self, ws_app, error):
        self.error_calls.append(error)


async def _noop_on_motion(device_id):
    pass


def _make_listener():
    return TuyaCameraMotionListener(
        cloud_cfg={"access_id": "a", "access_key": "b", "mq_endpoint": "wss://x/", "camera_device_ids": []},
        on_motion=_noop_on_motion,
    )


class TestWrapErrorHandler:
    async def test_detects_401_and_sets_auth_error(self):
        listener = _make_listener()
        listener._pulsar = FakePulsar()
        listener._wrap_error_handler()

        listener._pulsar.ws_app.on_error(listener._pulsar.ws_app, "Handshake status 401 Unauthorized")

        assert listener._auth_error is not None
        assert "401" in listener._auth_error

    async def test_detects_unauthorized_text_even_without_401_digits(self):
        listener = _make_listener()
        listener._pulsar = FakePulsar()
        listener._wrap_error_handler()

        listener._pulsar.ws_app.on_error(listener._pulsar.ws_app, "Unauthorized")

        assert listener._auth_error == "Unauthorized"

    async def test_non_auth_errors_do_not_set_auth_error(self):
        listener = _make_listener()
        listener._pulsar = FakePulsar()
        listener._wrap_error_handler()

        listener._pulsar.ws_app.on_error(listener._pulsar.ws_app, "Connection reset by peer")

        assert listener._auth_error is None

    async def test_original_handler_is_still_called(self):
        listener = _make_listener()
        listener._pulsar = FakePulsar()
        listener._wrap_error_handler()

        listener._pulsar.ws_app.on_error(listener._pulsar.ws_app, "Handshake status 401 Unauthorized")

        assert listener._pulsar.error_calls == ["Handshake status 401 Unauthorized"]

    async def test_auth_error_starts_as_none(self):
        listener = _make_listener()
        assert listener._auth_error is None
