"""Human-readable renderings of a violation, for Telegram and the console."""

from __future__ import annotations

from decimal import Decimal
from html import escape

from .ladder import Violation

CENT = Decimal("0.01")


def price(d: Decimal) -> str:
    return f"{d:.2f}" if d == d.quantize(CENT) else format(d.normalize(), "f")


def cents(d: Decimal) -> str:
    c = d * 100
    return f"{c:.0f}¢" if c == c.to_integral_value() else f"{format(c.normalize(), 'f')}¢"


def alert_text(v: Violation) -> str:
    side = v.side.upper()
    s, d = v.safer, v.dominated
    lines = [
        f"🪜 <b>Ladder contradiction</b> · {escape(s.title)}",
        f"Buy <b>{side} · {escape(s.label)}</b> @ <b>{price(v.ask)}</b> (size {v.ask_size.normalize():f})",
        f"Safer than <b>{side} · {escape(d.label)}</b>, mid {price(v.dominated_mid)} "
        f"(bid {price(d.bid(v.side))} / ask {price(d.ask(v.side))})",
        f"Saving vs that mid: <b>{cents(v.saving)}</b>",
    ]
    if v.bid_through:
        lines.append("⚡ The ask is below the dominated rung's bid, so buying one and selling the other locks it in.")
    if not v.buyable:
        empty = ", ".join(f"<code>{escape(t)}</code>" for t in v.empty_books)
        lines.append(f"⚠️ Empty book on {empty}: not a real price, so no buy button.")
    lines.append("<i>One of these two quotes is wrong; the rule doesn't say which.</i>")
    lines.append(f"<code>{escape(s.ticker)}</code> vs <code>{escape(d.ticker)}</code>")
    return "\n".join(lines)


def console_line(v: Violation) -> str:
    flags = []
    if v.bid_through:
        flags.append("BID-THROUGH")
    if not v.buyable:
        flags.append("EMPTY-BOOK")
    return (
        f"{cents(v.saving):>6}  {v.side.upper():3}  buy {v.safer.ticker} @ {price(v.ask)} "
        f"(size {v.ask_size.normalize():f})  vs {v.dominated.ticker} mid {price(v.dominated_mid)} "
        f"[{price(v.dominated.bid(v.side))}/{price(v.dominated.ask(v.side))}]"
        + (f"  {' '.join(flags)}" if flags else "")
    )
