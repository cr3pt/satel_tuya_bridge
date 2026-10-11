# Copyright (c) 2026 cr3pt. BSD-3-Clause with Commons Clause (no selling). See LICENSE.
# SPDX-License-Identifier: BSD-3-Clause AND LicenseRef-Commons-Clause

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from bridge.config import BridgeConfig
from bridge.log_capture import attach_log_capture
from bridge.logging_config import apply_log_levels
from bridge.rules_engine import RulesEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bridge.main")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.db")
    args = parser.parse_args()

    try:
        config = BridgeConfig.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        sys.exit(1)

    apply_log_levels(config.raw)

    engine = RulesEngine(config)
    attach_log_capture(engine.history, "bridge")
    await engine.start()

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Zatrzymano")
