"""High-probability, near-expiry strategy.

Buy the side (YES or NO) of a market whose ask is at or above `min_price`
(default 85c) when the market closes within `max_seconds_to_close`
(default 5 minutes), then hold to settlement.

Pure functions only: no network, no state. Easy to test and to reuse in the backtest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class StrategyParams:
    min_price: float = 0.85          # only buy a side priced at least this high
    max_price: float = 0.97          # above this, fees eat the whole profit
    max_seconds_to_close: int = 300  # market must close within 5 minutes
    min_seconds_to_close: int = 5    # too close to the bell: order may not make it in time
    max_spread: float = 0.05         # yes_ask - yes_bid; wide spread = empty/stale book
    min_volume: float = 0.0          # lifetime contracts traded in the market
    fee_rate: float = 0.07           # Kalshi taker fee: ceil(rate * C * P * (1 - P))
    exclude_prefixes: tuple[str, ...] = ("KXMVE",)  # multivariate parlays


@dataclass(frozen=True)
class Candidate:
    ticker: str
    title: str
    side: str            # "yes" or "no"
    price: float         # ask for that side, in dollars
    available: float     # contracts offered at that ask
    seconds_to_close: float
    close_time: str


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def taker_fee(price: float, count: float, rate: float = 0.07) -> float:
    """Kalshi taker fee in dollars for one order, rounded up to the cent."""
    raw_cents = rate * count * price * (1 - price) * 100
    return math.ceil(round(raw_cents, 6)) / 100


def net_profit_if_win(price: float, count: int, rate: float = 0.07) -> float:
    return round((1 - price) * count - taker_fee(price, count, rate), 4)


def side_quotes(market: dict) -> dict[str, tuple[float, float]]:
    """Best ask and size for each side. Buying NO takes the resting YES bids."""
    yes_bid = to_float(market.get("yes_bid_dollars"))
    yes_ask = to_float(market.get("yes_ask_dollars"))
    no_ask = to_float(market.get("no_ask_dollars"), 1 - yes_bid)
    return {
        "yes": (yes_ask, to_float(market.get("yes_ask_size_fp"))),
        "no": (no_ask, to_float(market.get("yes_bid_size_fp"))),
    }


def evaluate_market(market: dict, now: datetime, p: StrategyParams) -> Candidate | None:
    ticker = market.get("ticker", "")
    if market.get("status") not in ("active", "open"):
        return None
    if market.get("market_type", "binary") != "binary":
        return None
    if ticker.startswith(p.exclude_prefixes) or market.get("mve_collection_ticker"):
        return None
    if to_float(market.get("volume_fp")) < p.min_volume:
        return None

    seconds_left = (parse_time(market["close_time"]) - now).total_seconds()
    if not (p.min_seconds_to_close <= seconds_left <= p.max_seconds_to_close):
        return None

    yes_bid = to_float(market.get("yes_bid_dollars"))
    yes_ask = to_float(market.get("yes_ask_dollars"))
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask - yes_bid > p.max_spread + 1e-9:
        return None

    for side, (ask, size) in side_quotes(market).items():
        if p.min_price - 1e-9 <= ask <= p.max_price + 1e-9 and size > 0:
            return Candidate(
                ticker=ticker,
                title=market.get("title", ""),
                side=side,
                price=round(ask, 4),
                available=size,
                seconds_to_close=seconds_left,
                close_time=market["close_time"],
            )
    return None


def find_candidates(markets, now: datetime | None = None,
                    params: StrategyParams = StrategyParams()) -> list[Candidate]:
    now = now or datetime.now(timezone.utc)
    found = [c for m in markets if (c := evaluate_market(m, now, params))]
    return sorted(found, key=lambda c: c.seconds_to_close)


def size_order(c: Candidate, max_cost: float, fee_rate: float = 0.07) -> int:
    """Contracts to buy: as many as `max_cost` allows, capped by book size,
    and only if a win still makes money after fees."""
    count = min(int(max_cost // c.price), int(c.available))
    while count > 0 and count * c.price + taker_fee(c.price, count, fee_rate) > max_cost:
        count -= 1
    if count <= 0 or net_profit_if_win(c.price, count, fee_rate) <= 0:
        return 0
    return count
