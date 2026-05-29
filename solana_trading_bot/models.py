"""Structures de données partagées dans tout le bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class TokenPair:
    """Représentation normalisée d'une paire de trading (issue de DexScreener)."""

    pair_address: str
    dex: str
    base_address: str          # mint du token tradé
    base_symbol: str
    quote_symbol: str
    price_usd: float
    market_cap: float
    fdv: float
    liquidity_usd: float
    volume_24h: float
    txns_24h: int
    price_change_h1: float
    price_change_h24: float
    pair_created_ms: int
    url: str = ""

    @property
    def age_hours(self) -> float:
        if not self.pair_created_ms:
            return 0.0
        return (time.time() * 1000 - self.pair_created_ms) / 3_600_000

    @property
    def vol_liq_ratio(self) -> float:
        return self.volume_24h / self.liquidity_usd if self.liquidity_usd else 0.0


@dataclass
class SafetyReport:
    """Résultat des contrôles anti-rug / honeypot."""

    passed: bool
    mint_revoked: Optional[bool] = None
    freeze_revoked: Optional[bool] = None
    top10_holders_pct: Optional[float] = None
    holders: Optional[int] = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """Sortie du moteur d'analyse technique pour un token."""

    score: float                       # 0-100
    signal: str                        # BUY | HOLD | AVOID
    components: dict[str, float] = field(default_factory=dict)
    indicators: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass
class Position:
    """Position ouverte (paper ou live)."""

    token_address: str
    symbol: str
    entry_price: float
    quantity: float                    # quantité de tokens détenue
    cost_usd: float                    # capital investi (hors frais)
    opened_at: float = field(default_factory=time.time)
    highest_price: float = 0.0         # pour le trailing stop
    partial_taken: bool = False
    fees_paid_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.highest_price == 0.0:
            self.highest_price = self.entry_price

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl_usd(self, price: float) -> float:
        return self.market_value(price) - self.cost_usd

    def unrealized_pnl_pct(self, price: float) -> float:
        if self.cost_usd == 0:
            return 0.0
        return (self.market_value(price) - self.cost_usd) / self.cost_usd * 100

    def hold_hours(self) -> float:
        return (time.time() - self.opened_at) / 3600


@dataclass
class Trade:
    """Trade exécuté (entrée ou sortie), pour journalisation/PnL."""

    timestamp: float
    token_address: str
    symbol: str
    side: Side
    price: float
    quantity: float
    value_usd: float
    fees_usd: float
    pnl_usd: float = 0.0               # rempli sur les sorties
    reason: str = ""
    mode: str = "paper"
