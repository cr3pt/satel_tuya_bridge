"""CLI do ręcznego lub cronowego uruchamiania czyszczenia i backupu bazy.

Uruchomienie ręczne:
    python maintenance.py --config config.yaml

Zalecane uruchamianie przez cron (codziennie o 3:00, poza godzinami
szczytu ruchu w domu):
    0 3 * * * /home/pi/satel_tuya_bridge/venv/bin/python \\
        /home/pi/satel_tuya_bridge/maintenance.py --config /home/pi/satel_tuya_bridge/config.yaml \\
        >> /home/pi/satel_tuya_bridge/maintenance.log 2>&1

Bridge sam robi to samo automatycznie raz na dobę (patrz
`maintenance.run_in_process` w config.yaml) - użyj crona zamiast tego (nie
oprócz), jeśli wolisz mieć pełną kontrolę nad dokładną porą i niezależność
od tego, czy proces bridge'a akurat działa.
"""
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
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = BridgeConfig.load(args.config)
    summary = run_maintenance(config)
    logger.info("Podsumowanie: %s", summary)


if __name__ == "__main__":
    main()
