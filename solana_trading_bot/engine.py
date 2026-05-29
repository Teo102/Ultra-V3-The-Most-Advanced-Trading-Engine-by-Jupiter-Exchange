"""Orchestrateur principal du bot.

Pipeline par cycle :
  1. DÉCOUVERTE   — DexScreener agrège les paires Solana actives
  2. FILTRE 1     — éligibilité market-cap (500k-3M) + liquidité
  3. ANALYSE      — OHLCV Birdeye -> indicateurs -> score composite
  4. FILTRE 2     — anti-rug / honeypot (Birdeye + route Jupiter)
  5. DÉCISION     — sizing + risk checks -> achat (paper)
  6. GESTION      — SL / TP / trailing / time-exit sur positions ouvertes
"""

from __future__ import annotations

import time

from .analysis.signals import SignalEngine
from .clients.birdeye import BirdeyeClient
from .clients.dexscreener import DexScreenerClient
from .clients.jupiter import JupiterClient
from .config import Config
from .logger import get_logger
from .models import TokenPair
from .safety.filters import SafetyChecker, UniverseFilter
from .storage.database import Database
from .trading.portfolio import Portfolio
from .trading.risk import RiskManager

log = get_logger("engine")


class TradingEngine:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.db = Database(config.get("storage.db_path", "bot_data.sqlite"))

        # Clients
        self.dex = DexScreenerClient()
        self.birdeye = BirdeyeClient(config.secrets.birdeye_api_key)
        self.jupiter = JupiterClient()

        # Modules
        self.universe = UniverseFilter(config)
        self.safety = SafetyChecker(config, self.birdeye, self.jupiter)
        self.signals = SignalEngine(config)
        self.risk = RiskManager(config)
        self.portfolio = Portfolio(config, self.db)

        self.scan_interval = config.get("loop.scan_interval_sec", 60)
        self.manage_interval = config.get("loop.manage_interval_sec", 15)
        self.max_tokens = config.get("loop.max_tokens_per_scan", 40)

        self._last_scan = 0.0
        self._price_cache: dict[str, float] = {}

    # ------------------------------------------------------------------
    #  Découverte + analyse + entrée
    # ------------------------------------------------------------------
    def scan_and_trade(self) -> None:
        log.info("─" * 60)
        log.info("SCAN du marché…")
        pairs = self.dex.discover()

        # Filtre 1 : univers
        candidates: list[TokenPair] = []
        for pair in pairs:
            ok, _ = self.universe.passes(pair)
            if ok and pair.base_address not in self.portfolio.positions:
                candidates.append(pair)

        # Priorise les plus dynamiques (volume + momentum h1)
        candidates.sort(
            key=lambda p: (p.volume_24h * (1 + max(p.price_change_h1, 0) / 100)),
            reverse=True,
        )
        candidates = candidates[: self.max_tokens]
        log.info("Filtre univers : %d candidat(s) éligible(s) (500k-3M MC)",
                 len(candidates))

        # Coupe-circuit journalier
        equity = self.portfolio.equity(self._price_cache)
        self.portfolio.roll_day_if_needed(self._price_cache)
        if self.risk.daily_circuit_breaker(self.portfolio.day_start_equity,
                                            equity):
            log.warning("Achats suspendus aujourd'hui (coupe-circuit).")
            return

        for pair in candidates:
            self._evaluate_candidate(pair)

    def _evaluate_candidate(self, pair: TokenPair) -> None:
        # Analyse technique
        candles = self.birdeye.get_ohlcv(
            pair.base_address,
            self.cfg.get("analysis.ohlcv_interval", "15m"),
            self.cfg.get("analysis.ohlcv_lookback", 200),
        )
        analysis = self.signals.analyze(pair, candles)
        log.info("%-10s | MC %8.0fk | score %5.1f | %-5s | %s",
                 pair.base_symbol, (pair.market_cap or pair.fdv) / 1000,
                 analysis.score, analysis.signal,
                 ", ".join(analysis.reasons[:2]))

        if analysis.signal != "BUY":
            return

        # Vérifs de risque avant d'engager des appels de sécurité coûteux
        equity = self.portfolio.equity(self._price_cache)
        positions_value = self.portfolio.positions_value(self._price_cache)
        can, why = self.risk.can_open(
            equity, self.portfolio.cash,
            len(self.portfolio.positions), positions_value,
        )
        if not can:
            log.info("  ↳ entrée bloquée (risk) : %s", why)
            return

        # Filtre 2 : sécurité anti-rug / honeypot
        report = self.safety.check(pair)
        if not report.passed:
            log.info("  ↳ REJET sécurité : %s", "; ".join(report.reasons))
            return
        if report.reasons:
            log.info("  ↳ sécurité OK (notes : %s)", "; ".join(report.reasons))

        # Sizing + achat
        size = self.risk.position_size_usd(equity, self.portfolio.cash)
        if size < 1:
            return
        reason = f"score={analysis.score} | {report.top10_holders_pct or '?'}%top10"
        self.portfolio.buy(pair.base_address, pair.base_symbol,
                           pair.price_usd, size, reason)
        self._price_cache[pair.base_address] = pair.price_usd

    # ------------------------------------------------------------------
    #  Gestion des positions ouvertes
    # ------------------------------------------------------------------
    def manage_positions(self) -> None:
        if not self.portfolio.positions:
            return
        for addr, pos in list(self.portfolio.positions.items()):
            price = self._fresh_price(addr, pos.symbol)
            if price <= 0:
                continue
            self._price_cache[addr] = price

            # Mise à jour du plus haut (trailing)
            if price > pos.highest_price:
                pos.highest_price = price
                self.db.upsert_position(pos)

            reason, fraction = self.risk.evaluate_exit(pos, price)
            if reason:
                self.portfolio.sell(addr, price, fraction, reason)

        # Snapshot d'équité
        eq = self.portfolio.equity(self._price_cache)
        pv = self.portfolio.positions_value(self._price_cache)
        self.db.record_equity(eq, self.portfolio.cash, pv,
                              len(self.portfolio.positions))

    def _fresh_price(self, token_address: str, symbol: str) -> float:
        """Prix courant via DexScreener (paire la plus liquide du token)."""
        pairs = self.dex.get_token_pairs(token_address)
        if not pairs:
            return self._price_cache.get(token_address, 0.0)
        best = max(pairs, key=lambda p: p.liquidity_usd)
        return best.price_usd

    # ------------------------------------------------------------------
    #  Boucle principale
    # ------------------------------------------------------------------
    def run_once(self) -> None:
        """Un cycle complet (utile pour tests/cron)."""
        self.scan_and_trade()
        self.manage_positions()
        self._log_summary()

    def run_forever(self) -> None:
        log.info("Bot démarré en mode %s. Ctrl+C pour arrêter.",
                 self.cfg.mode.upper())
        try:
            while True:
                now = time.time()
                if now - self._last_scan >= self.scan_interval:
                    self.scan_and_trade()
                    self._last_scan = now
                self.manage_positions()
                self._log_summary()
                time.sleep(self.manage_interval)
        except KeyboardInterrupt:
            log.info("Arrêt demandé. Sauvegarde de l'état…")
        finally:
            self.shutdown()

    def _log_summary(self) -> None:
        eq = self.portfolio.equity(self._price_cache)
        start = self.cfg.get("risk.starting_balance_usd", 1000)
        pnl_pct = (eq - start) / start * 100 if start else 0
        log.info("💼 Équité %.2f$ (%+.1f%%) | cash %.2f$ | %d position(s) | "
                 "PnL réalisé %.2f$",
                 eq, pnl_pct, self.portfolio.cash,
                 len(self.portfolio.positions), self.portfolio.realized_pnl)

    def shutdown(self) -> None:
        self.db.close()
        log.info("État sauvegardé. À bientôt.")
