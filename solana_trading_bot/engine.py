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
from concurrent.futures import ThreadPoolExecutor

from .analysis.recommendation import Recommender
from .analysis.signals import SignalEngine
from .clients.birdeye import BirdeyeClient
from .clients.dexscreener import DexScreenerClient
from .clients.geckoterminal import GeckoTerminalClient
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
        self.gecko = GeckoTerminalClient()
        self.jupiter = JupiterClient()

        # Modules
        self.universe = UniverseFilter(config)
        self.safety = SafetyChecker(config, self.birdeye, self.jupiter)
        self.signals = SignalEngine(config)
        self.recommender = Recommender(config)
        self.risk = RiskManager(config)
        self.portfolio = Portfolio(config, self.db)
        log.info("Stratégie '%s' | risque '%s'",
                 config.active_strategy, config.active_risk_profile)

        self.scan_interval = config.get("loop.scan_interval_sec", 60)
        self.manage_interval = config.get("loop.manage_interval_sec", 15)
        self.max_tokens = config.get("loop.max_tokens_per_scan", 40)
        self.ohlcv_interval = config.get("analysis.ohlcv_interval", "15m")
        self.ohlcv_lookback = config.get("analysis.ohlcv_lookback", 200)
        self.workers = config.get("loop.workers", 8)

        self._last_scan = 0.0
        self._price_cache: dict[str, float] = {}

    # ------------------------------------------------------------------
    #  OHLCV : Birdeye (par token) -> repli GeckoTerminal (par pool)
    # ------------------------------------------------------------------
    def _get_ohlcv(self, pair: TokenPair):
        candles = None
        if self.birdeye.enabled:
            candles = self.birdeye.get_ohlcv(
                pair.base_address, self.ohlcv_interval, self.ohlcv_lookback
            )
        if not candles and pair.pair_address:
            candles = self.gecko.get_ohlcv(
                pair.pair_address, self.ohlcv_interval, self.ohlcv_lookback
            )
        return candles

    def _analyze_pair(self, pair: TokenPair, equity: float, cash: float,
                      positions_value: float) -> tuple:
        """Unité d'analyse READ-ONLY (sûre à paralléliser)."""
        candles = self._get_ohlcv(pair)
        analysis = self.signals.analyze(pair, candles)
        plan = self.recommender.evaluate(pair, analysis, equity, cash,
                                         positions_value)
        return pair, analysis, plan

    def _analyze_many(self, candidates: list[TokenPair], equity: float,
                      cash: float, positions_value: float) -> list[tuple]:
        """Analyse en parallèle (I/O-bound) puis tri par score décroissant."""
        if not candidates:
            return []
        workers = max(1, min(self.workers, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda p: self._analyze_pair(p, equity, cash, positions_value),
                candidates,
            ))
        results.sort(key=lambda t: t[1].score, reverse=True)
        return results

    # ------------------------------------------------------------------
    #  Découverte + filtre univers
    # ------------------------------------------------------------------
    def _eligible_candidates(self, skip_open: bool = True) -> list[TokenPair]:
        pairs = self.dex.discover()
        candidates = []
        for pair in pairs:
            if skip_open and pair.base_address in self.portfolio.positions:
                continue
            if self.universe.passes(pair)[0]:
                candidates.append(pair)
        candidates.sort(
            key=lambda p: (p.volume_24h * (1 + max(p.price_change_h1, 0) / 100)),
            reverse=True,
        )
        return candidates[: self.max_tokens]

    # ------------------------------------------------------------------
    #  Scan + trading
    # ------------------------------------------------------------------
    def scan_and_trade(self) -> None:
        t0 = time.time()
        log.info("─" * 60)
        log.info("SCAN du marché…")
        candidates = self._eligible_candidates()
        log.info("Filtre univers : %d candidat(s) éligible(s) (500k-3M MC)",
                 len(candidates))

        # Coupe-circuit journalier
        equity = self.portfolio.equity(self._price_cache)
        positions_value = self.portfolio.positions_value(self._price_cache)
        self.portfolio.roll_day_if_needed(self._price_cache)
        if self.risk.daily_circuit_breaker(self.portfolio.day_start_equity,
                                            equity):
            log.warning("Achats suspendus aujourd'hui (coupe-circuit).")
            return

        # Analyse parallèle (lecture seule), puis entrées séquentielles (sûres)
        analyzed = self._analyze_many(candidates, equity, self.portfolio.cash,
                                      positions_value)
        log.info("Analyse de %d token(s) en %.1fs", len(analyzed),
                 time.time() - t0)
        for pair, analysis, plan in analyzed:
            self._log_grade(pair, plan, analysis)
            if plan.is_actionable:
                self._execute_entry(pair, analysis, plan)

    def analyze_market(self) -> list[tuple]:
        """Analyse + note CHAQUE candidat sans trader (commande `rank`)."""
        candidates = self._eligible_candidates(skip_open=False)
        log.info("Notation de %d token(s) éligible(s)…", len(candidates))
        equity = self.portfolio.equity(self._price_cache)
        return self._analyze_many(candidates, equity, self.portfolio.cash, 0)

    def _log_grade(self, pair: TokenPair, plan, analysis) -> None:
        log.info("%-10s | MC %7.0fk | NOTE %-2s | score %5.1f | conf %4.1f | "
                 "%-10s | %s",
                 pair.base_symbol, (pair.market_cap or pair.fdv) / 1000,
                 plan.grade, plan.score, plan.confidence, plan.action,
                 ", ".join(analysis.reasons[:2]))

    def _execute_entry(self, pair: TokenPair, analysis, plan) -> None:
        """Décision d'entrée séquentielle : risque -> sécurité -> achat."""
        self._log_plan(pair, plan)
        if plan.size_usd < 1:
            log.info("  ↳ taille de position trop faible — pas d'entrée")
            return

        # Re-snapshot du risque (l'état a pu changer depuis l'analyse parallèle)
        equity = self.portfolio.equity(self._price_cache)
        positions_value = self.portfolio.positions_value(self._price_cache)
        can, why = self.risk.can_open(
            equity, self.portfolio.cash,
            len(self.portfolio.positions), positions_value,
        )
        if not can:
            log.info("  ↳ entrée bloquée (risk) : %s", why)
            return

        # Filtre 2 : sécurité anti-rug / honeypot + impact prix réel (Jupiter)
        report = self.safety.check(pair)
        if not report.passed:
            log.info("  ↳ REJET sécurité : %s", "; ".join(report.reasons))
            return
        if report.reasons:
            log.info("  ↳ sécurité OK (notes : %s)", "; ".join(report.reasons))

        # Impact prix réel pour un fill paper réaliste
        impact = self.jupiter.get_price_impact(pair.base_address, plan.size_usd)
        real_impact = impact["price_impact_pct"] if impact else None

        reason = (f"{plan.action} note={plan.grade} score={plan.score} "
                  f"R/R={plan.risk_reward}")
        self.portfolio.buy(pair.base_address, pair.base_symbol,
                           pair.price_usd, plan.size_usd, reason, plan=plan,
                           liquidity_usd=pair.liquidity_usd,
                           real_impact_pct=real_impact)
        self._price_cache[pair.base_address] = pair.price_usd

    def _log_plan(self, pair: TokenPair, plan) -> None:
        tps = " ".join(
            f"TP{i+1} {t['price']:.6g}(+{t['pct']:.0f}%/{int(t['portion']*100)}%)"
            for i, t in enumerate(plan.take_profits)
        )
        log.info("  ↳ PLAN %s | entrée %.6g | STOP %.6g (-%.1f%%) | %s",
                 plan.strategy, plan.entry_price, plan.stop_price,
                 plan.stop_pct, tps)
        log.info("  ↳ taille %.2f$ | R/R %.2f | détention %s | confiance %.0f%%",
                 plan.size_usd, plan.risk_reward, plan.est_hold, plan.confidence)

    # ------------------------------------------------------------------
    #  Gestion des positions ouvertes
    # ------------------------------------------------------------------
    def manage_positions(self) -> None:
        if not self.portfolio.positions:
            return
        for addr, pos in list(self.portfolio.positions.items()):
            price, liquidity = self._fresh_quote(addr)
            if price <= 0:
                continue
            self._price_cache[addr] = price

            # Mise à jour du plus haut (trailing)
            if price > pos.highest_price:
                pos.highest_price = price
                self.db.upsert_position(pos)

            reason, fraction = self.risk.evaluate_exit(pos, price)
            if reason:
                self.portfolio.sell(addr, price, fraction, reason,
                                    liquidity_usd=liquidity)
                # Palier de TP partiel atteint : on le retire pour ne pas
                # le redéclencher, et on persiste le reliquat.
                if (fraction < 1.0 and addr in self.portfolio.positions
                        and pos.tp_targets):
                    pos.tp_targets.pop(0)
                    self.db.upsert_position(self.portfolio.positions[addr])

        # Snapshot d'équité
        eq = self.portfolio.equity(self._price_cache)
        pv = self.portfolio.positions_value(self._price_cache)
        self.db.record_equity(eq, self.portfolio.cash, pv,
                              len(self.portfolio.positions))

    def _fresh_quote(self, token_address: str) -> tuple[float, float]:
        """(prix, liquidité) courants via DexScreener (pool le plus liquide)."""
        pairs = self.dex.get_token_pairs(token_address)
        if not pairs:
            return self._price_cache.get(token_address, 0.0), 0.0
        best = max(pairs, key=lambda p: p.liquidity_usd)
        return best.price_usd, best.liquidity_usd

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
