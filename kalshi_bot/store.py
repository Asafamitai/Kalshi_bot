"""SQLite state: everything the bot must remember across restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from .ladder import Violation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    side TEXT NOT NULL,
    safer TEXT NOT NULL,
    dominated TEXT NOT NULL,
    ask TEXT NOT NULL,
    saving TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
-- Last time each option (contract + side) was alerted, for the cooldown.
CREATE TABLE IF NOT EXISTS sent (
    key TEXT PRIMARY KEY,
    sent_at REAL NOT NULL
);
-- A violation is an episode from the first scan that sees it to the first that doesn't.
CREATE TABLE IF NOT EXISTS episodes_open (
    key TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    safer TEXT NOT NULL,
    dominated TEXT NOT NULL,
    opened_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    first_saving TEXT NOT NULL,
    max_saving TEXT NOT NULL,
    max_net_saving TEXT NOT NULL,
    alertable INTEGER NOT NULL DEFAULT 0,
    -- Prices when the current pair of rungs first contradicted: the safer ask, the dominated mid.
    ref_safer_ask TEXT,
    ref_dominated_mid TEXT
);
CREATE TABLE IF NOT EXISTS episodes_closed (
    key TEXT NOT NULL,
    side TEXT NOT NULL,
    safer TEXT NOT NULL,
    dominated TEXT NOT NULL,
    opened_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    closed_at REAL NOT NULL,
    first_saving TEXT NOT NULL,
    max_saving TEXT NOT NULL,
    max_net_saving TEXT NOT NULL,
    alertable INTEGER NOT NULL,
    ref_safer_ask TEXT,
    ref_dominated_mid TEXT,
    close_safer_ask TEXT,
    close_dominated_mid TEXT,
    -- safer (its ask rose) | dominated (its mid fell) | both | expired | book | unknown
    closed_by TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    at REAL NOT NULL,
    seconds REAL NOT NULL,
    markets INTEGER NOT NULL,
    ladders INTEGER NOT NULL,
    violations INTEGER NOT NULL,
    alertable INTEGER NOT NULL,
    sent INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    at REAL NOT NULL,
    alert_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    price TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    result TEXT NOT NULL
);
-- Paper trading: what each strategy would have bought, one row per trade and per contract in it.
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    opened_at REAL NOT NULL,
    event TEXT NOT NULL,
    side TEXT NOT NULL,
    safer TEXT NOT NULL,
    dominated TEXT NOT NULL,
    saving TEXT NOT NULL,
    bid_through INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at REAL,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS paper_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    price TEXT NOT NULL,
    cost TEXT NOT NULL,
    mark TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price TEXT,
    payout TEXT,
    closed_at REAL
);
"""

# Columns added after the first release; a database created before then gets them on open.
_ADDED_COLUMNS = {
    "episodes_open": ("ref_safer_ask", "ref_dominated_mid"),
    "episodes_closed": ("ref_safer_ask", "ref_dominated_mid", "close_safer_ask", "close_dominated_mid", "closed_by"),
}

_EPISODE_COLS = ("key", "side", "safer", "dominated", "opened_at", "last_seen", "first_saving",
                 "max_saving", "max_net_saving", "alertable", "ref_safer_ask", "ref_dominated_mid")
_CLOSE_COLS = ("closed_at", "close_safer_ask", "close_dominated_mid", "closed_by")


@dataclass(frozen=True)
class Alert:
    id: int
    side: str
    safer: str
    dominated: str
    ask: Decimal
    saving: Decimal
    status: str


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            for table, cols in _ADDED_COLUMNS.items():
                have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                for col in cols:
                    if col not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

    def _write(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, args)

    def _read(self, sql: str, args=()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # Alerts behind buy buttons.

    def add_alert(self, v: Violation) -> int:
        cur = self._write(
            "INSERT INTO alerts (created_at, side, safer, dominated, ask, saving) VALUES (?,?,?,?,?,?)",
            (time.time(), v.side, v.safer.ticker, v.dominated.ticker, str(v.ask), str(v.saving)),
        )
        return cur.lastrowid

    def get_alert(self, alert_id: int) -> Alert | None:
        rows = self._read(
            "SELECT id, side, safer, dominated, ask, saving, status FROM alerts WHERE id = ?", (alert_id,)
        )
        if not rows:
            return None
        r = rows[0]
        return Alert(r[0], r[1], r[2], r[3], Decimal(r[4]), Decimal(r[5]), r[6])

    def claim_alert(self, alert_id: int) -> bool:
        """Mark an open alert as being bought; False if it already was (double tap)."""
        cur = self._write("UPDATE alerts SET status = 'buying' WHERE id = ? AND status = 'open'", (alert_id,))
        return cur.rowcount == 1

    def set_status(self, alert_id: int, status: str) -> None:
        self._write("UPDATE alerts SET status = ? WHERE id = ?", (status, alert_id))

    # Cooldown per option.

    def last_sent(self, key: str) -> float | None:
        rows = self._read("SELECT sent_at FROM sent WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def mark_sent(self, key: str, at: float) -> None:
        self._write("INSERT OR REPLACE INTO sent (key, sent_at) VALUES (?, ?)", (key, at))

    # Episodes.

    def open_episodes(self) -> dict[str, dict]:
        rows = self._read(f"SELECT {', '.join(_EPISODE_COLS)} FROM episodes_open")
        return {r[0]: dict(zip(_EPISODE_COLS, r)) for r in rows}

    def save_episodes(self, upserts: list[dict], closes: list[dict], closed_at: float) -> None:
        """`closes` carry their closing prices and `closed_by` as well as the episode fields."""
        open_sql = (f"INSERT OR REPLACE INTO episodes_open ({', '.join(_EPISODE_COLS)}) "
                    f"VALUES ({', '.join('?' * len(_EPISODE_COLS))})")
        cols = _EPISODE_COLS + _CLOSE_COLS
        close_sql = f"INSERT INTO episodes_closed ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        with self._lock, self._conn:
            for e in upserts:
                self._conn.execute(open_sql, [e.get(c) for c in _EPISODE_COLS])
            for e in closes:
                self._conn.execute("DELETE FROM episodes_open WHERE key = ?", (e["key"],))
                self._conn.execute(close_sql, [e.get(c) for c in _EPISODE_COLS] + [closed_at] +
                                   [e.get(c) for c in _CLOSE_COLS[1:]])

    def closed_episodes(self, since: float = 0) -> list[dict]:
        cols = ["key", "side", "opened_at", "last_seen", "closed_at", "max_saving", "max_net_saving", "alertable",
                "ref_safer_ask", "ref_dominated_mid", "close_safer_ask", "close_dominated_mid", "closed_by"]
        rows = self._read(f"SELECT {', '.join(cols)} FROM episodes_closed WHERE closed_at >= ?", (since,))
        return [dict(zip(cols, r)) for r in rows]

    # Scan log, settings, orders.

    def log_scan(self, **row) -> None:
        self._write(
            "INSERT INTO scans VALUES (?,?,?,?,?,?,?)",
            (row["at"], row["seconds"], row["markets"], row["ladders"], row["violations"],
             row["alertable"], row["sent"]),
        )

    def last_scan(self) -> dict | None:
        rows = self._read("SELECT at, seconds, markets, ladders, violations, alertable, sent "
                          "FROM scans ORDER BY at DESC LIMIT 1")
        if not rows:
            return None
        return dict(zip(["at", "seconds", "markets", "ladders", "violations", "alertable", "sent"], rows[0]))

    def sent_since(self, since: float) -> int:
        return self._read("SELECT COALESCE(SUM(sent), 0) FROM scans WHERE at >= ?", (since,))[0][0]

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        rows = self._read("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0][0] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._write("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    @property
    def trading_on(self) -> bool:
        return self.get_setting("trading", "off") == "on"

    def log_order(self, alert_id: int, ticker: str, side: str, count: int, price: Decimal,
                  client_order_id: str, result: dict) -> None:
        self._write(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), alert_id, ticker, side, count, str(price), client_order_id,
             json.dumps(result, default=str)),
        )

    # Paper trading.

    def add_paper_trade(self, strategy: str, v: Violation, legs: list[tuple[str, str, int, Decimal, Decimal]],
                        at: float) -> int:
        """`legs` are (ticker, side, count, price, cost including the fee)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO paper_trades (strategy, opened_at, event, side, safer, dominated, saving, bid_through) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (strategy, at, v.safer.event_ticker, v.side, v.safer.ticker, v.dominated.ticker, str(v.saving),
                 int(v.bid_through)),
            )
            for ticker, side, count, price, cost in legs:
                self._conn.execute(
                    "INSERT INTO paper_legs (trade_id, ticker, side, count, price, cost) VALUES (?,?,?,?,?,?)",
                    (cur.lastrowid, ticker, side, count, str(price), str(cost)),
                )
            return cur.lastrowid

    def open_paper_legs(self) -> list[dict]:
        cols = ["id", "trade_id", "strategy", "ticker", "side", "count", "price"]
        rows = self._read(
            "SELECT l.id, l.trade_id, t.strategy, l.ticker, l.side, l.count, l.price "
            "FROM paper_legs l JOIN paper_trades t ON t.id = l.trade_id WHERE l.status = 'open'"
        )
        return [dict(zip(cols, r), price=Decimal(r[6])) for r in rows]

    def set_paper_marks(self, marks: dict[int, Decimal]) -> None:
        with self._lock, self._conn:
            self._conn.executemany("UPDATE paper_legs SET mark = ? WHERE id = ?",
                                   [(str(m), leg_id) for leg_id, m in marks.items()])

    def close_paper_leg(self, leg_id: int, how: str, exit_price: Decimal, payout: Decimal, at: float) -> None:
        """`how` is sold or settled; the trade closes with its last open leg."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE paper_legs SET status = ?, exit_price = ?, payout = ?, closed_at = ? WHERE id = ?",
                (how, str(exit_price), str(payout), at, leg_id),
            )
            (trade_id,), = self._conn.execute("SELECT trade_id FROM paper_legs WHERE id = ?", (leg_id,))
            self._conn.execute(
                "UPDATE paper_trades SET status = 'closed', closed_at = ?, exit_reason = ? WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM paper_legs WHERE trade_id = ? AND status = 'open')",
                (at, how, trade_id, trade_id),
            )

    def paper_trades(self) -> list[dict]:
        """Every paper trade, oldest first, with its legs."""
        tcols = ["id", "strategy", "opened_at", "event", "side", "safer", "dominated", "saving", "bid_through",
                 "status", "closed_at", "exit_reason"]
        lcols = ["trade_id", "ticker", "side", "count", "price", "cost", "mark", "status", "exit_price", "payout"]
        trades = {r[0]: dict(zip(tcols, r), legs=[])
                  for r in self._read(f"SELECT {', '.join(tcols)} FROM paper_trades ORDER BY id")}
        for r in self._read(f"SELECT {', '.join(lcols)} FROM paper_legs ORDER BY id"):
            leg = dict(zip(lcols, r))
            for k in ("price", "cost", "mark", "exit_price", "payout"):
                leg[k] = Decimal(leg[k]) if leg[k] is not None else None
            trades[leg["trade_id"]]["legs"].append(leg)
        return list(trades.values())
