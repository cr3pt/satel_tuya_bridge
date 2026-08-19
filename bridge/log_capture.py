"""Przechwytywanie logów Pythona do bazy (HistoryStore.logs), żeby były
widoczne w przeglądarce (zakładka 🐞 Logi) - nie tylko w `docker compose
logs`, do czego trzeba dostępu do konsoli/SSH hosta.

`HistoryLogHandler` to zwykły `logging.Handler` - podpięty pod logger
"bridge" (rodzic wszystkich `bridge.*`) w main.py (proces bridge) i
dashboard/app.py (proces panelu), więc każdy komunikat na poziomie
wyższym-lub-równym efektywnemu poziomowi danego loggera (patrz
bridge/logging_config.py: apply_log_levels) trafia też do bazy, obok
zwykłego wypisania na stdout/stderr przez handler z logging.basicConfig.

Przycinanie (HistoryStore.prune_logs) uruchamiane co PRUNE_EVERY_N zapisów
zamiast po każdym - DEBUG potrafi generować dużo wpisów, a DELETE+ORDER BY
na każdym pojedynczym zapisie byłoby zbyt kosztowne."""
from __future__ import annotations

import logging

from .history_store import HistoryStore

PRUNE_EVERY_N = 200
KEEP_LOGS = 10000


class HistoryLogHandler(logging.Handler):
    def __init__(self, history: HistoryStore, component: str):
        super().__init__()
        self.history = history
        self.component = component
        self._writes_since_prune = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.history.write_log(self.component, record.name, record.levelname, message, ts=record.created)
            self._writes_since_prune += 1
            if self._writes_since_prune >= PRUNE_EVERY_N:
                self._writes_since_prune = 0
                self.history.prune_logs(keep=KEEP_LOGS)
        except Exception:
            # Handler logowania NIE MOŻE sam rzucić - to zapętliłoby
            # logowanie (błąd w handlerze -> logging.Handler.handleError ->
            # próba zalogowania błędu -> ten sam handler...). Ciche
            # pominięcie pojedynczego wpisu jest bezpieczniejsze.
            pass


def attach_log_capture(history: HistoryStore, component: str) -> HistoryLogHandler:
    """Podpina handler pod logger "bridge" (rodzic wszystkich modułów
    integracji - patrz bridge/logging_config.py: PLUGIN_LOGGERS).
    `propagate=True` (domyślne) na loggerach potomnych sprawia, że jeden
    handler tu wystarcza dla całego drzewa `bridge.*`.

    IDEMPOTENTNE: jeśli handler już istnieje (np. dashboard/app.py:
    create_app() wywoływane ponownie po każdym zapisie ustawień z panelu),
    NIE dodaje drugiego (co powielałoby każdy wpis logu), tylko podmienia
    referencję do `history` na świeżą (nowe połączenie SQLite po
    przeładowaniu configu)."""
    bridge_logger = logging.getLogger("bridge")
    for existing in bridge_logger.handlers:
        if isinstance(existing, HistoryLogHandler) and existing.component == component:
            existing.history = history
            return existing

    handler = HistoryLogHandler(history, component)
    handler.setFormatter(logging.Formatter("%(message)s"))
    bridge_logger.addHandler(handler)
    return handler
