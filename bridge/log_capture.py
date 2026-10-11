# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

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
            pass


def attach_log_capture(history: HistoryStore, component: str) -> HistoryLogHandler:
    bridge_logger = logging.getLogger("bridge")
    for existing in bridge_logger.handlers:
        if isinstance(existing, HistoryLogHandler) and existing.component == component:
            existing.history = history
            return existing

    handler = HistoryLogHandler(history, component)
    handler.setFormatter(logging.Formatter("%(message)s"))
    bridge_logger.addHandler(handler)
    return handler
