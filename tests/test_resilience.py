# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

import asyncio

from bridge.resilience import HealthReporter, Supervisor


class TestHealthReporterStatus:
    def test_connected_reported_to_history(self, history):
        reporter = HealthReporter("test_component", history=history)
        reporter.report(True)
        statuses = history.get_status("system_health")
        assert len(statuses) == 1
        assert statuses[0]["value"] == "połączono"

    def test_disconnected_reported_with_detail(self, history):
        reporter = HealthReporter("test_component", history=history)
        reporter.report(False, "timeout")
        statuses = history.get_status("system_health")
        assert "rozłączono" in statuses[0]["value"]
        assert "timeout" in statuses[0]["value"]

    def test_no_duplicate_events_on_repeated_same_state(self, history):
        reporter = HealthReporter("test_component", history=history)
        reporter.report(False, "błąd 1")
        reporter.report(False, "błąd 2")
        reporter.report(False, "błąd 3")
        events = history.get_events("system", 0, 9999999999)
        assert len(events) == 1

    def test_transition_logs_event(self, history):
        reporter = HealthReporter("test_component", history=history)
        reporter.report(True)
        reporter.report(False, "padło")
        reporter.report(True)
        events = history.get_events("system", 0, 9999999999)
        assert len(events) == 3


class TestHealthReporterNotifications:
    async def test_short_blip_within_grace_period_sends_no_notification(self, history, mocker):
        sent = []
        mocker.patch(
            "bridge.notifier.Notifier.send",
            side_effect=lambda title, msg, priority="default": sent.append((title, msg)),
        )
        mocker.patch("bridge.notifier.Notifier.enabled", new_callable=mocker.PropertyMock, return_value=True)

        reporter = HealthReporter("test", history=history, notify_cfg={"ntfy_topic_url": "https://x"}, grace_seconds=1.0)
        reporter.report(False, "padło")
        await asyncio.sleep(0.1)
        reporter.report(True)
        await asyncio.sleep(0.05)
        assert sent == []

    async def test_prolonged_outage_sends_offline_then_online_notification(self, history, mocker):
        sent = []

        async def fake_send(title, msg, priority="default"):
            sent.append((title, msg))

        mocker.patch("bridge.notifier.Notifier.send", side_effect=fake_send)
        mocker.patch("bridge.notifier.Notifier.enabled", new_callable=mocker.PropertyMock, return_value=True)

        reporter = HealthReporter("test", history=history, notify_cfg={"ntfy_topic_url": "https://x"}, grace_seconds=0.2)
        reporter.report(False, "padło na dłużej")
        await asyncio.sleep(0.35)
        assert len(sent) == 1
        assert "offline" in sent[0][0]

        reporter.report(True)
        await asyncio.sleep(0.05)
        assert len(sent) == 2
        assert "online" in sent[0][0] or "online" in sent[1][0]


class TestSupervisor:
    async def test_retries_with_backoff_until_success(self, history):
        attempt_count = 0

        async def connect_and_run(on_connected):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ConnectionError(f"awaria #{attempt_count}")
            await on_connected()
            await asyncio.sleep(10)

        supervisor = Supervisor("test", history=history, base_delay=0.05, max_delay=0.1)
        task = asyncio.create_task(supervisor.run(connect_and_run))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert attempt_count >= 3
        statuses = history.get_status("system_health")
        assert statuses[0]["value"] == "połączono"

    async def test_normal_return_treated_as_disconnect(self, history):
        call_count = 0

        async def connect_and_run(on_connected):
            nonlocal call_count
            call_count += 1
            await on_connected()

        supervisor = Supervisor("test", history=history, base_delay=0.02, max_delay=0.05)
        task = asyncio.create_task(supervisor.run(connect_and_run))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert call_count > 1
