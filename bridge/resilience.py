from __future__ import annotations

import asyncio
import logging

from .notifier import Notifier

logger = logging.getLogger("bridge.resilience")


class HealthReporter:

    def __init__(self, name: str, history=None, notify_cfg: dict | None = None,
                 grace_seconds: float = 120, loop: asyncio.AbstractEventLoop | None = None):
        self.name = name
        self.history = history
        self.notifier = Notifier(notify_cfg)
        self.grace_seconds = grace_seconds
        self._loop = loop
        self._last_connected: bool | None = None
        self._notified_offline = False
        self._grace_task: asyncio.Task | None = None

    def _loop_or_current(self) -> asyncio.AbstractEventLoop:
        return self._loop or asyncio.get_event_loop()

    def report(self, connected: bool, detail: str = "") -> None:
        status_text = "połączono" if connected else f"rozłączono{': ' + detail if detail else ''}"
        if self.history:
            self.history.set_status(f"health:{self.name}", "system_health", status_text, label=self.name)

        if connected == self._last_connected:
            return

        self._last_connected = connected
        if self.history:
            self.history.write_event(
                "system", source=self.name, detail={"connected": connected, "message": status_text},
            )
        logger.log(logging.INFO if connected else logging.WARNING, "%s: %s", self.name, status_text)

        if connected:
            if self._grace_task and not self._grace_task.done():
                self._grace_task.cancel()
            if self._notified_offline and self.notifier.enabled:
                loop = self._loop_or_current()
                asyncio.run_coroutine_threadsafe(
                    self.notifier.send(f"✅ {self.name}: online", "Połączenie zostało przywrócone."), loop,
                )
            self._notified_offline = False
        else:
            if self.notifier.enabled:
                loop = self._loop_or_current()
                self._grace_task = asyncio.run_coroutine_threadsafe(self._grace_timer(), loop)

    async def _grace_timer(self) -> None:
        await asyncio.sleep(self.grace_seconds)
        if self._last_connected is False:
            self._notified_offline = True
            await self.notifier.send(
                f"⚠️ {self.name}: offline",
                f"Brak połączenia od ponad {int(self.grace_seconds)}s.",
                priority="high",
            )


class Supervisor:
    def __init__(self, name: str, history=None, base_delay: float = 5, max_delay: float = 300,
                 notify_cfg: dict | None = None, grace_seconds: float = 120):
        self.name = name
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.health = HealthReporter(name, history=history, notify_cfg=notify_cfg, grace_seconds=grace_seconds)

    async def run(self, connect_and_run) -> None:
        delay = self.base_delay
        while True:
            async def on_connected():
                nonlocal delay
                delay = self.base_delay
                self.health.report(True)

            try:
                await connect_and_run(on_connected)
                raise ConnectionError("połączenie zakończone (funkcja monitorująca zwróciła się)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.report(False, str(exc))
                logger.debug("%s: szczegóły błędu", self.name, exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_delay)
