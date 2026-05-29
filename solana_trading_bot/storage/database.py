"""Couche de persistance SQLite : trades, positions ouvertes, courbe d'équité."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from ..logger import get_logger
from ..models import Position, Trade, Side

log = get_logger("db")


class Database:
    def __init__(self, path: str = "bot_data.sqlite") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL, token_address TEXT, symbol TEXT,
                side TEXT, price REAL, quantity REAL, value_usd REAL,
                fees_usd REAL, pnl_usd REAL, reason TEXT, mode TEXT
            );
            CREATE TABLE IF NOT EXISTS positions (
                token_address TEXT PRIMARY KEY,
                symbol TEXT, entry_price REAL, quantity REAL, cost_usd REAL,
                opened_at REAL, highest_price REAL, partial_taken INTEGER,
                fees_paid_usd REAL,
                stop_price REAL DEFAULT 0,
                tp_targets TEXT DEFAULT '[]',
                strategy TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS equity_curve (
                timestamp REAL PRIMARY KEY, equity_usd REAL,
                cash_usd REAL, positions_value_usd REAL, open_positions INTEGER
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT
            );
            """
        )
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Ajoute les colonnes manquantes aux bases créées par d'anciennes
        versions (migration idempotente)."""
        cols = {row["name"] for row in
                self.conn.execute("PRAGMA table_info(positions)").fetchall()}
        for name, ddl in (
            ("stop_price", "REAL DEFAULT 0"),
            ("tp_targets", "TEXT DEFAULT '[]'"),
            ("strategy", "TEXT DEFAULT ''"),
        ):
            if name not in cols:
                self.conn.execute(
                    f"ALTER TABLE positions ADD COLUMN {name} {ddl}"
                )

    # ---------------- Meta (état persistant) ----------------
    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    # ---------------- Trades ----------------
    def record_trade(self, trade: Trade) -> None:
        self.conn.execute(
            "INSERT INTO trades(timestamp,token_address,symbol,side,price,"
            "quantity,value_usd,fees_usd,pnl_usd,reason,mode) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (trade.timestamp, trade.token_address, trade.symbol,
             trade.side.value, trade.price, trade.quantity, trade.value_usd,
             trade.fees_usd, trade.pnl_usd, trade.reason, trade.mode),
        )
        self.conn.commit()

    def all_trades(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY timestamp"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- Positions ----------------
    def upsert_position(self, p: Position) -> None:
        self.conn.execute(
            "INSERT INTO positions(token_address,symbol,entry_price,quantity,"
            "cost_usd,opened_at,highest_price,partial_taken,fees_paid_usd,"
            "stop_price,tp_targets,strategy) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(token_address) DO UPDATE SET "
            "symbol=excluded.symbol, entry_price=excluded.entry_price, "
            "quantity=excluded.quantity, cost_usd=excluded.cost_usd, "
            "highest_price=excluded.highest_price, "
            "partial_taken=excluded.partial_taken, "
            "fees_paid_usd=excluded.fees_paid_usd, "
            "stop_price=excluded.stop_price, tp_targets=excluded.tp_targets, "
            "strategy=excluded.strategy",
            (p.token_address, p.symbol, p.entry_price, p.quantity, p.cost_usd,
             p.opened_at, p.highest_price, int(p.partial_taken), p.fees_paid_usd,
             p.stop_price, json.dumps(p.tp_targets), p.strategy),
        )
        self.conn.commit()

    def delete_position(self, token_address: str) -> None:
        self.conn.execute(
            "DELETE FROM positions WHERE token_address=?", (token_address,)
        )
        self.conn.commit()

    def load_positions(self) -> dict[str, Position]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        out: dict[str, Position] = {}
        for r in rows:
            keys = r.keys()
            out[r["token_address"]] = Position(
                token_address=r["token_address"], symbol=r["symbol"],
                entry_price=r["entry_price"], quantity=r["quantity"],
                cost_usd=r["cost_usd"], opened_at=r["opened_at"],
                highest_price=r["highest_price"],
                partial_taken=bool(r["partial_taken"]),
                fees_paid_usd=r["fees_paid_usd"],
                stop_price=r["stop_price"] if "stop_price" in keys else 0.0,
                tp_targets=json.loads(r["tp_targets"])
                if "tp_targets" in keys and r["tp_targets"] else [],
                strategy=r["strategy"] if "strategy" in keys else "",
            )
        return out

    # ---------------- Équité ----------------
    def record_equity(self, equity: float, cash: float,
                      positions_value: float, open_positions: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_curve VALUES(?,?,?,?,?)",
            (time.time(), equity, cash, positions_value, open_positions),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
