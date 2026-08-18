"""Punkt wejścia bridge'a Satel <-> Tuya <-> eBUS.

Uruchomienie:
    python main.py [--config config.yaml]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from bridge.config import BridgeConfig
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

    engine = RulesEngine(config)
    await engine.start()

    # Utrzymuj proces przy życiu - cała logika działa w zarejestrowanych
    # callbackach i zadaniach w tle.
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Zatrzymano")
