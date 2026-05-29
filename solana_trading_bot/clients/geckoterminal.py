"""Client GeckoTerminal — OHLCV public et gratuit (sans clé API).

Sert de source d'analyse technique par défaut / de repli à Birdeye :
l'OHLCV est récupéré **par adresse de pool** (= pair_address DexScreener).
Docs : https://www.geckoterminal.com/dex-api  (~30 req/min en gratuit)
"""

from __future__ import annotations

from typing import Optional

from ..logger import get_logger
from .http import HttpClient, HttpError

log = get_logger("geckoterminal")

# Intervalle config -> (timeframe GeckoTerminal, aggregate)
_INTERVAL_MAP = {
    "1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15),
    "30m": ("minute", 30), "1h": ("hour", 1), "4h": ("hour", 4),
    "1d": ("day", 1),
}


class GeckoTerminalClient:
    BASE = "https://api.geckoterminal.com/api/v2"

    def __init__(self, network: str = "solana") -> None:
        self.network = network
        # ~30 req/min en gratuit -> on espace d'environ 2.1s.
        self.http = HttpClient(
            self.BASE,
            default_headers={"Accept": "application/json"},
            min_interval_sec=2.1,
        )
        self.enabled = True

    def get_ohlcv(self, pool_address: str, interval: str = "15m",
                  lookback: int = 200) -> Optional[list[dict]]:
        """Bougies {o,h,l,c,v,t} en ordre chronologique, par adresse de pool."""
        timeframe, aggregate = _INTERVAL_MAP.get(interval, ("minute", 15))
        limit = min(max(lookback, 1), 1000)
        try:
            data = self.http.get(
                f"/networks/{self.network}/pools/{pool_address}/ohlcv/{timeframe}",
                params={"aggregate": aggregate, "limit": limit,
                        "currency": "usd"},
            )
        except HttpError as exc:
            log.debug("OHLCV pool %s échec : %s", pool_address[:6], exc)
            return None

        ohlcv = (((data or {}).get("data") or {}).get("attributes") or {}).get(
            "ohlcv_list"
        )
        if not ohlcv:
            return None

        candles = []
        for row in ohlcv:
            try:
                ts, o, h, low, c, v = row[:6]
                candles.append({
                    "o": float(o), "h": float(h), "l": float(low),
                    "c": float(c), "v": float(v or 0), "t": int(ts),
                })
            except (ValueError, TypeError, IndexError):
                continue
        # GeckoTerminal renvoie du plus récent au plus ancien : on remet en ordre
        candles.sort(key=lambda c: c["t"])
        return candles or None
