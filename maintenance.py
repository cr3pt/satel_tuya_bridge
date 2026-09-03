import argparse
import logging

from bridge.config import BridgeConfig
from bridge.maintenance import run_maintenance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("maintenance.cli")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.db")
    args = parser.parse_args()

    config = BridgeConfig.load(args.config)
    summary = run_maintenance(config)
    logger.info("Podsumowanie: %s", summary)


if __name__ == "__main__":
    main()
