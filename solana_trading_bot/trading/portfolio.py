"""Portefeuille : cash, positions, exécution paper (avec slippage + frais).

En mode paper, les ordres sont simulés au prix fourni, dégradé du slippage
et des frais configurés, pour un PnL réaliste. En mode live, l'exécution est
déléguée à Jupiter (stub volontairement non activé).
"""

from __future__ import annotations

import time

from ..logger import get_logger
from ..models import Position, Side, Trade
from ..storage.database import Database

log = get_logger("portfolio")


class Portfolio:
    def __init__(self, config, db: Database) -> None:
        self.cfg = config
        self.db = db
        self.mode = config.mode
        self.slippage_pct = config.get("risk.slippage_pct", 1.0)
        self.fee_pct = config.get("risk.fee_pct", 0.3)
        starting = config.get("risk.starting_balance_usd", 1000)

        # Reprise d'état si la base existe déjà
        self.cash: float = self.db.get_meta("cash", starting)
        self.positions: dict[str, Position] = self.db.load_positions()
        self.realized_pnl: float = self.db.get_meta("realized_pnl", 0.0)

        # Suivi journalier pour le coupe-circuit
        self.day_key: str = self.db.get_meta("day_key", _today())
        self.day_start_equity: float = self.db.get_meta(
            "day_start_equity", starting
        )

        if not self.db.get_meta("initialized"):
            self.db.set_meta("initialized", True)
            self.db.set_meta("cash", self.cash)
            log.info("Portefeuille initialisé : %.2f$ (mode %s)",
                     self.cash, self.mode)
        else:
            log.info("Portefeuille repris : cash=%.2f$, %d position(s)",
                     self.cash, len(self.positions))

    # ---------------- Helpers d'état ----------------
    def positions_value(self, prices: dict[str, float]) -> float:
        return sum(
            p.market_value(prices.get(addr, p.entry_price))
            for addr, p in self.positions.items()
        )

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.positions_value(prices)

    def _persist_cash(self) -> None:
        self.db.set_meta("cash", self.cash)
        self.db.set_meta("realized_pnl", self.realized_pnl)

    def roll_day_if_needed(self, prices: dict[str, float]) -> None:
        today = _today()
        if today != self.day_key:
            self.day_key = today
            self.day_start_equity = self.equity(prices)
            self.db.set_meta("day_key", today)
            self.db.set_meta("day_start_equity", self.day_start_equity)
            log.info("Nouveau jour — équité de référence : %.2f$",
                     self.day_start_equity)

    # ---------------- Exécution ----------------
    def buy(self, token_address: str, symbol: str, price: float,
            usd_amount: float, reason: str = "",
            plan=None) -> Position | None:
        if usd_amount <= 0 or price <= 0 or usd_amount > self.cash:
            log.warning("Achat refusé (%s) : montant=%.2f cash=%.2f",
                        symbol, usd_amount, self.cash)
            return None

        if self.mode == "live":
            raise NotImplementedError(
                "Exécution LIVE non activée — voir JupiterClient.execute_swap"
            )

        # Simulation paper : prix d'exécution dégradé + frais
        fill_price = price * (1 + self.slippage_pct / 100)
        fees = usd_amount * self.fee_pct / 100
        invested = usd_amount - fees
        quantity = invested / fill_price

        self.cash -= usd_amount
        pos = Position(
            token_address=token_address, symbol=symbol,
            entry_price=fill_price, quantity=quantity,
            cost_usd=invested, fees_paid_usd=fees,
        )
        # Attache les niveaux du plan de scalping (stop + paliers TP).
        if plan is not None:
            pos.stop_price = getattr(plan, "stop_price", 0.0) or 0.0
            pos.tp_targets = [
                {"price": t["price"], "portion": t["portion"]}
                for t in getattr(plan, "take_profits", [])
            ]
            pos.strategy = getattr(plan, "strategy", "") or ""
        self.positions[token_address] = pos
        self.db.upsert_position(pos)
        self._persist_cash()

        self.db.record_trade(Trade(
            timestamp=time.time(), token_address=token_address, symbol=symbol,
            side=Side.BUY, price=fill_price, quantity=quantity,
            value_usd=usd_amount, fees_usd=fees, reason=reason, mode=self.mode,
        ))
        log.info("[ACHAT] %s | %.2f$ @ %.8f | %s",
                 symbol, usd_amount, fill_price, reason)
        return pos

    def sell(self, token_address: str, price: float, fraction: float,
             reason: str = "") -> float:
        """Vend `fraction` (0-1) de la position. Retourne le PnL réalisé."""
        pos = self.positions.get(token_address)
        if not pos or price <= 0:
            return 0.0
        fraction = max(0.0, min(1.0, fraction))
        if fraction <= 0:
            return 0.0

        if self.mode == "live":
            raise NotImplementedError(
                "Exécution LIVE non activée — voir JupiterClient.execute_swap"
            )

        qty_sold = pos.quantity * fraction
        fill_price = price * (1 - self.slippage_pct / 100)
        gross = qty_sold * fill_price
        fees = gross * self.fee_pct / 100
        proceeds = gross - fees

        # Coût proportionnel de la portion vendue
        cost_portion = pos.cost_usd * fraction
        pnl = proceeds - cost_portion

        self.cash += proceeds
        self.realized_pnl += pnl

        # Mise à jour / clôture de la position
        if fraction >= 0.999:
            self.db.delete_position(token_address)
            del self.positions[token_address]
        else:
            pos.quantity -= qty_sold
            pos.cost_usd -= cost_portion
            pos.partial_taken = True
            pos.fees_paid_usd += fees
            self.db.upsert_position(pos)

        self._persist_cash()
        self.db.record_trade(Trade(
            timestamp=time.time(), token_address=token_address,
            symbol=pos.symbol, side=Side.SELL, price=fill_price,
            quantity=qty_sold, value_usd=proceeds, fees_usd=fees,
            pnl_usd=pnl, reason=reason, mode=self.mode,
        ))
        emoji = "🟢" if pnl >= 0 else "🔴"
        log.info("[VENTE %d%%] %s | %.2f$ @ %.8f | PnL %s%.2f$ | %s",
                 int(fraction * 100), pos.symbol, proceeds, fill_price,
                 emoji, pnl, reason)
        return pnl


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
