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
    alertable INTEGER NOT NULL DEFAULT 0
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
    alertable INTEGER NOT NULL
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
        cols = ["key", "side", "safer", "dominated", "opened_at", "last_seen",
                "first_saving", "max_saving", "max_net_saving", "alertable"]
        return {r[0]: dict(zip(cols, r)) for r in self._read(f"SELECT {', '.join(cols)} FROM episodes_open")}

    def save_episodes(self, upserts: list[dict], closes: list[dict], closed_at: float) -> None:
        with self._lock, self._conn:
            for e in upserts:
                self._conn.execute(
                    "INSERT OR REPLACE INTO episodes_open VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (e["key"], e["side"], e["safer"], e["dominated"], e["opened_at"], e["last_seen"],
                     e["first_saving"], e["max_saving"], e["max_net_saving"], e["alertable"]),
                )
            for e in closes:
                self._conn.execute("DELETE FROM episodes_open WHERE key = ?", (e["key"],))
                self._conn.execute(
                    "INSERT INTO episodes_closed VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (e["key"], e["side"], e["safer"], e["dominated"], e["opened_at"], e["last_seen"],
                     closed_at, e["first_saving"], e["max_saving"], e["max_net_saving"], e["alertable"]),
                )

    def closed_episodes(self, since: float = 0) -> list[dict]:
        cols = ["key", "opened_at", "last_seen", "closed_at", "max_saving", "max_net_saving", "alertable"]
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
