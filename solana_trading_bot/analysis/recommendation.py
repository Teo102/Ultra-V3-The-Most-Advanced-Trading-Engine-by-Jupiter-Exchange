"""Moteur de recommandation : note + plan d'action concret par token.

Transforme un `AnalysisResult` (+ contexte de marché et de risque) en un
`TradePlan` exploitable :
  - une **note** (A+ → F) synthétisant la qualité du setup ;
  - une **action** (STRONG_BUY / BUY / WATCH / AVOID) ;
  - pour les setups actionnables : **niveaux de scalping** (entrée, stop,
    paliers de take-profit), **taille de position calculée sur le risque**,
    ratio **risque/récompense** et durée de détention estimée.

Les niveaux sont pilotés par le profil de stratégie actif et adaptés à la
volatilité réelle (ATR) quand l'OHLCV est disponible.
"""

from __future__ import annotations

from ..models import AnalysisResult, TokenPair, TradePlan

# Barème de notation (score composite -> lettre)
_GRADE_BANDS = [
    (88, "A+"), (80, "A"), (72, "B"), (63, "C"), (52, "D"), (0, "F"),
]


def score_to_grade(score: float) -> str:
    for threshold, letter in _GRADE_BANDS:
        if score >= threshold:
            return letter
    return "F"


class Recommender:
    def __init__(self, config) -> None:
        self.cfg = config
        self.strategy = config.active_strategy
        self.threshold = config.get("analysis.entry_score_threshold", 68)

        r = config.get("risk")
        self.stop_loss_pct = r["stop_loss_pct"]
        self.max_position_usd = r["max_position_usd"]
        self.position_size_pct = r["position_size_pct"]
        # Sizing basé sur le risque (scalping) : risque max par trade en %
        self.risk_per_trade_pct = r.get("risk_per_trade_pct", 0)
        self.max_total_exposure_pct = r["max_total_exposure_pct"]
        self.max_hold_hours = r["max_hold_hours"]

        # Paliers de take-profit du profil (sinon dérivés du TP unique).
        self.tp_levels = r.get("take_profit_levels") or [
            {"pct": r["take_profit_pct"], "portion": r["partial_tp_pct"] / 100},
            {"pct": r["take_profit_pct"] * 1.8, "portion": 1.0},
        ]
        # Adaptation à la volatilité
        self.use_atr_stops = r.get("use_atr_stops", False)
        self.atr_stop_mult = r.get("atr_stop_mult", 1.5)

    # ------------------------------------------------------------------
    def evaluate(self, pair: TokenPair, analysis: AnalysisResult,
                 equity: float, cash: float,
                 positions_value: float) -> TradePlan:
        score = analysis.score
        grade = score_to_grade(score)
        rationale = list(analysis.reasons[:4])

        action = self._decide_action(score, analysis.signal)

        plan = TradePlan(
            grade=grade, action=action, score=score,
            confidence=self._confidence(analysis),
            strategy=self.strategy, rationale=rationale,
        )
        if not plan.is_actionable or pair.price_usd <= 0:
            return plan

        # --- Niveaux de scalping ---
        entry = pair.price_usd
        stop_pct = self._effective_stop_pct(analysis)
        stop_price = entry * (1 - stop_pct / 100)

        take_profits = []
        for lvl in self.tp_levels:
            tp_pct = float(lvl["pct"])
            take_profits.append({
                "pct": round(tp_pct, 2),
                "portion": float(lvl["portion"]),
                "price": entry * (1 + tp_pct / 100),
            })

        # Ratio risque/récompense (TP moyen pondéré vs stop)
        avg_tp_pct = sum(t["pct"] * t["portion"] for t in take_profits) / max(
            sum(t["portion"] for t in take_profits), 1e-9
        )
        risk_reward = round(avg_tp_pct / stop_pct, 2) if stop_pct else 0.0

        # --- Taille de position ---
        size = self._position_size(equity, cash, positions_value, stop_pct)

        plan.entry_price = entry
        plan.stop_price = stop_price
        plan.take_profits = take_profits
        plan.size_usd = size
        plan.risk_reward = risk_reward
        plan.est_hold = self._format_hold()
        plan.rationale.append(
            f"R/R≈{risk_reward} | stop {stop_pct:.1f}% | "
            f"{len(take_profits)} paliers TP"
        )
        return plan

    # ------------------------------------------------------------------
    def _decide_action(self, score: float, signal: str) -> str:
        if signal == "AVOID":
            return "AVOID"
        if score >= self.threshold + 10:
            return "STRONG_BUY"
        if score >= self.threshold:
            return "BUY"
        if score >= self.threshold - 12:
            return "WATCH"
        return "AVOID"

    @staticmethod
    def _confidence(analysis: AnalysisResult) -> float:
        """Confiance = cohérence entre les composantes (faible dispersion)."""
        comps = list(analysis.components.values())
        if not comps:
            return 50.0
        mean = sum(comps) / len(comps)
        var = sum((c - mean) ** 2 for c in comps) / len(comps)
        std = var ** 0.5
        # Beaucoup de dispersion => signaux contradictoires => moins de confiance
        conf = max(0.0, min(100.0, 100 - std))
        # Pondère par le niveau du score lui-même
        return round((conf * 0.5) + (analysis.score * 0.5), 1)

    def _effective_stop_pct(self, analysis: AnalysisResult) -> float:
        stop = self.stop_loss_pct
        if self.use_atr_stops:
            atr_pct = analysis.indicators.get("atr_pct")
            if atr_pct:
                stop = max(stop, atr_pct * self.atr_stop_mult)
        return round(stop, 2)

    def _position_size(self, equity: float, cash: float,
                       positions_value: float, stop_pct: float) -> float:
        # Sizing basé sur le risque si configuré (recommandé pour le scalping)
        if self.risk_per_trade_pct and stop_pct > 0:
            risk_amount = equity * self.risk_per_trade_pct / 100
            size = risk_amount / (stop_pct / 100)
        else:
            size = equity * self.position_size_pct / 100

        # Plafonds : position max, cash dispo, marge d'exposition restante
        exposure_room = max(
            equity * self.max_total_exposure_pct / 100 - positions_value, 0
        )
        size = min(size, self.max_position_usd, cash, exposure_room)
        return round(max(size, 0.0), 2)

    def _format_hold(self) -> str:
        h = self.max_hold_hours
        if h < 1:
            return f"~{int(h * 60)}min"
        if h <= 6:
            return f"~{h:.0f}h (scalp court)"
        if h <= 24:
            return f"~{h:.0f}h (intraday)"
        return f"~{h / 24:.0f}j (swing)"
