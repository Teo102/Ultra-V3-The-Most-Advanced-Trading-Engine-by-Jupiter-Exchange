"""Gestion du risque : sizing, limites d'exposition, coupe-circuit, sorties."""

from __future__ import annotations

import time

from ..logger import get_logger
from ..models import Position

log = get_logger("risk")


class RiskManager:
    def __init__(self, config) -> None:
        r = config.get("risk")
        self.position_size_pct = r["position_size_pct"]
        self.max_position_usd = r["max_position_usd"]
        self.max_open_positions = r["max_open_positions"]
        self.max_total_exposure_pct = r["max_total_exposure_pct"]
        self.daily_loss_limit_pct = r["daily_loss_limit_pct"]
        self.take_profit_pct = r["take_profit_pct"]
        self.stop_loss_pct = r["stop_loss_pct"]
        self.trailing_stop_pct = r["trailing_stop_pct"]
        self.trailing_activation_pct = r["trailing_activation_pct"]
        self.partial_tp_pct = r["partial_tp_pct"]
        self.max_hold_hours = r["max_hold_hours"]

    # ---------------- Entrée ----------------
    def position_size_usd(self, equity: float, cash: float) -> float:
        size = equity * self.position_size_pct / 100
        size = min(size, self.max_position_usd, cash)
        return max(size, 0.0)

    def can_open(self, equity: float, cash: float, open_positions: int,
                 positions_value: float) -> tuple[bool, str]:
        if open_positions >= self.max_open_positions:
            return False, "max positions ouvertes atteint"
        exposure_pct = (positions_value / equity * 100) if equity else 100
        if exposure_pct >= self.max_total_exposure_pct:
            return False, f"exposition max atteinte ({exposure_pct:.0f}%)"
        if cash < 1:
            return False, "cash insuffisant"
        return True, ""

    def daily_circuit_breaker(self, day_start_equity: float,
                              equity: float) -> bool:
        """True si la perte journalière dépasse la limite -> stop achats."""
        if day_start_equity <= 0:
            return False
        drawdown = (day_start_equity - equity) / day_start_equity * 100
        if drawdown >= self.daily_loss_limit_pct:
            log.warning("COUPE-CIRCUIT : perte journalière %.1f%% >= %.1f%%",
                        drawdown, self.daily_loss_limit_pct)
            return True
        return False

    # ---------------- Sortie ----------------
    def evaluate_exit(self, pos: Position, price: float) -> tuple[str | None, float]:
        """Retourne (raison_sortie, fraction_à_vendre) ou (None, 0).

        Si la position porte un plan (stop_price / tp_targets absolus), on
        applique le plan ; sinon on retombe sur la logique en pourcentage.
        fraction = 1.0 => sortie totale ; <1 => sortie partielle.
        """
        # Sortie temporelle (commune aux deux modes)
        if pos.hold_hours() >= self.max_hold_hours:
            return "max-hold", 1.0

        if pos.stop_price or pos.tp_targets:
            return self._evaluate_plan_exit(pos, price)
        return self._evaluate_pct_exit(pos, price)

    def _evaluate_plan_exit(self, pos: Position,
                            price: float) -> tuple[str | None, float]:
        """Sorties pilotées par les niveaux absolus du plan de scalping."""
        # Stop-loss au prix planifié
        if pos.stop_price and price <= pos.stop_price:
            return "stop-loss (plan)", 1.0

        # Paliers de take-profit (le premier palier atteint est exécuté)
        if pos.tp_targets:
            target = pos.tp_targets[0]
            if price >= target["price"]:
                portion = float(target.get("portion", 1.0))
                last = len(pos.tp_targets) == 1
                reason = "take-profit (plan, final)" if last \
                    else f"take-profit (palier @{target['price']:.6g})"
                return reason, 1.0 if last else portion

        # Trailing stop sur le reliquat après un premier TP partiel
        if pos.partial_taken and pos.highest_price > 0:
            drop = (pos.highest_price - price) / pos.highest_price * 100
            if drop >= self.trailing_stop_pct:
                return "trailing-stop", 1.0

        return None, 0.0

    def _evaluate_pct_exit(self, pos: Position,
                           price: float) -> tuple[str | None, float]:
        pnl_pct = pos.unrealized_pnl_pct(price)

        if pnl_pct <= -self.stop_loss_pct:
            return "stop-loss", 1.0

        if not pos.partial_taken and pnl_pct >= self.take_profit_pct:
            return "take-profit-partiel", self.partial_tp_pct / 100

        if pnl_pct >= self.trailing_activation_pct:
            drop_from_high = (pos.highest_price - price) / pos.highest_price * 100
            if drop_from_high >= self.trailing_stop_pct:
                return "trailing-stop", 1.0

        return None, 0.0
