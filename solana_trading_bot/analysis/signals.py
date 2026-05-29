"""Moteur de scoring : transforme les indicateurs en un score 0-100 + signal.

Score composite pondéré sur 5 dimensions :
  - trend            (alignement des EMA, prix vs EMA de tendance)
  - momentum         (RSI, MACD histogram, variation court terme)
  - volume           (pic de volume vs moyenne, activité)
  - volatility       (position dans les bandes de Bollinger, ATR sain)
  - liquidity_health (volume/liquidité, profondeur du marché)

Chaque composante renvoie un score 0-100, agrégé selon les poids config.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..models import AnalysisResult, TokenPair
from . import indicators


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


def _score_trend(df: pd.DataFrame) -> tuple[float, list[str]]:
    last = df.iloc[-1]
    reasons: list[str] = []
    score = 50.0
    # EMA rapide > lente > tendance = uptrend propre
    if last["ema_fast"] > last["ema_slow"] > last["ema_trend"]:
        score += 30
        reasons.append("EMA alignées haussières")
    elif last["ema_fast"] < last["ema_slow"] < last["ema_trend"]:
        score -= 30
        reasons.append("EMA alignées baissières")
    # Prix au-dessus de l'EMA de tendance
    if last["close"] > last["ema_trend"]:
        score += 12
    else:
        score -= 12
    # Pente récente de l'EMA rapide
    if len(df) >= 4:
        slope = (df["ema_fast"].iloc[-1] - df["ema_fast"].iloc[-4])
        if slope > 0:
            score += 8
        else:
            score -= 8
    return _clip(score), reasons


def _score_momentum(df: pd.DataFrame) -> tuple[float, list[str]]:
    last = df.iloc[-1]
    reasons: list[str] = []
    score = 50.0
    rsi = last["rsi"]
    # Zone idéale 50-70 (momentum sans surachat extrême)
    if 50 <= rsi <= 70:
        score += 22
        reasons.append(f"RSI sain ({rsi:.0f})")
    elif 40 <= rsi < 50:
        score += 5
    elif rsi > 80:
        score -= 20
        reasons.append(f"RSI surachat ({rsi:.0f})")
    elif rsi < 30:
        score -= 5
        reasons.append(f"RSI survente ({rsi:.0f})")
    # MACD histogram positif et croissant
    if last["macd_hist"] > 0:
        score += 15
        if len(df) >= 2 and df["macd_hist"].iloc[-1] > df["macd_hist"].iloc[-2]:
            score += 8
            reasons.append("MACD haussier accélère")
    else:
        score -= 12
    return _clip(score), reasons


def _score_volume(df: pd.DataFrame) -> tuple[float, list[str]]:
    last = df.iloc[-1]
    reasons: list[str] = []
    score = 45.0
    vol_ma = last.get("vol_ma", np.nan)
    if vol_ma and not np.isnan(vol_ma) and vol_ma > 0:
        ratio = last["volume"] / vol_ma
        if ratio >= 2.0:
            score += 35
            reasons.append(f"Pic de volume x{ratio:.1f}")
        elif ratio >= 1.2:
            score += 18
        elif ratio < 0.5:
            score -= 15
            reasons.append("Volume en baisse")
    return _clip(score), reasons


def _score_volatility(df: pd.DataFrame) -> tuple[float, list[str]]:
    last = df.iloc[-1]
    reasons: list[str] = []
    score = 50.0
    upper, lower, close = last["bb_upper"], last["bb_lower"], last["close"]
    if upper and lower and upper > lower:
        pos = (close - lower) / (upper - lower)  # position dans les bandes
        # On préfère un breakout contrôlé (haut des bandes mais pas explosé)
        if 0.6 <= pos <= 0.95:
            score += 25
            reasons.append("Cassure haussière des bandes")
        elif pos > 1.05:
            score -= 15
            reasons.append("Extension excessive (risque de mèche)")
        elif pos < 0.2:
            score -= 5
    # ATR relatif : volatilité présente mais pas folle
    if last["close"] > 0:
        atr_pct = last["atr"] / last["close"] * 100
        if 2 <= atr_pct <= 15:
            score += 10
        elif atr_pct > 30:
            score -= 15
            reasons.append("Volatilité extrême")
    return _clip(score), reasons


def _score_liquidity_health(pair: TokenPair) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 40.0
    ratio = pair.vol_liq_ratio
    # Activité saine : le pool tourne sans être en wash-trading
    if 1 <= ratio <= 10:
        score += 35
        reasons.append("Ratio volume/liquidité sain")
    elif 0.5 <= ratio < 1:
        score += 15
    elif ratio > 25:
        score -= 20
        reasons.append("Ratio vol/liq suspect (pump?)")
    # Bonus liquidité absolue
    if pair.liquidity_usd >= 100_000:
        score += 15
    elif pair.liquidity_usd >= 50_000:
        score += 8
    # Momentum de prix court terme cohérent
    if pair.price_change_h1 > 0 and pair.price_change_h24 > 0:
        score += 10
    return _clip(score), reasons


class SignalEngine:
    def __init__(self, config) -> None:
        self.params = config.get("analysis.indicators")
        self.weights = config.get("analysis.weights")
        self.threshold = config.get("analysis.entry_score_threshold", 68)

    def analyze(self, pair: TokenPair,
                candles: list[dict] | None) -> AnalysisResult:
        reasons: list[str] = []
        components: dict[str, float] = {}

        # Liquidité : toujours évaluable (DexScreener)
        liq_score, liq_reasons = _score_liquidity_health(pair)
        components["liquidity_health"] = liq_score
        reasons += liq_reasons

        df = indicators.candles_to_df(candles or [])
        have_ta = not df.empty and len(df) >= max(
            self.params["ema_trend"], self.params["bb_period"]
        )

        if have_ta:
            df = indicators.compute_all(df, self.params)
            for name, fn in (
                ("trend", _score_trend),
                ("momentum", _score_momentum),
                ("volume", _score_volume),
                ("volatility", _score_volatility),
            ):
                s, r = fn(df)
                components[name] = s
                reasons += r
            last = df.iloc[-1]
            indicators_snapshot = {
                "rsi": round(float(last["rsi"]), 1),
                "macd_hist": round(float(last["macd_hist"]), 8),
                "ema_fast": round(float(last["ema_fast"]), 8),
                "ema_slow": round(float(last["ema_slow"]), 8),
                "close": round(float(last["close"]), 8),
                "atr_pct": round(float(last["atr"] / last["close"] * 100), 2)
                if last["close"] else 0,
            }
        else:
            # Pas d'OHLCV (pas de clé Birdeye ou token trop récent) :
            # on retombe sur le momentum DexScreener, score neutre prudent.
            reasons.append("OHLCV indisponible — analyse dégradée (DexScreener)")
            mom = 50.0
            if pair.price_change_h1 > 0:
                mom += 10
            if pair.price_change_h24 > 0:
                mom += 10
            if pair.price_change_h1 < -5:
                mom -= 15
            components.update({
                "trend": _clip(mom), "momentum": _clip(mom),
                "volume": 45.0, "volatility": 45.0,
            })
            indicators_snapshot = {
                "price_change_h1": pair.price_change_h1,
                "price_change_h24": pair.price_change_h24,
            }

        # Agrégation pondérée
        total_w = sum(self.weights.values()) or 1.0
        score = sum(
            components.get(k, 50.0) * w for k, w in self.weights.items()
        ) / total_w

        if score >= self.threshold:
            signal = "BUY"
        elif score >= self.threshold - 15:
            signal = "HOLD"
        else:
            signal = "AVOID"

        return AnalysisResult(
            score=round(score, 1),
            signal=signal,
            components={k: round(v, 1) for k, v in components.items()},
            indicators=indicators_snapshot,
            reasons=reasons,
        )
