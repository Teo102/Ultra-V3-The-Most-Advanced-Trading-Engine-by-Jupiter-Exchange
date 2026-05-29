"""Backtester : rejoue la stratégie sur l'historique OHLCV.

Pour chaque token (pool), on avance bougie par bougie. À chaque pas :
  - on (re)calcule les indicateurs sur la fenêtre connue jusqu'ici ;
  - on demande au moteur de signal + au recommandeur une note + un plan ;
  - on gère la position ouverte avec les **mèches intra-bougie** (high/low),
    pour déclencher stop/TP de façon réaliste ;
  - les fills passent par le même `FillModel` que le paper trading.

En sortie : rendement total, winrate, drawdown max, profit factor, Sharpe,
nombre de trades — de quoi VALIDER une stratégie avant le réel.

Limites assumées : backtest mono-actif (pas de corrélation inter-tokens),
liquidité supposée constante, pas de simulation de rug en cours de route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .analysis.recommendation import Recommender
from .analysis.signals import SignalEngine
from .config import Config
from .logger import get_logger
from .models import TokenPair
from .trading.execution import FillModel

log = get_logger("backtest")

_INTERVAL_SEC = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


@dataclass
class BacktestTrade:
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_usd: float
    pnl_pct: float
    reason: str


@dataclass
class BacktestResult:
    symbol: str
    interval: str
    n_candles: int
    n_trades: int = 0
    wins: int = 0
    win_rate_pct: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe: float = 0.0
    final_equity: float = 0.0
    buy_hold_pct: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)


class Backtester:
    def __init__(self, config: Config, liquidity_usd: float = 100_000.0):
        self.cfg = config
        self.signals = SignalEngine(config)
        self.recommender = Recommender(config)
        self.starting = config.get("risk.starting_balance_usd", 1000)
        self.fill = FillModel(
            base_slippage_pct=config.get("risk.slippage_pct", 1.0),
            fee_pct=config.get("risk.fee_pct", 0.3),
            impact_k=config.get("risk.impact_k", 0.6),
        )
        self.trailing_stop_pct = config.get("risk.trailing_stop_pct", 10)
        self.max_hold_hours = config.get("risk.max_hold_hours", 48)
        self.liquidity = liquidity_usd

        ind = config.get("analysis.indicators")
        self.warmup = max(ind["ema_trend"], ind["bb_period"],
                          ind["macd_slow"]) + 5

    # ------------------------------------------------------------------
    def run(self, symbol: str, candles: list[dict],
            interval: str = "15m") -> BacktestResult:
        res = BacktestResult(symbol=symbol, interval=interval,
                             n_candles=len(candles))
        if len(candles) <= self.warmup + 5:
            log.warning("%s : pas assez de bougies (%d) pour backtester",
                        symbol, len(candles))
            res.final_equity = self.starting
            return res

        bar_sec = _INTERVAL_SEC.get(interval, 900)
        max_hold_bars = max(1, int(self.max_hold_hours * 3600 / bar_sec))

        cash = self.starting
        pos = None              # dict: qty, entry_price, cost, stop, tps, high, opened_i, partial
        equity_curve = [cash]

        for i in range(self.warmup, len(candles)):
            window = candles[: i + 1]
            bar = candles[i]
            close, high, low = bar["c"], bar["h"], bar["l"]

            if pos is not None:
                cash_delta, closed = self._manage(pos, bar, i, max_hold_bars, res)
                cash += cash_delta
                if closed:
                    pos = None
            else:
                pair = self._synthetic_pair(symbol, window)
                analysis = self.signals.analyze(pair, window)
                equity = cash
                plan = self.recommender.evaluate(pair, analysis, equity, cash, 0)
                if plan.is_actionable and plan.size_usd >= 1 and cash >= 1:
                    pos = self._open(plan, close, bar["t"], i, cash)
                    cash -= pos["cost_total"]

            mark = cash + (pos["qty"] * close if pos else 0)
            equity_curve.append(mark)

        # Clôture finale au dernier prix
        if pos is not None:
            last = candles[-1]
            cash += self._close(pos, last["c"], last["t"], "fin-backtest", res)
            equity_curve.append(cash)

        self._finalize(res, cash, equity_curve, candles)
        return res

    # ------------------------------------------------------------------
    def _open(self, plan, price: float, t: int, idx: int, cash: float) -> dict:
        size = min(plan.size_usd, cash)
        fill_price, fees = self.fill.buy_fill(price, size, self.liquidity)
        invested = size - fees
        qty = invested / fill_price
        return {
            "qty": qty, "entry_price": fill_price, "cost": invested,
            "cost_total": size, "stop": plan.stop_price,
            "tps": [dict(t) for t in plan.take_profits],
            "high": fill_price, "opened_i": idx, "opened_t": t,
            "partial": False,
        }

    def _manage(self, pos: dict, bar: dict, idx: int, max_hold_bars: int,
                res: BacktestResult) -> tuple[float, bool]:
        """Retourne (delta_cash, position_fermée). Une vente partielle crédite
        immédiatement le cash et garde la position ouverte."""
        high, low, close = bar["h"], bar["l"], bar["c"]
        pos["high"] = max(pos["high"], high)

        # 1) Stop-loss (mèche basse touche le stop)
        if pos["stop"] and low <= pos["stop"]:
            return self._close(pos, pos["stop"], bar["t"], "stop-loss", res), True

        # 2) Sortie temporelle
        if idx - pos["opened_i"] >= max_hold_bars:
            return self._close(pos, close, bar["t"], "max-hold", res), True

        # 3) Paliers de take-profit (mèche haute atteint le palier)
        if pos["tps"]:
            target = pos["tps"][0]
            if high >= target["price"]:
                if len(pos["tps"]) == 1:
                    return self._close(pos, target["price"], bar["t"],
                                       "take-profit (final)", res), True
                portion = float(target.get("portion", 0.5))
                credit = self._partial(pos, target["price"], portion,
                                       bar["t"], res)
                pos["tps"].pop(0)
                pos["partial"] = True
                return credit, False

        # 4) Trailing stop sur le reliquat après un premier TP
        if pos["partial"] and pos["high"] > 0:
            drop = (pos["high"] - close) / pos["high"] * 100
            if drop >= self.trailing_stop_pct:
                return self._close(pos, close, bar["t"], "trailing-stop", res), True

        return 0.0, False

    def _partial(self, pos: dict, price: float, portion: float, t: int,
                 res: BacktestResult) -> float:
        """Vend une fraction du reliquat. Retourne le produit (crédité au cash)
        et enregistre la vente comme un trade à part entière."""
        qty_sold = pos["qty"] * portion
        gross = qty_sold * price
        fill_price, fees = self.fill.sell_fill(price, gross, self.liquidity)
        proceeds = qty_sold * fill_price - fees
        cost_portion = pos["cost"] * portion
        pnl = proceeds - cost_portion
        res.trades.append(BacktestTrade(
            entry_time=pos["opened_t"], exit_time=t,
            entry_price=pos["entry_price"], exit_price=fill_price, qty=qty_sold,
            pnl_usd=pnl, pnl_pct=(pnl / cost_portion * 100) if cost_portion else 0,
            reason="take-profit (palier)",
        ))
        pos["qty"] -= qty_sold
        pos["cost"] -= cost_portion
        return proceeds

    def _close(self, pos: dict, price: float, t: int, reason: str,
               res: BacktestResult) -> float:
        qty = pos["qty"]
        gross = qty * price
        fill_price, fees = self.fill.sell_fill(price, gross, self.liquidity)
        proceeds = qty * fill_price - fees
        pnl = proceeds - pos["cost"]
        res.trades.append(BacktestTrade(
            entry_time=pos["opened_t"], exit_time=t,
            entry_price=pos["entry_price"], exit_price=fill_price, qty=qty,
            pnl_usd=pnl, pnl_pct=(pnl / pos["cost"] * 100) if pos["cost"] else 0,
            reason=reason,
        ))
        return proceeds

    # ------------------------------------------------------------------
    def _synthetic_pair(self, symbol: str, window: list[dict]) -> TokenPair:
        """Construit une paire synthétique pour l'analyse (prix = close)."""
        close = window[-1]["c"]
        # Volume ~24h et variations dérivés des bougies récentes
        last24 = window[-96:] if len(window) >= 96 else window
        vol_24h = sum(c["v"] for c in last24) * close
        h1 = self._pct_change(window, 4)
        h24 = self._pct_change(window, 96)
        return TokenPair(
            pair_address="", dex="backtest", base_address="", base_symbol=symbol,
            quote_symbol="USDC", price_usd=close, market_cap=1_500_000,
            fdv=1_500_000, liquidity_usd=self.liquidity,
            volume_24h=max(vol_24h, 1), txns_24h=500,
            price_change_h1=h1, price_change_h24=h24, pair_created_ms=0,
        )

    @staticmethod
    def _pct_change(window: list[dict], n: int) -> float:
        if len(window) <= n:
            return 0.0
        old = window[-n - 1]["c"]
        return (window[-1]["c"] - old) / old * 100 if old else 0.0

    def _finalize(self, res: BacktestResult, cash: float,
                  equity_curve: list[float], candles: list[dict]) -> None:
        res.final_equity = round(cash, 2)
        res.total_return_pct = round((cash - self.starting) / self.starting * 100, 2)
        res.n_trades = len(res.trades)
        wins = [t for t in res.trades if t.pnl_usd > 0]
        losses = [t for t in res.trades if t.pnl_usd <= 0]
        res.wins = len(wins)
        res.win_rate_pct = round(len(wins) / res.n_trades * 100, 1) if res.n_trades else 0.0
        gross_win = sum(t.pnl_usd for t in wins)
        gross_loss = abs(sum(t.pnl_usd for t in losses))
        res.profit_factor = round(gross_win / gross_loss, 2) if gross_loss else (
            float("inf") if gross_win > 0 else 0.0)

        # Drawdown max sur la courbe d'équité
        peak = equity_curve[0]
        max_dd = 0.0
        for v in equity_curve:
            peak = max(peak, v)
            dd = (peak - v) / peak * 100 if peak else 0
            max_dd = max(max_dd, dd)
        res.max_drawdown_pct = round(max_dd, 2)

        # Sharpe sur les rendements de barre (annualisation simple omise)
        rets = []
        for a, b in zip(equity_curve, equity_curve[1:]):
            if a > 0:
                rets.append((b - a) / a)
        if len(rets) > 1:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            res.sharpe = round(mean / std * math.sqrt(len(rets)), 2) if std else 0.0

        # Performance buy & hold sur la même période (référence)
        first, last = candles[self.warmup]["c"], candles[-1]["c"]
        res.buy_hold_pct = round((last - first) / first * 100, 2) if first else 0.0
