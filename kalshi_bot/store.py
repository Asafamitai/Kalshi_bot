"""SQLite persistence: alerts behind Telegram buttons, and a log of every finding."""

from __future__ import annotations

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
CREATE TABLE IF NOT EXISTS findings (
    scanned_at REAL NOT NULL,
    side TEXT NOT NULL,
    safer TEXT NOT NULL,
    dominated TEXT NOT NULL,
    ask TEXT NOT NULL,
    ask_size TEXT NOT NULL,
    dominated_bid TEXT NOT NULL,
    dominated_ask TEXT NOT NULL,
    saving TEXT NOT NULL,
    bid_through INTEGER NOT NULL,
    empty_book INTEGER NOT NULL
);
"""


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

    def log_findings(self, violations: list[Violation], scanned_at: float | None = None) -> None:
        ts = scanned_at or time.time()
        rows = [
            (
                ts,
                v.side,
                v.safer.ticker,
                v.dominated.ticker,
                str(v.ask),
                str(v.ask_size),
                str(v.dominated.bid(v.side)),
                str(v.dominated.ask(v.side)),
                str(v.saving),
                int(v.bid_through),
                int(not v.buyable),
            )
            for v in violations
        ]
        with self._lock, self._conn:
            self._conn.executemany("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    def add_alert(self, v: Violation) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO alerts (created_at, side, safer, dominated, ask, saving) VALUES (?,?,?,?,?,?)",
                (time.time(), v.side, v.safer.ticker, v.dominated.ticker, str(v.ask), str(v.saving)),
            )
            return cur.lastrowid

    def get_alert(self, alert_id: int) -> Alert | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, side, safer, dominated, ask, saving, status FROM alerts WHERE id = ?",
                (alert_id,),
            ).fetchone()
        if row is None:
            return None
        return Alert(row[0], row[1], row[2], row[3], Decimal(row[4]), Decimal(row[5]), row[6])

    def claim_alert(self, alert_id: int) -> bool:
        """Mark an open alert as being bought; False if someone already did (double tap)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE alerts SET status = 'buying' WHERE id = ? AND status = 'open'", (alert_id,)
            )
            return cur.rowcount == 1

    def set_status(self, alert_id: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE alerts SET status = ? WHERE id = ?", (status, alert_id))
