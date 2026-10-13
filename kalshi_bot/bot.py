"""Scan -> filter -> size -> buy (paper or live) -> settle, in a loop."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .client import KalshiClient, KalshiError
from .ledger import Ledger
from .strategy import Candidate, StrategyParams, find_candidates, size_order, taker_fee, to_float

log = logging.getLogger("kalshi_bot")


@dataclass
class RiskLimits:
    max_cost_per_trade: float = 10.0   # dollars per entry, fees included
    max_open_exposure: float = 50.0    # dollars across all unsettled positions
    max_daily_loss: float = 25.0       # stop opening trades after losing this much today (UTC)


@dataclass
class BotConfig:
    live: bool = False
    poll_seconds: float = 15.0
    ledger_path: str = "data/paper_trades.json"
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskLimits = field(default_factory=RiskLimits)


class Bot:
    def __init__(self, client: KalshiClient, config: BotConfig):
        if config.live and not client.authenticated:
            raise KalshiError("Live mode needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        self.client = client
        self.cfg = config
        self.ledger = Ledger(config.ledger_path)

    @property
    def mode(self) -> str:
        return "live" if self.cfg.live else "paper"

    def scan(self, now: datetime) -> list[Candidate]:
        ts = int(now.timestamp())
        p = self.cfg.strategy
        # Close-time filters can't be combined with status=open, so status is checked locally.
        markets = self.client.iter_markets(
            min_close_ts=ts + p.min_seconds_to_close,
            max_close_ts=ts + p.max_seconds_to_close,
            mve_filter="exclude",
        )
        return find_candidates(markets, now, p)

    def daily_loss_hit(self, now: datetime) -> bool:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.ledger.realized_pnl_since(midnight) <= -self.cfg.risk.max_daily_loss

    def step(self) -> None:
        now = datetime.now(timezone.utc)
        for t in self.ledger.settle(self.client, now):
            log.info("SETTLED %s %s @ %.2f x%s result=%s pnl=%+.2f",
                     t["ticker"], t["side"], t["price"], t["count"], t["result"], t["pnl"])

        if self.daily_loss_hit(now):
            log.warning("Daily loss limit reached; not opening new trades until 00:00 UTC")
            return

        candidates = self.scan(now)
        log.info("scan: %d candidates", len(candidates))
        for c in candidates:
            if self.ledger.has_ticker(c.ticker):
                continue
            room = self.cfg.risk.max_open_exposure - self.ledger.open_exposure()
            budget = min(self.cfg.risk.max_cost_per_trade, room)
            count = size_order(c, budget, self.cfg.strategy.fee_rate)
            if count <= 0:
                continue
            self.enter(c, count, now)

    def enter(self, c: Candidate, count: int, now: datetime) -> None:
        trade = {
            "mode": self.mode,
            "opened_at": now.isoformat(),
            "ticker": c.ticker,
            "title": c.title,
            "side": c.side,
            "close_time": c.close_time,
            "seconds_to_close": round(c.seconds_to_close),
            "status": "open",
        }
        if self.cfg.live:
            try:
                order = self.client.buy_ioc(c.ticker, c.side, count, c.price)
            except KalshiError as e:
                log.error("order failed for %s: %s", c.ticker, e)
                return
            filled = int(to_float(order.get("fill_count_fp")))
            if filled <= 0:
                log.info("no fill on %s %s @ %.2f", c.ticker, c.side, c.price)
                return
            cost = to_float(order.get("taker_fill_cost_dollars")) + to_float(order.get("maker_fill_cost_dollars"))
            fee = to_float(order.get("taker_fees_dollars")) + to_float(order.get("maker_fees_dollars"))
            trade.update(order_id=order.get("order_id"), count=filled,
                         price=round(cost / filled, 4), cost=round(cost, 4), fee=round(fee, 4))
        else:
            # Paper fill: assume the whole order fills at the displayed ask.
            trade.update(count=count, price=c.price, cost=round(c.price * count, 4),
                         fee=taker_fee(c.price, count, self.cfg.strategy.fee_rate))
        self.ledger.add(trade)
        log.info("BUY [%s] %s %s x%d @ %.2f (closes in %ds) %s",
                 self.mode, c.ticker, c.side.upper(), trade["count"], trade["price"],
                 c.seconds_to_close, c.title)

    def run(self, once: bool = False) -> None:
        log.info("starting in %s mode; ledger=%s", self.mode.upper(), self.ledger.path)
        while True:
            try:
                self.step()
            except Exception:  # keep the loop alive on transient API errors
                log.exception("step failed")
            if once:
                return
            time.sleep(self.cfg.poll_seconds)
