"""Statistiques de performance à partir de l'historique des trades."""

from __future__ import annotations

from .storage.database import Database


def compute_stats(db: Database) -> dict:
    trades = db.all_trades()
    sells = [t for t in trades if t["side"] == "SELL"]
    buys = [t for t in trades if t["side"] == "BUY"]

    pnls = [t["pnl_usd"] for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    total_fees = sum(t["fees_usd"] for t in trades)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    stats = {
        "n_buys": len(buys),
        "n_sells": len(sells),
        "total_realized_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "cash": round(db.get_meta("cash", 0.0), 2),
        "realized_pnl_meta": round(db.get_meta("realized_pnl", 0.0), 2),
    }
    return stats


_ACTION_STYLE = {
    "STRONG_BUY": "bold green", "BUY": "green",
    "WATCH": "yellow", "AVOID": "dim red",
}


def print_rankings(rows: list[tuple]) -> None:
    """Affiche le tableau de notation des tokens (pair, analysis, plan)."""
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="🏅 Notation des tokens (500k-3M MC)",
                      show_header=True, header_style="bold cyan")
        for col, just in (("Token", "left"), ("MC", "right"), ("Note", "center"),
                          ("Score", "right"), ("Conf", "right"),
                          ("Action", "center"), ("Entrée", "right"),
                          ("Stop", "right"), ("R/R", "right"),
                          ("Taille$", "right"), ("Setup", "left")):
            table.add_column(col, justify=just)

        for pair, analysis, plan in rows:
            mc = (pair.market_cap or pair.fdv) / 1000
            style = _ACTION_STYLE.get(plan.action, "white")
            entry = f"{plan.entry_price:.6g}" if plan.entry_price else "—"
            stop = f"{plan.stop_price:.6g}" if plan.stop_price else "—"
            rr = f"{plan.risk_reward:.2f}" if plan.risk_reward else "—"
            size = f"{plan.size_usd:.0f}" if plan.size_usd else "—"
            table.add_row(
                pair.base_symbol, f"{mc:.0f}k", f"[{style}]{plan.grade}[/]",
                f"{plan.score:.0f}", f"{plan.confidence:.0f}",
                f"[{style}]{plan.action}[/]", entry, stop, rr, size,
                ", ".join(analysis.reasons[:2])[:48],
            )
        Console().print(table)
    except Exception:
        print("\n=== Notation des tokens ===")
        for pair, analysis, plan in rows:
            print(f"  {pair.base_symbol:10s} note={plan.grade:2s} "
                  f"score={plan.score:5.1f} {plan.action:10s} "
                  f"R/R={plan.risk_reward}")


def print_report(db: Database) -> None:
    stats = compute_stats(db)
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="📊 Performance du bot", show_header=True)
        table.add_column("Métrique", style="cyan")
        table.add_column("Valeur", justify="right", style="bold")
        for k, v in stats.items():
            table.add_row(k, str(v))
        Console().print(table)
    except Exception:
        print("\n=== Performance du bot ===")
        for k, v in stats.items():
            print(f"  {k:24s}: {v}")
