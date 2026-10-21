"""What a violation looks like: Hebrew for Telegram, terse English for the console."""

from __future__ import annotations

from decimal import Decimal
from html import escape
from urllib.parse import quote

from .fees import fee_per_contract, order_cost
from .ladder import Violation

CENT = Decimal("0.01")


def price(d: Decimal) -> str:
    return f"{d:.2f}" if d == d.quantize(CENT) else format(d.normalize(), "f")


def cents(d: Decimal) -> str:
    c = (d * 100).quantize(Decimal("0.1"))
    return f"{c:.0f}¢" if c == c.to_integral_value() else f"{c}¢"


def qty(d: Decimal) -> str:
    return f"{d:,.0f}" if d >= 10 else format(d.normalize(), "f")


def event_url(event_ticker: str) -> str:
    """Kalshi's series page pointed at the event: no title slug needed, so it can't be wrong."""
    series = event_ticker.split("-")[0]
    return f"https://kalshi.com/markets/{quote(series.lower())}?event_ticker={quote(event_ticker)}"


def pair(v: Violation) -> dict:
    """Buy the safer contract and the other side of the dearer one.

    Together they pay 1 everywhere and 2 in the band between the strikes, so
    the worst case is known up front.
    """
    other = 1 - v.dominated.bid(v.side)
    cost = v.ask + other
    fees = fee_per_contract(v.ask) + fee_per_contract(other)
    return {
        "other_price": other,
        "cost": cost,
        "worst": 1 - cost - fees,
        "best": 2 - cost - fees,
        "size": min(v.ask_size, v.dominated.bid_size(v.side)),
    }


def alert_text(v: Violation) -> str:
    side = v.side.upper()
    other = "NO" if v.side == "yes" else "YES"
    s, d = v.safer, v.dominated
    p = pair(v)
    lines = [
        "🪜 <b>אפשרות חדשה · זול ובטוח יותר</b>",
        f'<a href="{escape(event_url(s.event_ticker))}">{escape(s.title)}</a>',
        f"קנה <b>{side} · {escape(s.label)}</b> ב-<b>{price(v.ask)}</b>"
        f" · עומק {qty(v.ask_size)} · מחזור 24ש׳ {qty(s.volume_24h)}",
        f"בטוח יותר מ-{side} · {escape(d.label)} ({price(d.bid(v.side))} / {price(d.ask(v.side))})",
        f"חיסכון מול האמצע <b>{cents(v.saving)}</b> · נטו אחרי עמלה <b>{cents(v.net_saving)}</b>",
        f"זוג: + {other} · {escape(d.label)} ב-{price(p['other_price'])} = {price(p['cost'])}"
        f" · במקרה הגרוע <b>{cents(p['worst'])}</b> · בטווח {cents(p['best'])} · עד {qty(p['size'])}",
    ]
    if p["worst"] > 0:
        lines.append("🔒 הזוג נעול: מרוויח בכל תוצאה, גם אחרי עמלות.")
    if not v.buyable:
        empty = ", ".join(f"<code>{escape(t)}</code>" for t in v.empty_books)
        lines.append(f"⚠️ ספר ריק ב-{empty}: אין bid מאחורי ה-ask, ולכן אין כפתור קנייה.")
    lines.append("<i>אחד משני המחירים שגוי; הכלל לא אומר איזה.</i>")
    return "\n".join(lines)


def buy_buttons(alert_id: int, v: Violation, sizes: tuple[int, ...]) -> list[list[tuple[str, str]]]:
    """One row: fixed sizes the book can fill, then everything at the ask."""
    depth = int(v.ask_size)
    row = [
        (f"קנה {n} · ${order_cost(v.ask, n):.2f}", f"buy:{alert_id}:{n}")
        for n in sizes
        if 1 <= n <= depth
    ]
    if depth >= 1 and depth not in sizes:
        row.append((f"הכל {depth} · ${order_cost(v.ask, depth):.2f}", f"buy:{alert_id}:all"))
    return [row] if row else []


def console_line(v: Violation) -> str:
    flags = []
    if pair(v)["worst"] > 0:
        flags.append("LOCKED")
    if not v.buyable:
        flags.append("EMPTY-BOOK")
    return (
        f"{cents(v.saving):>6} net {cents(v.net_saving):>6}  {v.side.upper():3}  buy {v.safer.ticker} "
        f"@ {price(v.ask)} (size {qty(v.ask_size)}, vol24h {qty(v.safer.volume_24h)})  vs "
        f"{v.dominated.ticker} [{price(v.dominated.bid(v.side))}/{price(v.dominated.ask(v.side))}]"
        + (f"  {' '.join(flags)}" if flags else "")
    )
