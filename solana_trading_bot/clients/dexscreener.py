"""Client DexScreener — découverte de paires et données de marché.

API publique, gratuite, sans clé. Docs : https://docs.dexscreener.com/
On l'utilise pour :
  - découvrir des paires Solana actives (recherche + boosts/profils)
  - récupérer market cap, liquidité, volume, txns, variations de prix
"""

from __future__ import annotations

from typing import Optional

from ..logger import get_logger
from ..models import TokenPair
from .http import HttpClient, HttpError

log = get_logger("dexscreener")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class DexScreenerClient:
    BASE = "https://api.dexscreener.com"

    def __init__(self) -> None:
        # DexScreener tolère ~300 req/min ; on reste prudent.
        self.http = HttpClient(self.BASE, min_interval_sec=0.25)

    # ---------- Parsing ----------
    @staticmethod
    def _parse_pair(raw: dict) -> Optional[TokenPair]:
        try:
            if raw.get("chainId") != "solana":
                return None
            base = raw.get("baseToken", {})
            quote = raw.get("quoteToken", {})
            liq = (raw.get("liquidity") or {}).get("usd", 0) or 0
            vol = (raw.get("volume") or {}).get("h24", 0) or 0
            txns = raw.get("txns") or {}
            txn_h24 = txns.get("h24") or {}
            n_txns = int((txn_h24.get("buys") or 0) + (txn_h24.get("sells") or 0))
            pc = raw.get("priceChange") or {}

            return TokenPair(
                pair_address=raw.get("pairAddress", ""),
                dex=raw.get("dexId", "?"),
                base_address=base.get("address", ""),
                base_symbol=base.get("symbol", "?"),
                quote_symbol=quote.get("symbol", "?"),
                price_usd=float(raw.get("priceUsd") or 0),
                market_cap=float(raw.get("marketCap") or 0),
                fdv=float(raw.get("fdv") or 0),
                liquidity_usd=float(liq),
                volume_24h=float(vol),
                txns_24h=n_txns,
                price_change_h1=float(pc.get("h1") or 0),
                price_change_h24=float(pc.get("h24") or 0),
                pair_created_ms=int(raw.get("pairCreatedAt") or 0),
                url=raw.get("url", ""),
            )
        except (TypeError, ValueError) as exc:
            log.debug("Parse paire échoué : %s", exc)
            return None

    # ---------- Endpoints ----------
    def search(self, query: str) -> list[TokenPair]:
        """Recherche libre (ex: 'SOL', 'USDC', un symbole...)."""
        try:
            data = self.http.get("/latest/dex/search", params={"q": query})
        except HttpError as exc:
            log.warning("search('%s') a échoué : %s", query, exc)
            return []
        pairs = (data or {}).get("pairs") or []
        out = [p for p in (self._parse_pair(r) for r in pairs) if p]
        return out

    def get_pair(self, pair_address: str) -> Optional[TokenPair]:
        try:
            data = self.http.get(f"/latest/dex/pairs/solana/{pair_address}")
        except HttpError:
            return None
        pairs = (data or {}).get("pairs") or []
        return self._parse_pair(pairs[0]) if pairs else None

    def get_token_pairs(self, token_address: str) -> list[TokenPair]:
        """Toutes les paires pour un mint donné."""
        try:
            data = self.http.get(f"/latest/dex/tokens/{token_address}")
        except HttpError:
            return []
        # Cet endpoint renvoie soit {"pairs": [...]}, soit directement [...]
        if isinstance(data, dict):
            pairs = data.get("pairs") or []
        elif isinstance(data, list):
            pairs = data
        else:
            pairs = []
        return [p for p in (self._parse_pair(r) for r in pairs) if p]

    def _discover_addresses(self) -> list[str]:
        """Mints Solana émergents via token-profiles + token-boosts.

        Ces endpoints surfacent des tokens récents/promus, bien plus
        susceptibles d'être dans la cible 500k-3M qu'une recherche générique.
        """
        endpoints = [
            "/token-profiles/latest/v1",
            "/token-boosts/latest/v1",
            "/token-boosts/top/v1",
        ]
        addresses: list[str] = []
        seen: set[str] = set()
        for ep in endpoints:
            try:
                data = self.http.get(ep)
            except HttpError as exc:
                log.debug("discover %s échec : %s", ep, exc)
                continue
            for item in data or []:
                if not isinstance(item, dict):
                    continue
                if item.get("chainId") != "solana":
                    continue
                addr = item.get("tokenAddress")
                if addr and addr not in seen:
                    seen.add(addr)
                    addresses.append(addr)
        return addresses

    def discover(self, queries: Optional[list[str]] = None) -> list[TokenPair]:
        """Découverte de paires Solana ciblant les small caps.

        DexScreener n'expose pas de 'screener' filtré côté serveur ; on
        combine deux sources puis on filtre côté bot :
          1) tokens émergents (profiles + boosts) -> leurs paires
          2) recherches génériques (complément de couverture)
        """
        seen: dict[str, TokenPair] = {}

        # 1) Tokens émergents -> paires détaillées
        for addr in self._discover_addresses():
            for pair in self.get_token_pairs(addr):
                if pair.pair_address and pair.pair_address not in seen:
                    seen[pair.pair_address] = pair

        # 2) Complément par recherche générique
        for q in (queries or ["SOL", "USDC"]):
            for pair in self.search(q):
                if pair.pair_address and pair.pair_address not in seen:
                    seen[pair.pair_address] = pair

        log.info("DexScreener : %d paires Solana uniques découvertes", len(seen))
        return list(seen.values())
