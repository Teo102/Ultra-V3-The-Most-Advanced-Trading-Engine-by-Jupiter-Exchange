"""Modèle de fill réaliste pour le paper trading.

Sur une small cap illiquide, le slippage n'est PAS un pourcentage fixe : il
croît avec le ratio taille_du_trade / liquidité du pool. Ce modèle estime un
slippage dynamique, et accepte l'impact prix réel mesuré par Jupiter quand il
est disponible (prioritaire, car c'est la vraie route exécutable).

    slippage% ≈ base% + k · (montant_trade / liquidité) · 100      (borné)

Le résultat alimente le prix d'exécution et les frais simulés, pour un PnL
paper bien plus proche du réel.
"""

from __future__ import annotations


class FillModel:
    def __init__(self, base_slippage_pct: float, fee_pct: float,
                 impact_k: float = 0.6, max_slippage_pct: float = 25.0):
        self.base = base_slippage_pct
        self.fee_pct = fee_pct
        self.impact_k = impact_k
        self.max_slippage = max_slippage_pct

    def slippage_pct(self, trade_usd: float, liquidity_usd: float,
                     real_impact_pct: float | None = None) -> float:
        """Slippage estimé pour ce trade (en %)."""
        if real_impact_pct is not None and real_impact_pct > 0:
            # Impact Jupiter réel + une petite marge de base (frais réseau/priorité)
            est = real_impact_pct + self.base
        elif liquidity_usd and liquidity_usd > 0:
            est = self.base + self.impact_k * (trade_usd / liquidity_usd) * 100
        else:
            est = self.base
        return min(est, self.max_slippage)

    def buy_fill(self, mid_price: float, usd_amount: float,
                 liquidity_usd: float,
                 real_impact_pct: float | None = None) -> tuple[float, float]:
        """Retourne (prix_d_execution, frais_usd) pour un achat."""
        slip = self.slippage_pct(usd_amount, liquidity_usd, real_impact_pct)
        fill_price = mid_price * (1 + slip / 100)
        fees = usd_amount * self.fee_pct / 100
        return fill_price, fees

    def sell_fill(self, mid_price: float, usd_value: float,
                  liquidity_usd: float) -> tuple[float, float]:
        """Retourne (prix_d_execution, frais_usd) pour une vente."""
        slip = self.slippage_pct(usd_value, liquidity_usd)
        fill_price = mid_price * (1 - slip / 100)
        fees = usd_value * self.fee_pct / 100
        return fill_price, fees
