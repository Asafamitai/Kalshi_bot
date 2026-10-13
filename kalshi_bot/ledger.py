"""JSON trade ledger shared by paper and live trading, plus settlement and reporting."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from .strategy import parse_time, to_float


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self.trades: list[dict] = []
        if os.path.exists(path):
            with open(path) as f:
                self.trades = json.load(f)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.trades, f, indent=1)
        os.replace(tmp, self.path)

    def add(self, trade: dict) -> None:
        self.trades.append(trade)
        self.save()

    def has_ticker(self, ticker: str) -> bool:
        return any(t["ticker"] == ticker for t in self.trades)

    def open_trades(self) -> list[dict]:
        return [t for t in self.trades if t["status"] == "open"]

    def open_exposure(self) -> float:
        return sum(t["cost"] + t["fee"] for t in self.open_trades())

    def realized_pnl_since(self, since: datetime) -> float:
        return sum(
            t["pnl"] for t in self.trades
            if t["status"] == "settled" and parse_time(t["settled_at"]) >= since
        )

    def settle(self, client, now: datetime | None = None) -> list[dict]:
        """Look up results for open trades whose market has closed."""
        now = now or datetime.now(timezone.utc)
        settled = []
        for t in self.open_trades():
            if parse_time(t["close_time"]) > now:
                continue
            m = client.get_market(t["ticker"])
            payout = settlement_payout(m, t["side"])
            if payout is None:
                continue
            t.update(
                status="settled",
                result=m.get("result") or "",
                settled_at=now.isoformat(),
                pnl=round(payout * t["count"] - t["cost"] - t["fee"], 4),
            )
            settled.append(t)
        if settled:
            self.save()
        return settled


def settlement_payout(market: dict, side: str) -> float | None:
    """Payout per contract for `side`, or None if the market has not settled yet."""
    result = market.get("result")
    if result in ("yes", "no"):
        return 1.0 if result == side else 0.0
    if market.get("status") in ("settled", "finalized") and market.get("settlement_value_dollars"):
        yes_value = to_float(market["settlement_value_dollars"])
        return yes_value if side == "yes" else 1 - yes_value
    return None


def price_bucket(price: float) -> str:
    for lo, hi in ((0.85, 0.90), (0.90, 0.95), (0.95, 1.0)):
        if price < hi:
            return f"{int(lo * 100)}-{int(hi * 100) - 1}c"
    return "100c"


def summarize(trades: list[dict]) -> str:
    """Win rate vs. breakeven and P&L, overall and per price bucket.

    Each trade needs: price, count, cost, fee, pnl, and won (bool).
    """
    if not trades:
        return "No settled trades yet."

    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups["ALL"].append(t)
        groups[price_bucket(t["price"])].append(t)

    lines = [f"{'bucket':<9}{'trades':>7}{'wins':>6}{'win%':>8}{'breakeven%':>12}"
             f"{'cost $':>10}{'pnl $':>10}{'roi%':>8}"]
    for name in ["ALL"] + sorted(k for k in groups if k != "ALL"):
        g = groups[name]
        wins = sum(1 for t in g if t["won"])
        contracts = sum(t["count"] for t in g)
        spent = sum(t["cost"] + t["fee"] for t in g)
        pnl = sum(t["pnl"] for t in g)
        breakeven = spent / contracts if contracts else 0
        lines.append(
            f"{name:<9}{len(g):>7}{wins:>6}{100 * wins / len(g):>7.1f}%{100 * breakeven:>11.1f}%"
            f"{spent:>10.2f}{pnl:>10.2f}{100 * pnl / spent if spent else 0:>7.1f}%"
        )
    lines.append("breakeven% = what fraction of contracts must win to cover price + fees.")
    return "\n".join(lines)


def report(ledger: Ledger) -> str:
    settled = [dict(t, won=t["pnl"] > 0) for t in ledger.trades if t["status"] == "settled"]
    open_ = ledger.open_trades()
    head = (f"Ledger: {ledger.path}  |  settled: {len(settled)}  |  open: {len(open_)}"
            f"  (exposure ${ledger.open_exposure():.2f})")
    losses = [t for t in settled if not t["won"]]
    tail = ""
    if losses:
        tail = "\nLosses:\n" + "\n".join(
            f"  {t['ticker']} {t['side']} @ {t['price']:.2f} x{t['count']} pnl {t['pnl']:.2f}"
            for t in losses[-10:]
        )
    return head + "\n" + summarize(settled) + tail
