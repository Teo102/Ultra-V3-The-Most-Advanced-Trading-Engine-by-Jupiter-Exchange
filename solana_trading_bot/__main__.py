"""Point d'entrée CLI du bot.

Exemples :
  python -m solana_trading_bot run          # boucle continue (paper)
  python -m solana_trading_bot once         # un seul cycle
  python -m solana_trading_bot scan         # découverte + analyse, sans trade
  python -m solana_trading_bot stats        # rapport de performance
"""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .engine import TradingEngine
from .logger import setup_logging, get_logger
from .reporting import print_report
from .storage.database import Database

log = get_logger("cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solana_trading_bot",
        description="Bot de trading & analyse small-cap Solana (500k-3M MC)",
    )
    parser.add_argument(
        "command", nargs="?", default="run",
        choices=["run", "once", "scan", "stats"],
        help="run=boucle | once=1 cycle | scan=analyse seule | stats=rapport",
    )
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="chemin du fichier de configuration")
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[CONFIG] {exc}", file=sys.stderr)
        return 2

    setup_logging(
        config.get("storage.log_path", "bot.log"),
        config.get("storage.log_level", "INFO"),
    )

    if args.command == "stats":
        db = Database(config.get("storage.db_path", "bot_data.sqlite"))
        print_report(db)
        db.close()
        return 0

    engine = TradingEngine(config)

    if config.is_live:
        log.warning("⚠️  MODE LIVE sélectionné — l'exécution réelle n'est pas "
                    "activée dans ce build (garde-fou). Reste en paper.")

    if args.command == "run":
        engine.run_forever()
    elif args.command == "once":
        engine.run_once()
        engine.shutdown()
    elif args.command == "scan":
        engine.scan_and_trade()
        engine.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
