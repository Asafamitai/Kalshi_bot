"""Paper trading: what an autopilot would have done with each contradiction, without money.

Two books run side by side, each with its own bankroll, so they can be compared:

- single: the safer contract of every contradiction with at least the alerts'
  net saving and the book's own, looser filters (PAPER_MIN_PRICE to
  PAPER_MAX_PRICE, PAPER_MIN_DEPTH, PAPER_MIN_VOLUME_24H). Each trade records
  whether it also passed the alert filters, so the two can be compared. Sold
  once its bid holds PAPER_TAKE_PROFIT of the maximum profit, otherwise held
  to settlement. It wins or loses with the outcome.
- pair: only where the safer ask is below the dominated rung's bid. Buys the
  safer contract and the other side of the dominated one, which together pay
  at least 1 in every outcome, so the profit is known at entry, after fees.
  Held to settlement.

Both hold at most one trade per event at a time (the two sides of one
inversion are the same bet), spend at most PAPER_MAX_TRADE on it and only cash
their bankroll still has, and buy only after a second read of both rungs.
Fills are assumed at that read's ask, up to the depth shown there, with the
taker fee; sales at the bid with the fee; settlement pays 1 or 0, no fee.
Real orders would fill no better.
"""

from __future__ import annotations

import time
from collections import Counter
from decimal import Decimal
from html import escape

from . import alerts
from .config import Config
from .fees import fee_per_contract, order_cost, order_fee
from .format import event_url, pair, price
from .kalshi import KalshiClient
from .ladder import ONE, ZERO, Rung, Violation, to_decimal
from .store import Store

SINGLE, PAIR = "single", "pair"
STRATEGIES = (SINGLE, PAIR)

Leg = tuple[str, str, int, Decimal, Decimal]  # ticker, side, count, price, cost with the fee


def other_side(side: str) -> str:
    return "no" if side == "yes" else "yes"


def locked(v: Violation) -> bool:
    """The pair pays at least 1 in every outcome and costs less than 1 after fees."""
    return v.buyable and v.bid_through and pair(v)["worst"] > 0


def single_filters(v: Violation, cfg: Config) -> bool:
    return (
        v.net_saving >= cfg.min_net_saving
        and cfg.paper_min_price <= v.ask <= cfg.paper_max_price
        and v.ask_size >= cfg.paper_min_depth
        and v.safer.volume_24h >= cfg.paper_min_volume_24h
    )


def wanted(strategy: str, v: Violation, cfg: Config) -> bool:
    # Like the buy buttons: no single contract without a bid behind its ask to sell back to.
    return v.buyable and single_filters(v, cfg) if strategy == SINGLE else locked(v)


def legs(strategy: str, v: Violation, budget: Decimal) -> list[Leg]:
    """The most contracts `budget` buys at the ask, fees included; [] if not even one."""
    if strategy == SINGLE:
        quotes, depth = [(v.safer.ticker, v.side, v.ask)], v.ask_size
    else:
        p = pair(v)
        quotes = [(v.safer.ticker, v.side, v.ask), (v.dominated.ticker, other_side(v.side), p["other_price"])]
        depth = p["size"]
    prices = [q[2] for q in quotes]
    n = min(int(budget / sum(p + fee_per_contract(p) for p in prices)), int(depth))
    while n > 0 and sum(order_cost(p, n) for p in prices) > budget:
        n -= 1  # fees round up per order
    return [(t, s, n, p, order_cost(p, n)) for t, s, p in quotes] if n >= 1 else []


def settlement(m: dict | None, side: str) -> Decimal | None:
    """What one contract on `side` pays once the market has resolved; None until then."""
    if m is None:
        return None
    result = m.get("result")
    if result in ("yes", "no"):
        yes = ONE if result == "yes" else ZERO
    elif m.get("status") in ("settled", "finalized") and m.get("settlement_value_dollars") is not None:
        yes = to_decimal(m["settlement_value_dollars"])
    else:
        return None
    return yes if side == "yes" else ONE - yes


def cash(trades: list[dict], strategy: str, bankroll: Decimal) -> Decimal:
    """The bankroll, less what was paid, plus what came back."""
    held = [leg for t in trades if t["strategy"] == strategy for leg in t["legs"]]
    paid = sum((leg["cost"] for leg in held), ZERO)
    back = sum((leg["payout"] for leg in held if leg["payout"] is not None), ZERO)
    return bankroll - paid + back


def step(store: Store, client: KalshiClient, cfg: Config, violations: list[Violation],
         markets: dict[str, dict], now: float) -> dict:
    """One pass: mark what is held, sell or settle what is done, then open new trades.

    `markets` is the scan's open markets by ticker; a leg missing from it has
    stopped trading and is looked up to see whether it has resolved. Returns
    the ids of the trades opened and closed, and how many legs were sold and
    settled.
    """
    done = {"opened": [], "closed": [], "sold": 0, "settled": 0}
    marks, gone = {}, []
    for leg in store.open_paper_legs():
        m = markets.get(leg["ticker"])
        if m is None:
            gone.append(leg)
            continue
        rung = Rung.from_market(m)
        bid, depth = (rung.bid(leg["side"]), rung.bid_size(leg["side"])) if rung else (ZERO, ZERO)
        marks[leg["id"]] = bid
        if leg["strategy"] == SINGLE and _take_profit(leg, bid, depth, cfg.paper_take_profit):
            proceeds = bid * leg["count"] - order_fee(bid, leg["count"])
            _closed(done, store.close_paper_leg(leg["id"], "sold", bid, proceeds, now))
            done["sold"] += 1
    store.set_paper_marks(marks)
    if gone:
        fresh = {m["ticker"]: m for m in client.get_markets(sorted({leg["ticker"] for leg in gone}))}
        for leg in gone:
            value = settlement(fresh.get(leg["ticker"]), leg["side"])
            if value is not None:
                _closed(done, store.close_paper_leg(leg["id"], "settled", value, value * leg["count"], now))
                done["settled"] += 1
    done["opened"] = _open(store, client, cfg, violations, now)
    return done


def _closed(done: dict, trade_id: int | None) -> None:
    if trade_id is not None:
        done["closed"].append(trade_id)


def _take_profit(leg: dict, bid: Decimal, depth: Decimal, share: Decimal) -> bool:
    """The bid holds `share` of the maximum profit and can take the whole position."""
    return bid > ZERO and depth >= leg["count"] and bid - leg["price"] >= share * (ONE - leg["price"])


def _key(v: Violation) -> tuple[str, str, str]:
    return (v.side, v.safer.ticker, v.dominated.ticker)


def _open(store: Store, client: KalshiClient, cfg: Config, violations: list[Violation], now: float) -> list[int]:
    trades = store.paper_trades()
    busy = {s: {t["event"] for t in trades if t["strategy"] == s and t["status"] == "open"} for s in STRATEGIES}
    want = {s: [v for v in violations if v.safer.event_ticker not in busy[s] and wanted(s, v, cfg)]
            for s in STRATEGIES}
    candidates = list({_key(v): v for s in STRATEGIES for v in want[s]}.values())
    if not candidates:
        return []
    fresh = alerts.reread(client, candidates, cfg)
    opened = []
    for s in STRATEGIES:
        left = cash(trades, s, cfg.paper_bankroll)
        asked = {_key(v) for v in want[s]}
        picks = [v for v in fresh if _key(v) in asked and wanted(s, v, cfg)]
        picks.sort(key=lambda v: v.net_saving if s == SINGLE else pair(v)["worst"], reverse=True)
        for v in picks:
            if v.safer.event_ticker in busy[s]:
                continue
            bought = legs(s, v, cfg.paper_max_trade)
            cost = sum((leg[4] for leg in bought), ZERO)
            if not bought or cost > left:
                continue
            if s == PAIR and bought[0][2] <= cost:
                continue  # the per-order fee rounding ate the lock at this size
            opened.append(store.add_paper_trade(s, v, bought, alerts.alertable(v, cfg), now))
            left -= cost
            busy[s].add(v.safer.event_ticker)
    return opened


# Report


def _cost(t: dict) -> Decimal:
    return sum((leg["cost"] for leg in t["legs"]), ZERO)


def _value(t: dict) -> Decimal:
    """What came back, plus what is still held at its last bid (at cost until first marked)."""
    return sum(
        (leg["payout"] if leg["payout"] is not None else leg["count"] * (leg["mark"] if leg["mark"] is not None
                                                                         else leg["price"])
         for leg in t["legs"]),
        ZERO,
    )


def _floor(t: dict) -> Decimal:
    """A pair pays at least 1 per contract: its worst case."""
    return t["legs"][0]["count"] - _cost(t)


def _signed(d: Decimal) -> str:
    return f"{'+' if d >= 0 else '-'}${abs(d):,.2f}"


def _book(strategy: str, trades: list[dict], left: Decimal) -> list[str]:
    if not trades:
        return [f"{strategy}: no trades yet, cash ${left:,.2f}"]
    closed = [t for t in trades if t["status"] == "closed"]
    open_ = [t for t in trades if t["status"] == "open"]
    out = [f"{strategy}: {len(trades)} trades, {len(open_)} open, cash ${left:,.2f}"]
    if closed:
        pnl = [_value(t) - _cost(t) for t in closed]
        cost = sum(_cost(t) for t in closed)
        won = sum(p > 0 for p in pnl)
        why = Counter(t["exit_reason"] for t in closed)
        out.append(f"  closed {len(closed)}: won {won}, lost {len(closed) - won}, P&L {_signed(sum(pnl))} "
                   f"on ${cost:,.2f} ({sum(pnl) / cost:+.1%}); sold {why['sold']}, settled {why['settled']}")
        if strategy == SINGLE:
            for label, key in (("pass the alert filters", "alertable"), ("bid-through (a pair was locked)",
                                                                          "bid_through")):
                some = [t for t in closed if t[key]]
                if some:
                    out.append(f"  of which {label}: {len(some)}, "
                               f"P&L {_signed(sum(_value(t) - _cost(t) for t in some))}")
    if open_:
        cost = sum(_cost(t) for t in open_)
        if strategy == PAIR:
            out.append(f"  open: cost ${cost:,.2f}, locked at least {_signed(sum(_floor(t) for t in open_))}")
        else:
            value = sum(_value(t) for t in open_)
            out.append(f"  open: cost ${cost:,.2f}, at the bid ${value:,.2f} ({_signed(value - cost)})")
    return out


def _line(t: dict) -> str:
    when = time.strftime("%m-%d %H:%M", time.gmtime(t["opened_at"]))
    bought = " + ".join(f"{leg['count']} {leg['side'].upper()} {leg['ticker']} @{price(leg['price'])}"
                        for leg in t["legs"])
    cost = _cost(t)
    if t["status"] == "closed":
        state = f"{t['exit_reason']} {_signed(_value(t) - cost)}"
    elif t["strategy"] == PAIR:
        state = f"open, locked {_signed(_floor(t))}"
    else:
        state = f"open, at the bid {_signed(_value(t) - cost)}"
    return f"  {when} {t['strategy']:6} {bought}  ${cost:,.2f}  {state}"


def report(store: Store, cfg: Config, latest: int = 8) -> str:
    trades = store.paper_trades()
    lines = [f"Paper trading: ${cfg.paper_bankroll:,.0f} per book, up to ${cfg.paper_max_trade:,.2f} a trade; "
             f"single buys at {price(cfg.paper_min_price)}-{price(cfg.paper_max_price)}, depth >= "
             f"{cfg.paper_min_depth:g}, 24h volume >= {cfg.paper_min_volume_24h:g}, "
             f"sells at {cfg.paper_take_profit:.0%} of max profit"]
    for s in STRATEGIES:
        lines += _book(s, [t for t in trades if t["strategy"] == s], cash(trades, s, cfg.paper_bankroll))
    if trades:
        lines.append("Latest (UTC):")
        lines += [_line(t) for t in reversed(trades[-latest:])]
    return "\n".join(lines)


# Telegram: one message per pass with what happened, in Hebrew like the alerts.

_NAMES = {SINGLE: "בודדת", PAIR: "זוג"}
_EXITS = {"sold": "נמכר ביעד הרווח", "settled": "הוכרע"}
_LIMIT = 3500  # Telegram allows 4096 characters a message


def _bought(t: dict) -> str:
    url = escape(event_url(t["event"]))
    legs_ = " + ".join(f'{leg["count"]} {leg["side"].upper()} <a href="{url}">{escape(leg["ticker"])}</a>'
                       f" ב-{price(leg['price'])}" for leg in t["legs"])
    notes = [f"חיסכון {Decimal(t['saving']) * 100:.1f}¢"]
    if t["strategy"] == PAIR:
        notes.append(f"נעול לפחות {_signed(_floor(t))}")
    elif t["alertable"]:
        notes.append("עובר את סינון ההתראות")
    return f"• {_NAMES[t['strategy']]} · {legs_} · ${_cost(t):,.2f} · {' · '.join(notes)}"


def _ended(t: dict) -> str:
    tickers = " + ".join(escape(leg["ticker"]) for leg in t["legs"])
    pnl = _value(t) - _cost(t)
    return f"• {'✅' if pnl > 0 else '❌'} {_NAMES[t['strategy']]} · {tickers} · {_EXITS[t['exit_reason']]} · <b>{_signed(pnl)}</b>"


def pass_messages(store: Store, cfg: Config, done: dict) -> list[str]:
    """What a pass opened and closed, ready for Telegram; [] when nothing happened."""
    if not done["opened"] and not done["closed"]:
        return []
    trades = store.paper_trades()
    by_id = {t["id"]: t for t in trades}
    lines = ["📝 <b>מסחר על נייר</b>"]
    if done["opened"]:
        lines += ["<b>נקנו</b>"] + [_bought(by_id[i]) for i in done["opened"]]
    if done["closed"]:
        lines += ["<b>נסגרו</b>"] + [_ended(by_id[i]) for i in done["closed"]]
    lines.append(" · ".join(f"קופה {_NAMES[s]} ${cash(trades, s, cfg.paper_bankroll):,.2f}" for s in STRATEGIES))
    messages, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > _LIMIT:
            messages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    return messages + [current]
