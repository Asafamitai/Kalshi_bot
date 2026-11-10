"""Ladder logic: find contracts that are both cheaper and safer than a sibling rung.

An event like "how many downloads will the app get" is split into nested
threshold strikes ("above 100", "above 105", ...). NO on "above 107" pays in
every state where NO on "above 105" pays, plus the band between them, so it is
strictly safer and must never be cheaper:

    no_ask  must be non-decreasing in strike
    yes_ask must be non-increasing in strike

(mirrored for "below K" ladders). Any inversion is a contradiction that can be
proven from the order book alone; nothing here forecasts anything.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .fees import fee_per_contract

ZERO = Decimal(0)
ONE = Decimal(1)

ABOVE = ("greater", "greater_or_equal")
BELOW = ("less", "less_or_equal")
SIDES = ("yes", "no")

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def to_epoch(value) -> float | None:
    """Kalshi's ISO timestamps in seconds; None when missing or unreadable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def to_decimal(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return ZERO


@dataclass(frozen=True)
class Rung:
    """One threshold market, with its top-of-book quotes in dollars."""

    ticker: str
    event_ticker: str
    strike_type: str
    strike: Decimal
    label: str
    title: str
    status: str
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    yes_bid_size: Decimal
    yes_ask_size: Decimal
    volume_24h: Decimal
    ladder_key: tuple
    open_interest: Decimal = ZERO
    # When trading stops or the outcome is due, whichever comes first.
    ends_at: float | None = None
    # Which of Kalshi's exchange shards lists it; money on one shard can't buy on another.
    exchange_index: int = 0

    @classmethod
    def from_market(cls, m: dict) -> Rung | None:
        strike_type = m.get("strike_type")
        floor, cap = m.get("floor_strike"), m.get("cap_strike")
        # A threshold has exactly one bound. Kalshi also tags some "exactly N"
        # buckets as `less` with floor == cap; those are not nested, so skip
        # anything carrying both.
        if strike_type in ABOVE and floor is not None and cap is None:
            strike = floor
        elif strike_type in BELOW and cap is not None and floor is None:
            strike = cap
        else:
            return None
        # Rungs only compare when they measure the same thing at the same time:
        # same event, same strike type, same close, and the same rules text
        # once the numbers are blanked out.
        rules_template = _NUMBER.sub("#", m.get("rules_primary") or "")
        ladder_key = (m["event_ticker"], strike_type, m.get("close_time"), rules_template)
        ends = [t for t in (to_epoch(m.get("close_time")), to_epoch(m.get("expected_expiration_time"))) if t]
        return cls(
            ticker=m["ticker"],
            event_ticker=m["event_ticker"],
            strike_type=strike_type,
            strike=to_decimal(strike),
            label=m.get("yes_sub_title") or str(strike),
            title=m.get("title") or m["ticker"],
            status=m.get("status", ""),
            yes_bid=to_decimal(m.get("yes_bid_dollars")),
            yes_ask=to_decimal(m.get("yes_ask_dollars")),
            no_bid=to_decimal(m.get("no_bid_dollars")),
            no_ask=to_decimal(m.get("no_ask_dollars")),
            yes_bid_size=to_decimal(m.get("yes_bid_size_fp")),
            yes_ask_size=to_decimal(m.get("yes_ask_size_fp")),
            volume_24h=to_decimal(m.get("volume_24h_fp")),
            ladder_key=ladder_key,
            open_interest=to_decimal(m.get("open_interest_fp")),
            ends_at=min(ends) if ends else None,
            exchange_index=int(m.get("exchange_index") or 0),
        )

    def ask(self, side: str) -> Decimal:
        return self.yes_ask if side == "yes" else self.no_ask

    def bid(self, side: str) -> Decimal:
        return self.yes_bid if side == "yes" else self.no_bid

    def hours_left(self, now: float) -> float | None:
        return None if self.ends_at is None else (self.ends_at - now) / 3600

    def spread(self, side: str) -> Decimal:
        return self.ask(side) - self.bid(side)

    def mid(self, side: str) -> Decimal:
        return (self.bid(side) + self.ask(side)) / 2

    def bid_size(self, side: str) -> Decimal:
        # A NO bid is the other face of a YES ask.
        return self.yes_bid_size if side == "yes" else self.yes_ask_size

    def ask_size(self, side: str) -> Decimal:
        # A NO ask is the other face of a YES bid, so it has the YES bid's size.
        return self.yes_ask_size if side == "yes" else self.yes_bid_size

    def is_empty(self, side: str) -> bool:
        """A 1.00 ask, or an ask with no bid behind it, is not a price."""
        # Kalshi fills missing quotes with 0 or 1 (a fully empty book can even
        # show a 0.00 YES ask and a 1.00 NO bid), so only 0 < bid < ask < 1 is real.
        return not (ZERO < self.bid(side) < self.ask(side) < ONE)

    def is_priced(self, side: str, max_spread: Decimal) -> bool:
        """A two-sided book tight enough for its mid to be a price."""
        return not self.is_empty(side) and self.spread(side) <= max_spread

    def safety(self, side: str) -> Decimal:
        """Higher means this contract pays in a superset of states."""
        # YES on "above K" gets safer as K falls, NO as K rises; "below K" mirrors that.
        sign = -1 if (self.strike_type in ABOVE) == (side == "yes") else 1
        return sign * self.strike


@dataclass(frozen=True)
class Violation:
    """`safer` pays whenever `dominated` does, yet its ask is below `dominated`'s mid."""

    side: str
    safer: Rung
    dominated: Rung

    @property
    def ask(self) -> Decimal:
        return self.safer.ask(self.side)

    @property
    def ask_size(self) -> Decimal:
        return self.safer.ask_size(self.side)

    @property
    def dominated_mid(self) -> Decimal:
        return self.dominated.mid(self.side)

    @property
    def saving(self) -> Decimal:
        # Measured against the dominated rung's mid, not its ask: an ask in a
        # wide market is a price nobody pays.
        return self.dominated_mid - self.ask

    @property
    def fee(self) -> Decimal:
        return fee_per_contract(self.ask)

    @property
    def net_saving(self) -> Decimal:
        """The saving left after the taker fee on the contract bought."""
        return self.saving - self.fee

    @property
    def bid_through(self) -> bool:
        """Safer ask below the dominated bid: buying one and selling the other locks it in."""
        return self.ask < self.dominated.bid(self.side)

    @property
    def empty_books(self) -> list[str]:
        # In practice only the safer rung: the dominated one must be priced.
        return [r.ticker for r in (self.safer, self.dominated) if r.is_empty(self.side)]

    @property
    def buyable(self) -> bool:
        return not self.empty_books

    @property
    def key(self) -> tuple[str, str]:
        """What you'd buy; the same contract isn't re-alerted just because its reference rung changed."""
        return (self.safer.ticker, self.side)


def build_ladders(markets: Iterable[dict]) -> list[list[Rung]]:
    groups: dict[tuple, list[Rung]] = defaultdict(list)
    for m in markets:
        rung = Rung.from_market(m)
        if rung is not None:
            groups[rung.ladder_key].append(rung)
    return [sorted(g, key=lambda r: r.strike) for g in groups.values() if len(g) >= 2]


def ladder_violations(rungs: list[Rung], min_saving: Decimal, max_spread: Decimal) -> list[Violation]:
    """For each rung and side, the strongest contradiction against a less-safe rung."""
    found = []
    for side in SIDES:
        # Only a quote someone could trade at proves anything: the dominated
        # rung needs a two-sided book no wider than max_spread for its mid to
        # mean something, and the safer rung needs an ask to buy.
        priced = [r for r in rungs if r.is_priced(side, max_spread)]
        for safer in rungs:
            if not ZERO < safer.ask(side) < ONE:
                continue
            best = None
            for other in priced:
                if other.safety(side) >= safer.safety(side):
                    continue
                v = Violation(side, safer, other)
                if v.saving >= min_saving and (best is None or v.saving > best.saving):
                    best = v
            if best is not None:
                found.append(best)
    return found


def find_violations(markets: Iterable[dict], min_saving: Decimal, max_spread: Decimal) -> list[Violation]:
    found = []
    for rungs in build_ladders(markets):
        found.extend(ladder_violations(rungs, min_saving, max_spread))
    return sorted(found, key=lambda v: (v.buyable, v.saving), reverse=True)
