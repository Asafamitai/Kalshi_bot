"""One tap, one order: re-read both rungs, check every guard, then buy the safer contract."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import requests

from .config import Config
from .fees import order_cost
from .format import cents, price, qty
from .kalshi import KalshiClient, KalshiError
from .ladder import Rung, Violation
from .store import Alert, Store


@dataclass(frozen=True)
class Outcome:
    kind: str  # bought | unfilled | dry_run | refused | repriced | rejected | unknown
    message: str
    violation: Violation | None = None


def recheck(
    client: KalshiClient, alert: Alert, min_saving: Decimal, max_spread: Decimal
) -> tuple[Violation | None, str | None]:
    """Fresh violation for the alerted pair, or the reason it can no longer be bought."""
    markets = {m["ticker"]: m for m in client.get_markets([alert.safer, alert.dominated])}
    missing = [t for t in (alert.safer, alert.dominated) if t not in markets]
    if missing:
        return None, f"השוק לא נמצא: {', '.join(missing)}"
    closed = [t for t, m in markets.items() if m.get("status") != "active"]
    if closed:
        return None, f"השוק כבר לא נסחר: {', '.join(closed)}"
    safer = Rung.from_market(markets[alert.safer])
    dominated = Rung.from_market(markets[alert.dominated])
    v = Violation(alert.side, safer, dominated)
    if not dominated.is_priced(alert.side, max_spread):
        return v, "לחוזה היקר כבר אין מחיר אמיתי להשוות אליו"
    if not v.buyable:
        return v, f"ספר ריק ב-{', '.join(v.empty_books)}"
    if v.saving < min_saving:
        return v, f"הסתירה נסגרה (החיסכון עכשיו {cents(v.saving)})"
    return v, None


def buy(client: KalshiClient, store: Store, alert: Alert, choice: str, cfg: Config) -> Outcome:
    v, problem = recheck(client, alert, cfg.min_saving, cfg.max_spread)
    if problem:
        return Outcome("refused", f"לא נקנה: {problem}.", v)
    if v.ask > alert.ask:
        # Never pay more than the price the button showed.
        return Outcome("repriced", f"לא נקנה: ה-ask עלה מ-{price(alert.ask)} ל-{price(v.ask)}. התראה מעודכנת למטה.", v)
    count = int(v.ask_size) if choice == "all" else int(choice)
    if count < 1:
        return Outcome("refused", "לא נקנה: אין עומק ב-ask.", v)
    cost = order_cost(v.ask, count)
    what = f"{count} {v.side.upper()} {v.safer.ticker} ב-{price(v.ask)} (עד ${cost:.2f} כולל עמלה)"
    if not store.trading_on:
        return Outcome("dry_run", f"המסחר כבוי (/trading on): הייתי קונה {what}.", v)
    if not client.can_trade:
        return Outcome("refused", "לא נקנה: אין מפתח Kalshi בסביבה.", v)

    try:
        cash = client.balance()
    except (KalshiError, requests.RequestException):
        cash = None  # an unreadable balance is no reason to block; Kalshi refuses if it's short
    if cash is not None and cost > cash:
        return Outcome("refused", f"לא נקנה: היתרה ${cash:.2f} קטנה מהעלות ${cost:.2f}.", v)

    # Derived from the alert and the choice, so a retry can never buy twice.
    client_order_id = f"lk-{alert.id}-{choice}"
    try:
        result = client.buy(v.safer.ticker, v.side, count, v.ask, client_order_id)
    except KalshiError as e:
        store.log_order(alert.id, v.safer.ticker, v.side, count, v.ask, client_order_id, {"error": str(e)})
        if e.status < 500:
            return Outcome("rejected", f"Kalshi דחתה את ההזמנה ({what}): {e}", v)
        return Outcome("unknown", f"לא ברור אם ההזמנה ({what}) עברה: {e}. בדוק את הפוזיציות ב-Kalshi.", v)
    except requests.RequestException as e:
        store.log_order(alert.id, v.safer.ticker, v.side, count, v.ask, client_order_id, {"error": str(e)})
        return Outcome("unknown", f"אין תשובה מ-Kalshi על ההזמנה ({what}): {e}. בדוק את הפוזיציות ב-Kalshi.", v)
    store.log_order(alert.id, v.safer.ticker, v.side, count, v.ask, client_order_id, result)

    if result.get("already"):
        return Outcome("unknown", "ההזמנה הזו כבר נשלחה קודם; בדוק את הפוזיציות ב-Kalshi.", v)
    filled = result["filled"]
    if filled <= 0:
        return Outcome("unfilled", f"ההזמנה לא התמלאה ({what}); לא נקנה כלום.", v)
    avg = result.get("avg_price") or v.ask
    return Outcome("bought", f"נקנו {qty(filled)} מתוך {count} {v.side.upper()} {v.safer.ticker} במחיר ממוצע {price(avg)}.", v)
