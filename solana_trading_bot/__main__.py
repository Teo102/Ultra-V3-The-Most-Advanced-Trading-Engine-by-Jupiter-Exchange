"""Point d'entrée CLI du bot.

Exemples :
  python -m solana_trading_bot run          # boucle continue (paper)
  python -m solana_trading_bot once         # un seul cycle
  python -m solana_trading_bot scan         # découverte + analyse, sans trade
  python -m solana_trading_bot rank         # tableau de notation des tokens
  python -m solana_trading_bot stats        # rapport de performance
  python -m solana_trading_bot backtest --pool <pool> --interval 5m
  python -m solana_trading_bot backtest     # backtest des tokens découverts
"""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .engine import TradingEngine
from .logger import setup_logging, get_logger
from .reporting import print_report, print_rankings, print_backtest
from .storage.database import Database

log = get_logger("cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solana_trading_bot",
        description="Bot de trading & analyse small-cap Solana (500k-3M MC)",
    )
    parser.add_argument(
        "command", nargs="?", default="run",
        choices=["run", "once", "scan", "rank", "stats", "backtest"],
        help="run=boucle | once=1 cycle | scan=analyse seule | "
             "rank=notation | stats=rapport | backtest=test historique",
    )
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="chemin du fichier de configuration")
    # Options de backtest
    parser.add_argument("--pool", help="adresse de pool à backtester")
    parser.add_argument("--symbol", default="", help="libellé du token")
    parser.add_argument("--interval", help="intervalle OHLCV (défaut: config)")
    parser.add_argument("--limit", type=int, default=500,
                        help="nombre de bougies (défaut 500)")
    parser.add_argument("--liquidity", type=float, default=100000,
                        help="liquidité supposée pour les fills (défaut 100k$)")
    parser.add_argument("--top", type=int, default=5,
                        help="nb de tokens découverts à backtester (défaut 5)")
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

    if args.command == "backtest":
        return _run_backtest(config, args)

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
    elif args.command == "rank":
        rows = engine.analyze_market()
        print_rankings(rows)
        engine.shutdown()

    return 0


def _run_backtest(config: Config, args) -> int:
    from .backtest import Backtester
    from .clients.geckoterminal import GeckoTerminalClient
    from .clients.dexscreener import DexScreenerClient

    interval = args.interval or config.get("analysis.ohlcv_interval", "15m")
    gecko = GeckoTerminalClient()
    bt = Backtester(config, liquidity_usd=args.liquidity)
    results = []

    if args.pool:
        candles = gecko.get_ohlcv(args.pool, interval, args.limit)
        if not candles:
            print("[BACKTEST] Aucune donnée OHLCV pour ce pool.", file=sys.stderr)
            return 1
        results.append(bt.run(args.symbol or args.pool[:8], candles, interval))
    else:
        # Backtest sur les tokens éligibles découverts
        log.info("Découverte des tokens à backtester…")
        dex = DexScreenerClient()
        from .safety.filters import UniverseFilter
        uni = UniverseFilter(config)
        pairs = [p for p in dex.discover() if uni.passes(p)[0]]
        pairs.sort(key=lambda p: p.volume_24h, reverse=True)
        for pair in pairs[: args.top]:
            candles = gecko.get_ohlcv(pair.pair_address, interval, args.limit)
            if candles:
                results.append(bt.run(pair.base_symbol, candles, interval))

    if not results:
        print("[BACKTEST] Aucun résultat.", file=sys.stderr)
        return 1
    print_backtest(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
