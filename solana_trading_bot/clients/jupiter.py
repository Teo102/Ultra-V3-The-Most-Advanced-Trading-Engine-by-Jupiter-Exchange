"""Client Jupiter — quotes de swap et (en live) exécution.

Quote API publique : https://lite-api.jup.ag / https://quote-api.jup.ag
On l'utilise pour :
  - obtenir un prix exécutable réel + price impact (essentiel pour estimer
    le slippage et détecter les honeypots/illiquidités)
  - construire/envoyer un swap en mode LIVE (stub sécurisé fourni)
"""

from __future__ import annotations

from typing import Optional

from ..logger import get_logger
from .http import HttpClient, HttpError

log = get_logger("jupiter")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class JupiterClient:
    # Endpoint "lite" public, sans clé.
    BASE = "https://lite-api.jup.ag"

    def __init__(self) -> None:
        self.http = HttpClient(self.BASE, min_interval_sec=0.3)

    def get_quote(self, input_mint: str, output_mint: str, amount: int,
                  slippage_bps: int = 150) -> Optional[dict]:
        """Quote brut Jupiter. `amount` en plus petite unité de input_mint."""
        try:
            data = self.http.get(
                "/swap/v1/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": amount,
                    "slippageBps": slippage_bps,
                    "restrictIntermediateTokens": "true",
                },
            )
        except HttpError as exc:
            log.debug("quote %s->%s échec : %s",
                      input_mint[:5], output_mint[:5], exc)
            return None
        return data

    def get_price_impact(self, token_mint: str, usdc_amount: float) -> Optional[dict]:
        """Estime l'impact prix d'un achat de `usdc_amount` $ du token.

        Renvoie {price_impact_pct, out_amount, route_ok}. Sert de proxy de
        liquidité réelle et de détection d'illiquidité/honeypot côté achat.
        """
        amount = int(usdc_amount * 1_000_000)  # USDC a 6 décimales
        quote = self.get_quote(USDC, token_mint, amount)
        if not quote or "outAmount" not in quote:
            return None
        try:
            impact = float(quote.get("priceImpactPct") or 0) * 100
        except (TypeError, ValueError):
            impact = 0.0
        return {
            "price_impact_pct": impact,
            "out_amount": int(quote["outAmount"]),
            "route_ok": True,
        }

    def can_sell(self, token_mint: str, token_amount_raw: int) -> bool:
        """Vérifie qu'une route de vente existe (anti-honeypot côté vente)."""
        quote = self.get_quote(token_mint, USDC, token_amount_raw)
        return bool(quote and int(quote.get("outAmount") or 0) > 0)

    # ------------------------------------------------------------------
    #  Exécution LIVE — volontairement laissée en stub sécurisé.
    #  Activer nécessite solana-py/solders + signature de la transaction.
    # ------------------------------------------------------------------
    def execute_swap(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError(
            "L'exécution LIVE n'est pas activée. Le bot tourne en paper "
            "trading. Implémente la signature de transaction (solders + "
            "/swap/v1/swap) et retire ce garde-fou en connaissance de cause."
        )
