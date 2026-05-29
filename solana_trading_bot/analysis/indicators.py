"""Indicateurs techniques (implémentation pure pandas/numpy, sans TA-Lib)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def candles_to_df(candles: list[dict]) -> pd.DataFrame:
    """Convertit la liste de bougies Birdeye en DataFrame trié."""
    df = pd.DataFrame(candles)
    if df.empty:
        return df
    df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume", "t": "time"})
    df = df.sort_values("time").reset_index(drop=True)
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def macd(series: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(series: pd.Series, period: int = 20,
              n_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    upper = mid + n_std * std
    lower = mid - n_std * std
    return upper, mid, lower


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_all(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Ajoute toutes les colonnes d'indicateurs au DataFrame."""
    if df.empty or len(df) < 5:
        return df
    close = df["close"]
    df["ema_fast"] = ema(close, params["ema_fast"])
    df["ema_slow"] = ema(close, params["ema_slow"])
    df["ema_trend"] = ema(close, params["ema_trend"])
    df["rsi"] = rsi(close, params["rsi_period"])
    macd_line, signal_line, hist = macd(
        close, params["macd_fast"], params["macd_slow"], params["macd_signal"]
    )
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"] = hist
    upper, mid, lower = bollinger(close, params["bb_period"], params["bb_std"])
    df["bb_upper"] = upper
    df["bb_mid"] = mid
    df["bb_lower"] = lower
    df["atr"] = atr(df, params["atr_period"])
    # Moyenne de volume pour repérer les pics
    df["vol_ma"] = df["volume"].rolling(20).mean()
    return df
