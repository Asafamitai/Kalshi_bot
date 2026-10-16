"""The Kalshi subaccount automatic trading runs in, known here as KALSHI_LADDER_AUTO_BOT.

Kalshi numbers subaccounts (1-63) and keeps no names, so the bot remembers
the number of the one it created under that name, in its database.
Automatic trading uses KALSHI_LADDER_AUTO_BOT_SUBACCOUNT if it is set, else
that subaccount, else the primary account (0). Cash moves between it and the
primary account only when asked: `fund` puts money in, `withdraw` takes it
back. Kalshi offers subaccounts only through its API, so these commands are
the way to manage it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

import requests

from .config import Config
from .kalshi import KalshiClient, KalshiError
from .store import Store

NAME = "KALSHI_LADDER_AUTO_BOT"
EXCHANGE = 0  # the exchange shard the subaccount is created and funded on (see KalshiClient)
_KEY = f"subaccount:{NAME}"


def number(store: Store, cfg: Config) -> int:
    if cfg.live_subaccount is not None:
        return cfg.live_subaccount
    return int(store.get_setting(_KEY) or 0)


_PRIMARY = {"ב": "בחשבון הראשי", "מ": "מהחשבון הראשי", "ל": "לחשבון הראשי"}


def name(n: int, prefix: str = "ב") -> str:
    """In Hebrew, with its preposition (ב, מ or ל): "בתת-חשבון KALSHI_LADDER_AUTO_BOT (3)"."""
    return f"{prefix}תת-חשבון {NAME} ({n})" if n else _PRIMARY[prefix]


def name_en(n: int) -> str:
    return f"subaccount {NAME} ({n})" if n else "the primary account"


def _cash(client: KalshiClient, n: int) -> str:
    try:
        cash = client.balance(subaccount=n)
    except (KalshiError, requests.RequestException) as e:
        return f"לא ידוע ({e})"
    return "לא ידוע" if cash is None else f"${cash:,.2f}"


def show(store: Store, client: KalshiClient, cfg: Config) -> str:
    n = number(store, cfg)
    if not client.can_trade:
        return f"המסחר האוטומטי עובד {name(n)}. אין מפתח Kalshi בסביבה, אז אין יתרות להציג."
    lines = [f"המסחר האוטומטי עובד {name(n)}."]
    if n:
        lines.append(f"יתרה {name(n)}: {_cash(client, n)}")
    elif cfg.live_subaccount is None:
        lines.append(f"עוד לא נפתח תת-חשבון {NAME}: /subaccount create")
    lines.append(f"יתרה בחשבון הראשי: {_cash(client, 0)}")
    return "\n".join(lines)


def create(store: Store, client: KalshiClient, cfg: Config) -> str:
    if not client.can_trade:
        return "אין מפתח Kalshi בסביבה, אז אי אפשר לפתוח תת-חשבון."
    n = number(store, cfg)
    if n:
        return f"המסחר האוטומטי כבר עובד {name(n)}; לא נפתח תת-חשבון נוסף."
    try:
        n = client.create_subaccount()
    except (KalshiError, requests.RequestException) as e:
        return f"Kalshi לא פתחה תת-חשבון: {e}"
    store.set_setting(_KEY, str(n))
    return (f"✅ נפתח תת-חשבון {NAME} במספר {n}, והמסחר האוטומטי יעבוד בו מעכשיו.\n"
            f"היתרה בו 0; להעברת כסף אליו: /subaccount fund 100")


def transfer(store: Store, client: KalshiClient, cfg: Config, amount: str, back: bool = False) -> str:
    """`fund` (back=False) moves `amount` dollars from the primary account in; `withdraw` moves it back."""
    if not client.can_trade:
        return "אין מפתח Kalshi בסביבה, אז אי אפשר להעביר כסף."
    n = number(store, cfg)
    if not n:
        return "אין תת-חשבון להעביר אליו או ממנו; לפתוח: /subaccount create"
    try:
        dollars = Decimal(amount.strip().lstrip("$"))
    except (InvalidOperation, AttributeError):
        return f"סכום לא תקין: {amount}"
    if dollars <= 0 or dollars != dollars.quantize(Decimal("0.01")):
        return f"סכום לא תקין: {amount} (דולרים, עד סנטים שלמים)"
    source, target = (n, 0) if back else (0, n)
    what = f"${dollars:,.2f} {name(source, 'מ')} {name(target, 'ל')}"
    try:
        client.transfer(source, target, dollars, str(uuid.uuid4()))  # Kalshi takes a bare UUID only
    except KalshiError as e:
        if e.status >= 500:
            return f"⚠️ לא ברור אם העברת {what} בוצעה: {e}. בדוק עם /subaccount לפני שמנסים שוב."
        return f"Kalshi דחתה את העברת {what}: {e}"
    except requests.RequestException as e:
        return f"⚠️ אין תשובה מ-Kalshi על העברת {what}: {e}. בדוק עם /subaccount לפני שמנסים שוב."
    return f"✅ הועברו {what}.\n" + show(store, client, cfg)
