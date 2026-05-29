"""Filtres en deux étages :

1) `passes_universe`  — éligibilité market-cap + liquidité (rapide, DexScreener)
2) `safety_check`     — anti-rug / honeypot (plus coûteux : Birdeye + Jupiter)

L'étage 1 sert à réduire des centaines de paires à une poignée de candidats
avant de dépenser des appels API coûteux sur l'étage 2.
"""

from __future__ import annotations

from ..clients.birdeye import BirdeyeClient
from ..clients.jupiter import JupiterClient
from ..logger import get_logger
from ..models import SafetyReport, TokenPair

log = get_logger("safety")


class UniverseFilter:
    """Étage 1 : filtres market-cap + liquidité (sans appel coûteux)."""

    def __init__(self, config) -> None:
        self.mc_min = config.get("universe.market_cap_min")
        self.mc_max = config.get("universe.market_cap_max")
        self.quote_symbols = set(config.get("universe.quote_symbols", []))
        liq = config.get("liquidity")
        self.liq = liq
        self.blacklist_tokens = set(config.get("safety.blacklist_tokens", []))

    def passes(self, pair: TokenPair) -> tuple[bool, list[str]]:
        reasons: list[str] = []

        if pair.base_address in self.blacklist_tokens:
            return False, ["token blacklisté"]

        if not pair.base_address or pair.price_usd <= 0:
            return False, ["données prix invalides"]

        # Market cap (fallback FDV si MC absent, fréquent sur les small caps)
        mc = pair.market_cap or pair.fdv
        if mc <= 0:
            return False, ["market cap inconnu"]
        if not (self.mc_min <= mc <= self.mc_max):
            return False, [f"MC hors cible ({mc:,.0f}$)"]

        if self.quote_symbols and pair.quote_symbol not in self.quote_symbols:
            return False, [f"quote non autorisée ({pair.quote_symbol})"]

        if pair.liquidity_usd < self.liq["min_liquidity_usd"]:
            return False, [f"liquidité faible ({pair.liquidity_usd:,.0f}$)"]

        if pair.volume_24h < self.liq["min_volume_24h_usd"]:
            return False, [f"volume 24h faible ({pair.volume_24h:,.0f}$)"]

        ratio = pair.vol_liq_ratio
        if not (self.liq["min_vol_liq_ratio"] <= ratio <= self.liq["max_vol_liq_ratio"]):
            return False, [f"ratio vol/liq anormal ({ratio:.1f})"]

        if pair.txns_24h < self.liq["min_txns_24h"]:
            return False, [f"trop peu de txns ({pair.txns_24h})"]

        age = pair.age_hours
        if age < self.liq["min_age_hours"]:
            return False, [f"paire trop récente ({age:.1f}h)"]
        if age > self.liq["max_age_hours"]:
            return False, [f"paire trop ancienne ({age/24:.0f}j)"]

        return True, reasons


class SafetyChecker:
    """Étage 2 : anti-rug / honeypot via Birdeye (on-chain) + Jupiter (route)."""

    def __init__(self, config, birdeye: BirdeyeClient,
                 jupiter: JupiterClient) -> None:
        self.cfg = config.get("safety")
        self.birdeye = birdeye
        self.jupiter = jupiter
        self.entry_probe_usd = max(
            config.get("risk.max_position_usd", 100), 50
        )

    def check(self, pair: TokenPair) -> SafetyReport:
        report = SafetyReport(passed=True)
        reasons: list[str] = []

        # --- 2a) Contrôle de route de vente (anti-honeypot, via Jupiter) ---
        impact = self.jupiter.get_price_impact(pair.base_address,
                                                self.entry_probe_usd)
        if impact is None:
            report.passed = False
            reasons.append("aucune route Jupiter (illiquide/honeypot probable)")
            report.reasons = reasons
            return report

        report_impact = impact["price_impact_pct"]
        if report_impact > self.cfg["max_price_impact_pct"]:
            report.passed = False
            reasons.append(
                f"impact prix achat trop élevé ({report_impact:.1f}%)"
            )

        # Test de revente : on simule la vente du montant qu'on recevrait
        out_amount = impact.get("out_amount", 0)
        if out_amount > 0 and not self.jupiter.can_sell(pair.base_address,
                                                        out_amount):
            report.passed = False
            reasons.append("revente impossible (HONEYPOT détecté)")

        # --- 2b) Sécurité on-chain via Birdeye (si clé dispo) ---
        sec = self.birdeye.get_token_security(pair.base_address)
        if sec is not None:
            mint_revoked = _is_revoked(sec.get("mintAuthority"))
            freeze_revoked = _is_revoked(sec.get("freezeAuthority"))
            report.mint_revoked = mint_revoked
            report.freeze_revoked = freeze_revoked

            if self.cfg["require_mint_revoked"] and mint_revoked is False:
                report.passed = False
                reasons.append("mint authority active (mint infini possible)")
            if self.cfg["require_freeze_revoked"] and freeze_revoked is False:
                report.passed = False
                reasons.append("freeze authority active (gel des comptes)")

            top10 = sec.get("top10HolderPercent")
            if top10 is not None:
                pct = float(top10) * 100 if top10 <= 1 else float(top10)
                report.top10_holders_pct = round(pct, 1)
                if pct > self.cfg["max_top10_holders_pct"]:
                    report.passed = False
                    reasons.append(f"concentration top10 élevée ({pct:.0f}%)")
        else:
            reasons.append("sécurité on-chain non vérifiée (Birdeye indispo)")

        # --- 2c) Holders via overview ---
        overview = self.birdeye.get_token_overview(pair.base_address)
        if overview is not None:
            holders = overview.get("holder") or overview.get("holders")
            if holders is not None:
                report.holders = int(holders)
                if int(holders) < self.cfg["min_holders"]:
                    report.passed = False
                    reasons.append(f"trop peu de holders ({holders})")

        report.reasons = reasons
        return report


def _is_revoked(authority) -> bool | None:
    """None/'' => révoqué (True). Une adresse => actif (False)."""
    if authority is None:
        return True
    if isinstance(authority, str) and authority.strip() == "":
        return True
    return False
