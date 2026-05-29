"""Client Birdeye — données OHLCV, sécurité du token, holders.

Nécessite une clé API (header `x-api-key`). Docs : https://docs.birdeye.so/
Si aucune clé n'est configurée, les méthodes renvoient None proprement et
le bot bascule sur des heuristiques dégradées (sans planter).
"""

from __future__ import annotations

import time
from typing import Optional

from ..logger import get_logger
from .http import HttpClient, HttpError

log = get_logger("birdeye")

# Correspondance intervalle config -> format Birdeye
_INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "4h": "4H", "1d": "1D",
}

# secondes par intervalle (pour calculer la fenêtre time_from)
_INTERVAL_SEC = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


class BirdeyeClient:
    BASE = "https://public-api.birdeye.so"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        headers = {"x-chain": "solana"}
        if api_key:
            headers["x-api-key"] = api_key
        self.http = HttpClient(self.BASE, default_headers=headers,
                               min_interval_sec=1.1)
        self.enabled = bool(api_key)
        if not self.enabled:
            log.warning(
                "Birdeye : aucune clé API (BIRDEYE_API_KEY). "
                "OHLCV et sécurité on-chain seront indisponibles."
            )

    def get_ohlcv(self, token_address: str, interval: str = "15m",
                  lookback: int = 200) -> Optional[list[dict]]:
        """Retourne une liste de bougies {o,h,l,c,v,unixTime} (ordre chrono)."""
        if not self.enabled:
            return None
        be_interval = _INTERVAL_MAP.get(interval, "15m")
        step = _INTERVAL_SEC.get(interval, 900)
        now = int(time.time())
        time_from = now - step * (lookback + 5)
        try:
            data = self.http.get(
                "/defi/ohlcv",
                params={
                    "address": token_address,
                    "type": be_interval,
                    "time_from": time_from,
                    "time_to": now,
                },
            )
        except HttpError as exc:
            log.debug("OHLCV %s échec : %s", token_address[:6], exc)
            return None
        items = ((data or {}).get("data") or {}).get("items") or []
        candles = []
        for it in items:
            try:
                candles.append({
                    "o": float(it["o"]), "h": float(it["h"]),
                    "l": float(it["l"]), "c": float(it["c"]),
                    "v": float(it.get("v", 0)), "t": int(it["unixTime"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return candles or None

    def get_token_security(self, token_address: str) -> Optional[dict]:
        """Infos sécurité : mint/freeze authority, top holders, etc."""
        if not self.enabled:
            return None
        try:
            data = self.http.get(
                "/defi/token_security", params={"address": token_address}
            )
        except HttpError as exc:
            log.debug("token_security %s échec : %s", token_address[:6], exc)
            return None
        return (data or {}).get("data")

    def get_token_overview(self, token_address: str) -> Optional[dict]:
        """Vue d'ensemble : holders, volume, liquidité, supply..."""
        if not self.enabled:
            return None
        try:
            data = self.http.get(
                "/defi/token_overview", params={"address": token_address}
            )
        except HttpError as exc:
            log.debug("token_overview %s échec : %s", token_address[:6], exc)
            return None
        return (data or {}).get("data")
