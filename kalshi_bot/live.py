"""Automatic trading with real money: the strict paper book's trades, bought for real.

Off until `/auto on` in Telegram (which needs a Kalshi key). While on, every
pass buys what the strict paper book would: the safer contract of a
contradiction that passes the alert filters, on a book no wider than
PAPER_STRICT_MAX_SPREAD, confirmed by a second read, at most one position per
event. Each buy is one immediate-or-cancel limit order at the ask, sized to
LIVE_MAX_TRADE including the fee, and only with money LIVE_BUDGET still has:
the budget is spent by buys, refilled by what comes back, and never topped
up, so LIVE_BUDGET is the most this can lose. Positions are sold, with an
immediate-or-cancel order at the bid, once the bid holds PAPER_TAKE_PROFIT of
the maximum profit and can take them all, and otherwise held to settlement.

Safety:
- the Kalshi cash balance must be readable and cover the order;
- before a sale, the position on Kalshi is read and no more than it is sold,
  since selling what isn't held would open the other side;
- an order whose outcome is unknown (no answer, or a 5xx) switches automatic
  trading off and says so, so nothing more is bought until someone has
  looked at the account.

Everything it does on Kalshi (orders, the balance, positions) happens in its
subaccount (see subaccounts.py): KALSHI_LADDER_AUTO_BOT, with its own cash
and positions, which keeps it apart from anything bought by hand.

With automatic trading off, no order of any kind is sent, but what is held is
still marked at its bid and settled when its market resolves.
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from html import escape

import requests

from . import alerts, paper, subaccounts
from .config import Config
from .fees import order_cost, order_fee
from .format import event_url, price
from .kalshi import KalshiClient, KalshiError
from .ladder import ZERO, Rung, Violation
from .store import Store

log = logging.getLogger("kalshi_bot")


def budget_left(positions: list[dict], budget: Decimal) -> Decimal:
    """The budget, less what was paid, plus what came back."""
    return budget - sum((p["cost"] for p in positions), ZERO) + sum((p["proceeds"] for p in positions), ZERO)


def step(store: Store, client: KalshiClient, cfg: Config, violations: list[Violation],
         markets: dict[str, dict], now: float) -> dict:
    """One pass: mark, sell and settle what is held, then buy what the strict book would.

    Returns the ids of the positions opened and closed, and what went wrong.
    """
    done = {"opened": [], "closed": [], "problems": []}
    marks, gone = {}, []
    for p in store.live_positions():
        if p["status"] != "open":
            continue
        m = markets.get(p["ticker"])
        if m is None:
            gone.append(p)
            continue
        rung = Rung.from_market(m)
        bid, depth = (rung.bid(p["side"]), rung.bid_size(p["side"])) if rung else (ZERO, ZERO)
        marks[p["id"]] = bid
        if _trading(store, client) and paper.take_profit(p, bid, depth, cfg.paper_take_profit):
            _sell(store, client, p, bid, subaccounts.number(store, cfg), now, done)
    store.set_live_marks(marks)
    if gone:
        fresh = {m["ticker"]: m for m in client.get_markets(sorted({p["ticker"] for p in gone}))}
        for p in gone:
            value = paper.settlement(fresh.get(p["ticker"]), p["side"])
            if value is not None and store.reduce_live_position(p["id"], p["count"], value * p["count"], "settled", now):
                done["closed"].append(p["id"])
    if _trading(store, client):
        _open(store, client, cfg, violations, now, done)
    return done


def _trading(store: Store, client: KalshiClient) -> bool:
    # Asked again before every order: an order with an unknown outcome switches it off mid-pass.
    return store.auto_on and client.can_trade


def _order_id() -> str:
    # New for every order; the client re-sends the same body (and id) on a 429, so that can't fill twice.
    return f"auto-{uuid.uuid4().hex[:16]}"


def _unknown(store: Store, done: dict, what: str, e: Exception) -> None:
    """An order that may or may not have gone through: stop until someone looks."""
    store.set_setting("auto", "off")
    done["problems"].append(f"לא ברור אם ההזמנה ({what}) עברה: {e}. המסחר האוטומטי כובה; "
                            "בדוק את הפוזיציות ב-Kalshi והפעל מחדש עם /auto on.")


def _send(store: Store, client: KalshiClient, action: str, ticker: str, side: str, count: int, limit: Decimal,
          subaccount: int, done: dict) -> dict | None:
    """One order, logged; None if it was refused or its outcome is unknown (reported in `done`)."""
    what = f"{'קנייה' if action == 'buy' else 'מכירה'} {count} {side.upper()} {ticker} ב-{price(limit)}"
    coid = _order_id()
    send = client.buy if action == "buy" else client.sell
    try:
        result = send(ticker, side, count, limit, coid, subaccount=subaccount)
    except KalshiError as e:
        store.log_order(0, ticker, side, count, limit, coid, {"action": action, "error": str(e)})
        if e.status < 500:
            done["problems"].append(f"Kalshi דחתה את ההזמנה ({what}): {e}")
        else:
            _unknown(store, done, what, e)
        return None
    except requests.RequestException as e:
        store.log_order(0, ticker, side, count, limit, coid, {"action": action, "error": str(e)})
        _unknown(store, done, what, e)
        return None
    store.log_order(0, ticker, side, count, limit, coid, {"action": action, **result})
    if result.get("already"):
        _unknown(store, done, what, RuntimeError("Kalshi says this order was already sent"))
        return None
    return result


def _sell(store: Store, client: KalshiClient, p: dict, bid: Decimal, subaccount: int, now: float,
          done: dict) -> None:
    try:
        held = client.position(p["ticker"], p["side"], subaccount=subaccount)
    except (KalshiError, requests.RequestException) as e:
        done["problems"].append(f"לא הצלחתי לקרוא את הפוזיציה ב-{p['ticker']} לפני מכירה: {e}")
        return
    if held is None:
        done["problems"].append(f"לא הצלחתי לקרוא את הפוזיציה ב-{p['ticker']}; לא נמכר.")
        return
    count = min(p["count"], int(held))
    if count < 1:
        # Sold or closed outside the bot: what it brought back is unknown, so it counts as nothing.
        if store.reduce_live_position(p["id"], p["count"], ZERO, "missing", now):
            done["closed"].append(p["id"])
        done["problems"].append(f"{p['count']} {p['side'].upper()} {p['ticker']} כבר לא מוחזקים ב-Kalshi; "
                                "הפוזיציה נסגרה בלי תמורה.")
        return
    result = _send(store, client, "sell", p["ticker"], p["side"], count, bid, subaccount, done)
    if result is None:
        return
    sold = int(result["filled"])
    if sold < 1:
        return  # the bid moved; try again next pass
    avg = result.get("avg_price") or bid
    if store.reduce_live_position(p["id"], sold, avg * sold - order_fee(avg, sold), "sold", now):
        done["closed"].append(p["id"])


def _open(store: Store, client: KalshiClient, cfg: Config, violations: list[Violation], now: float,
          done: dict) -> None:
    positions = store.live_positions()
    busy = {p["event"] for p in positions if p["status"] == "open"}
    want = [v for v in violations
            if v.safer.event_ticker not in busy and paper.wanted(paper.STRICT, v, cfg, now)]
    if not want:
        return
    left = budget_left(positions, cfg.live_budget)
    if left <= ZERO:
        return  # the budget is spent
    picks = [v for v in alerts.reread(client, want, cfg) if paper.wanted(paper.STRICT, v, cfg, now)]
    if not picks:
        return
    account = subaccounts.number(store, cfg)
    try:
        cash = client.balance(subaccount=account)
    except (KalshiError, requests.RequestException) as e:
        cash = None
        log.warning("could not read the Kalshi balance: %s", e)
    if cash is None:
        done["problems"].append("לא הצלחתי לקרוא את היתרה ב-Kalshi, אז לא נקנה כלום בסבב הזה.")
        return
    picks.sort(key=lambda v: v.net_saving, reverse=True)
    for v in picks:
        if v.safer.event_ticker in busy or not _trading(store, client):
            continue
        legs = paper.legs(paper.STRICT, v, min(cfg.live_max_trade, left, cash))
        if not legs:
            continue
        ((ticker, side, count, ask, _, mid),) = legs
        result = _send(store, client, "buy", ticker, side, count, ask, account, done)
        if result is None:
            continue
        filled = int(result["filled"])
        if filled < 1:
            continue  # the ask moved before the order arrived
        avg = result.get("avg_price") or ask
        cost = order_cost(avg, filled)
        done["opened"].append(store.add_live_position(v, filled, avg, cost, mid, result.get("order_id"), now))
        left -= cost
        cash -= cost
        busy.add(v.safer.event_ticker)


# Report


def _value(p: dict) -> Decimal:
    """What came back, plus what is still held at its last bid (at cost until first marked)."""
    mark = p["mark"] if p["mark"] is not None else p["price"]
    return p["proceeds"] + (p["count"] * mark if p["status"] == "open" else ZERO)


def _signed(d: Decimal) -> str:
    return f"{'+' if d >= 0 else '-'}${abs(d):,.2f}"


def report(store: Store, cfg: Config, latest: int = 8) -> str:
    positions = store.live_positions()
    left = budget_left(positions, cfg.live_budget)
    lines = [f"Automatic trading (strict, real money): {'ON' if store.auto_on else 'off'} in "
             f"{subaccounts.name_en(subaccounts.number(store, cfg))}; "
             f"up to ${cfg.live_max_trade:,.2f} a trade, budget ${cfg.live_budget:,.2f}, left ${left:,.2f}"]
    if not positions:
        lines.append("no positions yet")
        return "\n".join(lines)
    closed = [p for p in positions if p["status"] == "closed"]
    open_ = [p for p in positions if p["status"] == "open"]
    lines.append(f"{len(positions)} positions, {len(open_)} open")
    if closed:
        pnl = [_value(p) - p["cost"] for p in closed]
        cost = sum(p["cost"] for p in closed)
        won = sum(x > 0 for x in pnl)
        lines.append(f"  closed {len(closed)}: won {won}, lost {len(closed) - won}, P&L {_signed(sum(pnl))} "
                     f"on ${cost:,.2f} ({sum(pnl) / cost:+.1%})")
    if open_:
        cost = sum(p["cost"] for p in open_)
        value = sum(_value(p) for p in open_)
        lines.append(f"  open: cost ${cost:,.2f}, at the bid ${value:,.2f} ({_signed(value - cost)})")
    lines.append("Latest (UTC):")
    for p in reversed(positions[-latest:]):
        when = time.strftime("%m-%d %H:%M", time.gmtime(p["opened_at"]))
        state = f"{p['exit_reason']} {_signed(_value(p) - p['cost'])}" if p["status"] == "closed" \
            else f"open, at the bid {_signed(_value(p) - p['cost'])}"
        lines.append(f"  {when} {p['bought']} {p['side'].upper()} {p['ticker']} @{price(p['price'])}  "
                     f"${p['cost']:,.2f}  {state}")
    return "\n".join(lines)


# Telegram: one message per pass with what happened, in Hebrew like the alerts.

_EXITS = {"sold": "נמכר ביעד הרווח", "settled": "הוכרע", "missing": "לא נמצא ב-Kalshi"}


def _link(p: dict) -> str:
    """The position's ticker, linked to its event on Kalshi."""
    return f'<a href="{escape(event_url(p["event"]))}">{escape(p["ticker"])}</a>'


def open_positions_html(store: Store) -> str:
    """The open positions, each linked to its market on Kalshi, for Telegram; "" when there are none."""
    open_ = [p for p in store.live_positions() if p["status"] == "open"]
    if not open_:
        return ""
    lines = ["<b>פוזיציות פתוחות</b>"]
    for p in open_:
        pnl = _value(p) - p["cost"]
        lines.append(f"• {p['count']} {p['side'].upper()} {_link(p)} ב-{price(p['price'])} · ${p['cost']:,.2f}"
                     f" · לפי ה-bid {_signed(pnl)}")
    return "\n".join(lines)


def pass_message(store: Store, cfg: Config, done: dict) -> str | None:
    """What a pass bought, closed and couldn't do, ready for Telegram; None when nothing happened."""
    if not done["opened"] and not done["closed"] and not done["problems"]:
        return None
    positions = store.live_positions()
    by_id = {p["id"]: p for p in positions}
    lines = ["💵 <b>מסחר אוטומטי · כסף אמיתי</b>"]
    if done["opened"]:
        lines.append("<b>נקנו</b>")
        for i in done["opened"]:
            p = by_id[i]
            lines.append(f'• {p["bought"]} {p["side"].upper()} {_link(p)}'
                         f" ב-{price(p['price'])} · ${p['cost']:,.2f} · חיסכון {p['saving'] * 100:.1f}¢")
    if done["closed"]:
        lines.append("<b>נסגרו</b>")
        for i in done["closed"]:
            p = by_id[i]
            pnl = _value(p) - p["cost"]
            lines.append(f"• {'✅' if pnl > 0 else '❌'} {_link(p)} · {_EXITS[p['exit_reason']]} · "
                         f"<b>{_signed(pnl)}</b>")
    if done["problems"]:
        lines.append("<b>⚠️ בעיות</b>")
        lines += [f"• {escape(x)}" for x in done["problems"]]
    lines.append(f"נשאר בתקציב ${budget_left(positions, cfg.live_budget):,.2f} מתוך ${cfg.live_budget:,.2f}"
                 + ("" if store.auto_on else " · המסחר האוטומטי כבוי"))
    return "\n".join(lines)
