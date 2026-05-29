"""Tests de fumée : valident la logique cœur sans appels réseau réels."""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd

from solana_trading_bot.analysis import indicators
from solana_trading_bot.analysis.recommendation import Recommender, score_to_grade
from solana_trading_bot.analysis.signals import SignalEngine
from solana_trading_bot.config import Config
from solana_trading_bot.models import Position, TokenPair
from solana_trading_bot.storage.database import Database
from solana_trading_bot.trading.portfolio import Portfolio
from solana_trading_bot.trading.risk import RiskManager


def _config() -> Config:
    return Config.load("config.yaml")


def _fake_pair(mc=1_000_000, liq=80_000, vol=200_000, price=0.001) -> TokenPair:
    import time
    return TokenPair(
        pair_address="P", dex="raydium", base_address="MINT", base_symbol="TKN",
        quote_symbol="SOL", price_usd=price, market_cap=mc, fdv=mc,
        liquidity_usd=liq, volume_24h=vol, txns_24h=500,
        price_change_h1=2.0, price_change_h24=10.0,
        pair_created_ms=int((time.time() - 3600 * 48) * 1000),
    )


def _trending_candles(n=120, start=0.001, drift=0.004):
    rng = np.random.default_rng(42)
    price = start
    out = []
    t0 = 1_700_000_000
    for i in range(n):
        ret = drift + rng.normal(0, 0.01)
        new = max(price * (1 + ret), 1e-9)
        o, c = price, new
        h = max(o, c) * (1 + abs(rng.normal(0, 0.005)))
        low = min(o, c) * (1 - abs(rng.normal(0, 0.005)))
        out.append({"o": o, "h": h, "l": low, "c": c,
                    "v": float(rng.integers(1000, 5000)), "t": t0 + i * 900})
        price = new
    return out


def test_indicators_run():
    df = indicators.candles_to_df(_trending_candles())
    params = _config().get("analysis.indicators")
    df = indicators.compute_all(df, params)
    assert {"rsi", "macd_hist", "ema_fast", "bb_upper", "atr"} <= set(df.columns)
    assert 0 <= df["rsi"].iloc[-1] <= 100


def test_signal_uptrend_scores_high():
    eng = SignalEngine(_config())
    res = eng.analyze(_fake_pair(), _trending_candles(drift=0.006))
    assert 0 <= res.score <= 100
    assert res.signal in ("BUY", "HOLD", "AVOID")
    # Une tendance haussière franche doit scorer au-dessus de la neutralité
    assert res.score > 50


def test_signal_degraded_without_ohlcv():
    eng = SignalEngine(_config())
    res = eng.analyze(_fake_pair(), None)
    assert 0 <= res.score <= 100
    assert any("dégradée" in r for r in res.reasons)


def test_risk_sizing_and_exits():
    rm = RiskManager(_config())
    size = rm.position_size_usd(equity=1000, cash=1000)
    assert size > 0 and size <= rm.max_position_usd

    pos = Position("MINT", "TKN", entry_price=1.0, quantity=100, cost_usd=100)
    # Stop-loss
    reason, frac = rm.evaluate_exit(pos, price=1.0 - rm.stop_loss_pct / 100 - 0.01)
    assert reason == "stop-loss" and frac == 1.0
    # Take-profit partiel
    reason, frac = rm.evaluate_exit(pos, price=1.0 + rm.take_profit_pct / 100 + 0.01)
    assert reason == "take-profit-partiel" and 0 < frac < 1


def test_paper_portfolio_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "t.sqlite"))
        cfg = _config()
        pf = Portfolio(cfg, db)
        start_cash = pf.cash

        pos = pf.buy("MINT", "TKN", price=1.0, usd_amount=100, reason="test")
        assert pos is not None
        assert pf.cash < start_cash
        assert "MINT" in pf.positions

        # Vente totale à +20%
        pnl = pf.sell("MINT", price=1.2, fraction=1.0, reason="tp")
        assert "MINT" not in pf.positions
        # Doit être profitable malgré slippage + frais
        assert pnl > 0
        db.close()


def test_config_validation():
    cfg = _config()
    assert cfg.mode in ("paper", "live")
    assert cfg.get("universe.market_cap_min") < cfg.get("universe.market_cap_max")


def test_strategy_profiles_applied():
    cfg = _config()
    # Profil scalping actif -> timeframe court + paliers TP + sizing risque
    assert cfg.active_strategy == "scalping"
    assert cfg.get("analysis.ohlcv_interval") == "5m"
    assert isinstance(cfg.get("risk.take_profit_levels"), list)
    # Profil de risque 'moderate' s'applique par-dessus
    assert cfg.get("risk.risk_per_trade_pct") == 1.5


def test_grade_mapping():
    assert score_to_grade(95) == "A+"
    assert score_to_grade(81) == "A"
    assert score_to_grade(40) == "F"


def test_recommender_produces_graded_plan():
    cfg = _config()
    eng = SignalEngine(cfg)
    rec = Recommender(cfg)
    pair = _fake_pair(price=0.002)
    analysis = eng.analyze(pair, _trending_candles(drift=0.008))
    plan = rec.evaluate(pair, analysis, equity=1000, cash=1000,
                        positions_value=0)

    assert plan.grade in ("A+", "A", "B", "C", "D", "F")
    assert plan.action in ("STRONG_BUY", "BUY", "WATCH", "AVOID")
    assert 0 <= plan.confidence <= 100
    if plan.is_actionable:
        assert 0 < plan.stop_price < plan.entry_price
        prices = [t["price"] for t in plan.take_profits]
        assert prices == sorted(prices)          # paliers croissants
        assert all(p > plan.entry_price for p in prices)
        assert plan.size_usd > 0
        assert plan.risk_reward > 0


def test_plan_based_exits():
    rm = RiskManager(_config())
    # Position avec plan : stop à 0.9, deux paliers TP
    pos = Position("MINT", "TKN", entry_price=1.0, quantity=100, cost_usd=100,
                   stop_price=0.9,
                   tp_targets=[{"price": 1.1, "portion": 0.5},
                               {"price": 1.3, "portion": 1.0}])
    # Sous le stop -> sortie totale
    reason, frac = rm.evaluate_exit(pos, price=0.89)
    assert "stop-loss" in reason and frac == 1.0
    # Premier palier TP -> sortie partielle 50%
    reason, frac = rm.evaluate_exit(pos, price=1.15)
    assert "take-profit" in reason and frac == 0.5


def test_position_plan_persistence(tmp_path):
    from solana_trading_bot.models import TradePlan
    db = Database(str(tmp_path / "p.sqlite"))
    pf = Portfolio(_config(), db)
    plan = TradePlan(grade="A", action="BUY", score=80, confidence=70,
                     strategy="scalping", entry_price=1.0, stop_price=0.95,
                     take_profits=[{"price": 1.1, "portion": 0.5, "pct": 10},
                                   {"price": 1.25, "portion": 1.0, "pct": 25}])
    pf.buy("MINT", "TKN", price=1.0, usd_amount=100, plan=plan)
    # Recharge depuis la base : le plan doit être préservé
    reloaded = db.load_positions()["MINT"]
    assert reloaded.stop_price == 0.95
    assert len(reloaded.tp_targets) == 2
    assert reloaded.strategy == "scalping"
    db.close()
