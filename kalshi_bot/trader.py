"""One-tap buy: re-check both rungs against fresh quotes, then buy the safer one."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import requests

from .config import Config
from .format import cents, price
from .kalshi import KalshiClient, KalshiError
from .ladder import Rung, Violation, to_decimal
from .store import Alert


@dataclass(frozen=True)
class Outcome:
    kind: str  # bought | unfilled | dry_run | refused | repriced | rejected | unknown
    message: str
    violation: Violation | None = None


def contracts_for(budget: Decimal, ask: Decimal) -> int:
    return int(budget // ask) if ask > 0 else 0


def recheck(client: KalshiClient, alert: Alert, min_saving: Decimal) -> tuple[Violation | None, str | None]:
    """Fresh violation for the alerted pair, or the reason it can no longer be bought."""
    markets = {m["ticker"]: m for m in client.get_markets([alert.safer, alert.dominated])}
    missing = [t for t in (alert.safer, alert.dominated) if t not in markets]
    if missing:
        return None, f"market not found: {', '.join(missing)}"
    closed = [t for t, m in markets.items() if m.get("status") != "active"]
    if closed:
        return None, f"no longer trading: {', '.join(closed)}"
    safer = Rung.from_market(markets[alert.safer])
    dominated = Rung.from_market(markets[alert.dominated])
    v = Violation(alert.side, safer, dominated)
    if not v.buyable:
        return v, f"empty book on {', '.join(v.empty_books)}"
    if v.saving < min_saving:
        return v, f"the contradiction is gone (saving now {cents(v.saving)})"
    return v, None


def buy(client: KalshiClient, alert: Alert, cfg: Config) -> Outcome:
    v, problem = recheck(client, alert, cfg.min_saving)
    if problem:
        return Outcome("refused", f"Not bought: {problem}.", v)
    if v.ask > alert.ask:
        # Never pay more than the price the button showed.
        return Outcome(
            "repriced",
            f"Not bought: the ask moved from {price(alert.ask)} to {price(v.ask)}. Fresh alert below.",
            v,
        )
    count = contracts_for(cfg.budget, v.ask)
    if count < 1:
        return Outcome("refused", f"Not bought: budget ${cfg.budget} buys less than one contract.", v)
    what = f"{count} {v.side.upper()} {v.safer.ticker} @ {price(v.ask)}"
    if not cfg.live_trading:
        return Outcome("dry_run", f"Dry run (LIVE_TRADING is off): would buy {what}, saving {cents(v.saving)}.", v)

    try:
        order = client.buy_ioc(
            v.safer.ticker, v.side, count, v.ask, client_order_id=f"ladder-{alert.id}-{uuid.uuid4().hex[:12]}"
        )
    except KalshiError as e:
        if e.status < 500:
            return Outcome("rejected", f"Kalshi rejected the order for {what}: {e}", v)
        return Outcome("unknown", f"The order for {what} may or may not have gone through ({e}). "
                       "Check your Kalshi positions; this button is now disabled.", v)
    except requests.RequestException as e:
        return Outcome("unknown", f"No answer from Kalshi for the order for {what} ({e}). "
                       "Check your Kalshi positions; this button is now disabled.", v)
    filled = to_decimal(order.get("fill_count_fp"))
    if filled <= 0:
        return Outcome("unfilled", f"Order for {what} did not fill; nothing bought.", v)
    cost = to_decimal(order.get("taker_fill_cost_dollars"))
    fees = to_decimal(order.get("taker_fees_dollars"))
    return Outcome(
        "bought",
        f"Bought {filled.normalize():f} of {count} {v.side.upper()} {v.safer.ticker} "
        f"for ${cost:.2f} + ${fees:.2f} fees.",
        v,
    )
