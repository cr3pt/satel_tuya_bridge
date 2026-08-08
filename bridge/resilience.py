"""Wspólny mechanizm odporności na awarie połączeń.

RPi Zero W często łączy się przez WiFi - chwilowe zerwania (routera,
sieci, restartu ebusd, chwilowej niedostępności urządzenia Tuya) są
normą, nie wyjątkiem. Każdy komponent, który łączy się z czymś
zewnętrznym, powinien sam się podnosić po awarii zamiast wywalać cały
proces (co i tak by uratował systemd, ale ze zbędną przerwą w działaniu
WSZYSTKICH funkcji, nie tylko tej jednej niedziałającej).

Dwie odpowiedzialności, celowo rozdzielone:

1. `HealthReporter` - zapisuje status "połączono/rozłączono" do panelu
   (kategoria system_health) i loguje zdarzenie PRZY ZMIANIE stanu.
   Dodatkowo wysyła powiadomienie push (ntfy), ale dopiero po okresie
   KARENCJI (domyślnie 120s) nieprzerwanego rozłączenia - inaczej
   chwilowe zerwanie WiFi na kilka sekund zasypywałoby telefon alertami.
   Gdy połączenie wraca, a powiadomienie o awarii zostało wcześniej
   wysłane, wysyłane jest też powiadomienie o przywróceniu.

2. `Supervisor` - opakowuje dowolną korutynę w pętlę z exponential
   backoff, korzystając z HealthReportera do raportowania stanu.
"""
from __future__ import annotations

import asyncio
import logging

from .notifier import Notifier

logger = logging.getLogger("bridge.resilience")


class HealthReporter:
    """Można używać zarówno z kodu asyncio (Supervisor), jak i z callbacków
    wywoływanych z innego wątku (np. paho-mqtt) - w tym drugim przypadku
    trzeba podać `loop` (pętlę główną procesu), żeby powiadomienia
    (korutyny) dało się bezpiecznie zaplanować z obcego wątku."""

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
            return  # brak zmiany stanu - nic więcej do zrobienia

        self._last_connected = connected
        if self.history:
            self.history.write_event(
                "system", source=self.name, detail={"connected": connected, "message": status_text},
            )
        logger.log(logging.INFO if connected else logging.WARNING, "%s: %s", self.name, status_text)

        # Pętlę asyncio pobieramy DOPIERO tutaj, TYLKO gdy faktycznie
        # trzeba zaplanować powiadomienie - nie przy każdym wywołaniu
        # report(). Dzięki temu report() można bezpiecznie wywołać także
        # poza działającą pętlą asyncio (np. przy starcie/testach), o ile
        # nie trzeba akurat wysłać powiadomienia - inaczej Python 3.12
        # rzuca RuntimeError zamiast tylko ostrzeżenia.
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
        if self._last_connected is False:  # nadal rozłączone po okresie karencji
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
        """`connect_and_run` to funkcja async przyjmująca `on_connected()` -
        callback, który komponent wywołuje PO faktycznym nawiązaniu
        połączenia (nie tylko po starcie próby), żeby backoff resetował
        się dopiero po prawdziwym sukcesie, a nie po samym uruchomieniu
        pętli klienta."""
        delay = self.base_delay
        while True:
            async def on_connected():
                nonlocal delay
                delay = self.base_delay
                self.health.report(True)

            try:
                await connect_and_run(on_connected)
                # normalny powrót z funkcji = połączenie się zakończyło -
                # traktujemy to jak rozłączenie i próbujemy ponownie
                raise ConnectionError("połączenie zakończone (funkcja monitorująca zwróciła się)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.report(False, str(exc))
                logger.debug("%s: szczegóły błędu", self.name, exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_delay)
