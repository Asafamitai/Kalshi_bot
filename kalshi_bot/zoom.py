"""A closer look at one position: its ladder now, its reference rung, and how the series' past events settled.

The scan only proves that two prices in one ladder contradict each other; it
doesn't say which of them is wrong. This puts numbers on that question:

- the ladder now, rung by rung, with how much each one trades: a reference
  rung that barely trades has a mid that means nothing, and the saving
  measured against it proves nothing;
- how the series' recent events settled (Kalshi's `expiration_value`), how
  often the contract bought would have paid, and what that is worth against
  the price paid;
- the band between the two strikes, which the safer contract pays in and the
  dearer one doesn't: the market prices it at nothing (that is the
  contradiction), so how often it happened says which rung is wrong. Often:
  the dearer rung is, and the safer contract really was cheap. Hardly ever:
  the two are worth about the same, and the saving is smaller than it looks.

History is a base rate, not a forecast: a series can change regime, and the
market may know something the past doesn't.
"""

from __future__ import annotations

import html
import re
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape

from .config import Config
from .fees import fee_per_contract
from .format import cents, event_url, price, qty
from .kalshi import KalshiClient
from .ladder import ONE, ZERO, Rung, ladder_violations, to_epoch

HISTORY_DAYS = 60
# The latest events, which say more about the series as it is now than the whole window.
RECENT = 7
# How many past events the message lists.
SHOWN = 12
# Fewer settled events than this, and history says nothing.
MIN_HISTORY = 3
# The band between the strikes counts as happening "often" from this share up.
OFTEN = Decimal("0.15")
# A gap this big between history and the market is worth a warning either way.
GAP = Decimal("0.20")


@dataclass(frozen=True)
class Past:
    event: str
    closed_at: float
    value: Decimal


@dataclass(frozen=True)
class Zoom:
    rung: Rung  # what was bought
    side: str
    ladder: list[Rung]  # its ladder now, by strike
    reference: Rung | None  # the dearer rung it was measured against, if any
    past: list[Past]  # the series' settled events, oldest first
    paid: Decimal  # per contract, fee included
    count: int | None = None  # contracts held, for a position


def series_of(event_ticker: str) -> str:
    return event_ticker.split("-")[0]


def parse_value(raw) -> Decimal | None:
    """An expiration value as a number ("24.4", "1,250", "3.5%"); None if it isn't one."""
    text = str(raw if raw is not None else "").strip().replace(",", "").rstrip("%").lstrip("$")
    try:
        d = Decimal(text)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def pays(rung: Rung, side: str, value: Decimal) -> bool:
    """Whether `side` of `rung` pays when the event settles at `value`."""
    if rung.strike_type == "greater":
        yes = value > rung.strike
    elif rung.strike_type == "greater_or_equal":
        yes = value >= rung.strike
    elif rung.strike_type == "less":
        yes = value < rung.strike
    else:
        yes = value <= rung.strike
    return yes if side == "yes" else not yes


def history(markets: list[dict], skip_event: str | None = None) -> list[Past]:
    """One value per settled event (every market of an event settles on the same one), oldest first."""
    by_event: dict[str, Past] = {}
    for m in markets:
        event = m.get("event_ticker")
        if not event or event == skip_event or event in by_event:
            continue
        value, closed = parse_value(m.get("expiration_value")), to_epoch(m.get("close_time"))
        if value is not None and closed is not None:
            by_event[event] = Past(event, closed, value)
    return sorted(by_event.values(), key=lambda p: p.closed_at)


def build(client: KalshiClient, cfg: Config, ticker: str, side: str, reference: str | None = None,
          paid: Decimal | None = None, count: int | None = None, now: float | None = None) -> Zoom:
    """Read what a zoom needs: the market's ladder and the series' settled events.

    Without a `reference`, the rung the scan would compare it against now (if any).
    Without `paid`, the ask now plus the fee.
    """
    now = time.time() if now is None else now
    found = client.get_markets([ticker])
    if not found:
        raise LookupError(f"השוק {ticker} לא נמצא")
    rung = Rung.from_market(found[0])
    if rung is None:
        raise LookupError(f"{ticker} הוא לא מדרגה בסולם (רק שווקי 'מעל' או 'מתחת' לסף)")
    ladder = sorted((r for r in map(Rung.from_market, client.event_markets(rung.event_ticker))
                     if r is not None and r.ladder_key == rung.ladder_key), key=lambda r: r.strike)
    if not any(r.ticker == ticker for r in ladder):
        ladder = sorted(ladder + [rung], key=lambda r: r.strike)
    ref = next((r for r in ladder if r.ticker == reference), None) if reference else _current_reference(
        ladder, ticker, side, cfg)
    past = history(client.settled_markets(series_of(rung.event_ticker), now - HISTORY_DAYS * 86400),
                   skip_event=rung.event_ticker)
    if paid is None:
        ask = rung.ask(side)
        paid = ask + fee_per_contract(ask)
    return Zoom(rung, side, ladder, ref, past, paid, count)


def for_position(client: KalshiClient, cfg: Config, p: dict, now: float | None = None) -> Zoom:
    """A zoom on one automatic trading position, at what it paid per contract with the fee."""
    paid = p["cost"] / p["bought"] if p["bought"] else p["price"]
    return build(client, cfg, p["ticker"], p["side"], reference=p["dominated"], paid=paid,
                 count=p["count"], now=now)


def _current_reference(ladder: list[Rung], ticker: str, side: str, cfg: Config) -> Rung | None:
    for v in ladder_violations(ladder, cfg.min_saving, cfg.max_spread):
        if v.safer.ticker == ticker and v.side == side:
            return v.dominated
    return None


# Reading it


def hit_rate(z: Zoom, past: list[Past]) -> tuple[int, int]:
    """How many of `past` the contract bought would have paid in, out of how many."""
    return sum(pays(z.rung, z.side, p.value) for p in past), len(past)


def band(z: Zoom) -> tuple[Decimal, int, int] | None:
    """The band the safer contract pays in and the reference doesn't: its price now, and how often it happened."""
    if z.reference is None:
        return None
    implied = z.rung.mid(z.side) - z.reference.mid(z.side)
    inside = sum(pays(z.rung, z.side, p.value) and not pays(z.reference, z.side, p.value) for p in z.past)
    return implied, inside, len(z.past)


def _pct(n: int, of: int) -> str:
    return f"{n / of:.0%}" if of else "—"


def _num(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _short(event: str) -> str:
    return event.split("-", 1)[1] if "-" in event else event


def render(z: Zoom, cfg: Config) -> str:
    """The zoom as one Telegram message, in Hebrew like the rest of the bot."""
    side = z.side.upper()
    r = z.rung
    lines = [f'🔍 <b>זום אין</b> · <a href="{escape(event_url(r.event_ticker))}">{escape(r.title)}</a>']
    if z.count:
        lines.append(f"<b>{z.count} {side} · {escape(r.label)}</b> ב-{cents(z.paid)} כולל עמלה · "
                     f"עלות ${z.paid * z.count:,.2f} · רווח מקסימלי ${(ONE - z.paid) * z.count:,.2f}")
    else:
        lines.append(f"<b>{side} · {escape(r.label)}</b> · {cents(z.paid)} לחוזה כולל עמלה")

    rows = []
    for x in z.ladder:
        mark = "▶" if x.ticker == r.ticker else "◆" if z.reference and x.ticker == z.reference.ticker else " "
        rows.append(f"{mark} {_num(x.strike):>8}  {price(x.bid(z.side))}/{price(x.ask(z.side))}  "
                    f"vol {qty(x.volume_24h):>5}  oi {qty(x.open_interest):>5}")
    lines.append(f"<b>הסולם עכשיו</b> ({side} bid/ask · מחזור 24ש׳ · OI · ▶ נקנה · ◆ ייחוס)")
    lines.append("<pre>" + escape("\n".join(rows)) + "</pre>")

    ref = z.reference
    if ref is not None:
        lines.append(f"<b>מדרגת הייחוס</b>: {escape(ref.label)} · {price(ref.bid(z.side))}/{price(ref.ask(z.side))}"
                     f" (אמצע {price(ref.mid(z.side))}) · מחזור 24ש׳ {qty(ref.volume_24h)} · OI {qty(ref.open_interest)}")
        if ref.volume_24h < cfg.min_ref_volume_24h:
            lines.append(f"⚠️ מדרגת הייחוס כמעט לא נסחרת (פחות מ-{qty(cfg.min_ref_volume_24h)} ביממה): "
                         "האמצע שלה הוא לא מחיר, והחיסכון מולו לא מוכח.")
    else:
        lines.append("אין עכשיו מדרגה יקרה יותר שהחוזה הזה בטוח ממנה.")

    if len(z.past) < MIN_HISTORY:
        lines.append(f"אין מספיק אירועים מוכרעים בסדרה ב-{HISTORY_DAYS} הימים האחרונים "
                     f"({len(z.past)}) כדי להשוות להיסטוריה.")
        lines.append("<i>אחד משני המחירים שגוי; בלי היסטוריה אי אפשר לומר איזה.</i>")
        return "\n".join(lines)

    shown = z.past[-SHOWN:]
    rows = [f"{_short(p.event):>10}  {_num(p.value):>8}  {'✅' if pays(r, z.side, p.value) else '❌'}" for p in shown]
    lines.append(f"<b>איך הוכרעו האירועים הקודמים</b> ({len(shown)} אחרונים · ✅ החוזה היה משלם)")
    lines.append("<pre>" + escape("\n".join(rows)) + "</pre>")

    hits, n = hit_rate(z, z.past)
    recent = z.past[-RECENT:]
    rhits, rn = hit_rate(z, recent)
    values = [p.value for p in recent]
    mean = statistics.fmean(float(v) for v in values)
    sd = statistics.stdev(float(v) for v in values) if len(values) > 1 else 0.0
    last = z.past[-1].value
    lines.append(f"היה משלם ב-<b>{hits}/{n}</b> ({_pct(hits, n)}) ב-{HISTORY_DAYS} יום · "
                 f"ב-{rn} האחרונים <b>{rhits}/{rn}</b> ({_pct(rhits, rn)})")
    lines.append(f"ב-{rn} האחרונים: ממוצע {mean:.4g} ± {sd:.2g} · האחרון {_num(last)} · הסף {_num(r.strike)}")

    b = band(z)
    if b is not None:
        implied, inside, of = b
        lines.append(f"<b>הטווח בין {escape(r.label)} ל-{escape(ref.label)}</b>: השוק מתמחר אותו "
                     f"ב-{cents(max(implied, ZERO))} · בהיסטוריה {inside}/{of} ({_pct(inside, of)})")
        if Decimal(inside) / of >= max(implied, ZERO) + OFTEN:
            lines.append("✅ הטווח קורה לא מעט: המדרגה היקרה היא השגויה, והחוזה שנקנה זול באמת.")
        else:
            lines.append("⚖️ הטווח כמעט לא קורה: שתי המדרגות שוות בערך אותו דבר, והחיסכון קטן ממה שנראה.")

    rate, rrate = Decimal(hits) / n, Decimal(rhits) / rn
    ev = rate - z.paid
    icon = "✅" if min(rate, rrate) > z.paid else "❌" if max(rate, rrate) < z.paid else "⚖️"
    bottom = (f"{icon} <b>שורה תחתונה</b>: לפי ההיסטוריה {_pct(hits, n)} לתשלום מול {cents(z.paid)} ששולמו · "
              f"תוחלת {'+' if ev >= 0 else '−'}{cents(abs(ev))} לחוזה")
    if z.count:
        bottom += f" (≈ {'+' if ev >= 0 else '−'}${abs(ev) * z.count:,.2f} על הפוזיציה)"
    lines.append(bottom)

    if sd > 0 and abs(float(last - r.strike)) < sd / 2:
        lines.append(f"⚠️ קרוב לקצה: האירוע האחרון הוכרע ב-{_num(last)}, פחות מחצי סטיית תקן מהסף.")
    market = r.mid(z.side)
    if rrate - market >= GAP:
        lines.append(f"⚠️ השוק ({cents(market)}) הרבה מתחת להיסטוריה האחרונה: אולי הוא יודע משהו שאין בה "
                     "(נתון חלקי, שינוי מגמה). כדאי לבדוק את המקור.")
    elif market - rrate >= GAP:
        lines.append(f"⚠️ השוק ({cents(market)}) הרבה מעל ההיסטוריה האחרונה: או שהוא יודע משהו, "
                     "או שהחוזה יקר.")
    lines.append("<i>היסטוריה היא שיעור בסיס, לא תחזית.</i>")
    return "\n".join(lines)


def plain(text: str) -> str:
    """A rendered zoom for the console: the same words without the HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))
