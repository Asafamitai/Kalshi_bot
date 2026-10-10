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
from decimal import Decimal, InvalidOperation
from typing import Iterable

ZERO = Decimal(0)
ONE = Decimal(1)

ABOVE = ("greater", "greater_or_equal")
BELOW = ("less", "less_or_equal")
SIDES = ("yes", "no")

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


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
    ladder_key: tuple

    @classmethod
    def from_market(cls, m: dict) -> Rung | None:
        strike_type = m.get("strike_type")
        if strike_type in ABOVE:
            strike = m.get("floor_strike")
        elif strike_type in BELOW:
            strike = m.get("cap_strike")
        else:
            return None
        if strike is None:
            return None
        # Rungs only compare when they measure the same thing at the same time:
        # same event, same strike type, same close, and the same rules text
        # once the numbers are blanked out.
        rules_template = _NUMBER.sub("#", m.get("rules_primary") or "")
        ladder_key = (m["event_ticker"], strike_type, m.get("close_time"), rules_template)
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
            ladder_key=ladder_key,
        )

    def ask(self, side: str) -> Decimal:
        return self.yes_ask if side == "yes" else self.no_ask

    def bid(self, side: str) -> Decimal:
        return self.yes_bid if side == "yes" else self.no_bid

    def mid(self, side: str) -> Decimal:
        return (self.bid(side) + self.ask(side)) / 2

    def ask_size(self, side: str) -> Decimal:
        # A NO ask is the other face of a YES bid, so it has the YES bid's size.
        return self.yes_ask_size if side == "yes" else self.yes_bid_size

    def is_empty(self, side: str) -> bool:
        """A 1.00 ask, or an ask with no bid behind it, is not a price."""
        ask, bid = self.ask(side), self.bid(side)
        return ask >= ONE or ask <= ZERO or bid <= ZERO

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
    def bid_through(self) -> bool:
        """Safer ask below the dominated bid: buying one and selling the other locks it in."""
        return self.ask < self.dominated.bid(self.side)

    @property
    def empty_books(self) -> list[str]:
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


def ladder_violations(rungs: list[Rung], min_saving: Decimal) -> list[Violation]:
    """For each rung and side, the strongest contradiction against a less-safe rung."""
    found = []
    for side in SIDES:
        for safer in rungs:
            best = None
            for other in rungs:
                if other.safety(side) >= safer.safety(side):
                    continue
                v = Violation(side, safer, other)
                if v.saving < min_saving:
                    continue
                # Prefer a dominated rung with a real book over a bigger number
                # computed from an empty one.
                if best is None or (not other.is_empty(side), v.saving) > (
                    not best.dominated.is_empty(side),
                    best.saving,
                ):
                    best = v
            if best is not None:
                found.append(best)
    return found


def find_violations(markets: Iterable[dict], min_saving: Decimal) -> list[Violation]:
    found = []
    for rungs in build_ladders(markets):
        found.extend(ladder_violations(rungs, min_saving))
    return sorted(found, key=lambda v: (v.buyable, v.saving), reverse=True)
