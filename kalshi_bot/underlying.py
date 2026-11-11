"""The outcome's own volatility: how likely a market's underlying is to cross its strike before it resolves.

A Kalshi price can sit still while the number it bets on swings, or jump on a
thin book while the number barely moves. What decides a threshold market is
the number itself: a Treasury yield, an oil, metal or gas price, a stock
index, a crypto or currency rate. Kalshi records that number for every
market it settles (`expiration_value`), and some series settle every day or
every hour, so their settled markets are a time series of the underlying,
from the same data source the market resolves on. A series is matched to
its source by the asset's name and a known price suffix, whole (KXBTCMAXY
is Bitcoin's price; KXBTCDOM, its dominance, is not), and a strike more than
four times off the underlying's scale is taken for a different quantity.

For a contract whose series has such a source:

- the latest value is the last one the source settled on, and is no older
  than STALE_HOURS;
- the volatility is the root mean square of its changes, each scaled to one
  hour by the time between the two values (weekends and nights just make a
  longer step), so the expected move until the market resolves is that
  hourly figure times the square root of the hours left;
- z is the distance from the latest value to the strike in those expected
  moves. The bought contract must be the side that pays if the value stays
  where it is, and z at least UNDERLYING_MIN_Z.

A market with no known source, too little history, or a stale one, is not
bought. The model is a plain random walk: it knows nothing of news, and
"touch" markets (a yearly high or low) cross about twice as often as one
that only reads the value at the end.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from decimal import Decimal

import requests

from .kalshi import KalshiClient, KalshiError
from .ladder import Rung
from .zoom import Past, history, pays, series_of

log = logging.getLogger("kalshi_bot")

STALE_HOURS = 96  # a long weekend of a daily source
MIN_POINTS = 8
_CACHE_SECONDS = 1800

_STATES = set("AK AL AR AZ CA CO CT DE FL GA HI IA ID IL IN KS KY LA MA MD ME MI MN MO MS MT NC ND NE NH NJ NM NV NY "
              "OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY".split())


@dataclass(frozen=True)
class Source:
    series: str  # a Kalshi series that settles often on the underlying
    days: float  # how much of its history to read


# Suffixes Kalshi puts on a price series: a range or above/below on a date (D, W, M, Q, Y...), a high or low
# over a period (MAX, MINY, MAXMON...), up/down. A series must be an asset name plus one of these, whole:
# KXBTCMAXY is Bitcoin's price, KXBTCDOM (its dominance) and KXSOLAR are not prices at all.
_SUFFIX = (r"(D|E|EU|Y|W|M|Q|U|Z|AB|B|I|H|AD|AW|AH|MAX|MIN|MAXY|MINY|MAXM|MINM|MAXMM|MAXMON|MINMON|MAXW|MINW|"
           r"MAXD|MAXQ|DIRY|EOY|EOYCLOSE|MON|MONTHLY|YEAR|PRICE|D26)?")


def _asset(names: str, source: str, days: float) -> tuple[re.Pattern, Source]:
    return re.compile(rf"^(KX)?({names}){_SUFFIX}$"), Source(source, days)


# What a series measures, by its ticker. The daily and hourly series are their own sources.
_RULES: list[tuple[re.Pattern, Source]] = [
    # Treasury yields, from the hourly yield series.
    (re.compile(r"^(KX)?(UST10A?[MWD]?|NOTE10[MW]?|TNOTE[DW]?|10YRRATE(H|15M)|10YR\d+PCT|UST10Y\d\d)$"),
     Source("KX10YRRATEH", 5)),
    (re.compile(r"^(KX)?(NOTE30W?|30YRRATE(H|15M)|30YR\d+PCT|30YUSTW)$"), Source("KX30YRRATEH", 5)),
    (re.compile(r"^(KX)?(UST2A|2YRRATE(H|15M))$"), Source("KX2YRRATEH", 5)),
    (re.compile(r"^(KX)?(UST5A|5YRRATE(H|15M))$"), Source("KX5YRRATEH", 5)),
    (re.compile(r"^(KX)?(UST7A|7YRRATE(H|15M))$"), Source("KX7YRRATEH", 5)),
    # Energy and metals, from their daily series.
    _asset("WTI", "KXWTI", 45),
    _asset("BRENT", "KXBRENTD", 45),
    _asset("NATGAS|NGAS", "KXNATGASD", 45),
    _asset("GOLD", "KXGOLDD", 45),
    _asset("SILVER", "KXSILVERD", 45),
    _asset("COPPER", "KXCOPPERD", 45),
    # Stock indices.
    _asset("INX", "KXINX", 45),
    _asset("NASDAQ100|NDQ", "KXNASDAQ100", 45),
    # Crypto, from the hourly above/below series.
    _asset("BTC", "KXBTCD", 5),
    _asset("ETH", "KXETHD", 5),
    _asset("SOL", "KXSOLD", 5),
    _asset("XRP|RIPPLE", "KXXRPD", 5),
    _asset("DOGE", "KXDOGED", 5),
    # Currencies, from their daily series.
    _asset("GBPUSD", "KXGBPUSDAD", 45),
    _asset("USDJPY", "KXUSDJPYAD", 45),
    _asset("USDCAD", "KXUSDCADAD", 45),
    _asset("USDNOK", "KXUSDNOKAD", 45),
    _asset("USDBRL", "KXUSDBRLAD", 45),
    _asset("NZDUSD", "KXNZDUSDAD", 45),
]
_GAS_STATE = re.compile(r"^(KX)?AAAGAS(D|MAX|MIN|M|W|EOY)?([A-Z]{2})$")
_GAS_ANY = re.compile(r"^(KX)?AAAGAS")
_GAS = Source("KXAAAGASD", 45)

# A strike more than this many times the underlying, or less than its inverse, is not about that underlying.
_SANE_RATIO = 4


def source_for(series: str) -> Source | None:
    """The series that settles often on what `series` measures; None if there is none."""
    for pattern, source in _RULES:
        if pattern.match(series):
            return source
    state = _GAS_STATE.match(series)
    if state and state.group(3) in _STATES:
        return Source(f"KXAAAGASD{state.group(3)}", 45)
    if _GAS_ANY.match(series):
        return _GAS
    return None


@dataclass(frozen=True)
class Outlook:
    source: str
    latest: Decimal
    latest_at: float
    hourly: float  # expected move in one hour
    hours_left: float
    z: float  # distance to the strike in expected moves until resolution
    status_quo: bool  # the contract pays if the value stays where it is

    @property
    def expected_move(self) -> float:
        return self.hourly * math.sqrt(self.hours_left)


_cache: dict[str, tuple[float, list[Past] | None]] = {}


def values(client: KalshiClient, source: Source, now: float) -> list[Past] | None:
    """The source's settled values over its window, oldest first; cached. None if unreadable."""
    hit = _cache.get(source.series)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        markets = client.settled_markets(source.series, now - source.days * 86400)
    except (KalshiError, requests.RequestException) as e:
        log.warning("could not read %s's history: %s", source.series, e)
        return None  # not cached: try again next pass
    found = history(markets)
    _cache[source.series] = (now, found)
    return found


def hourly_move(past: list[Past]) -> float | None:
    """Root mean square of the changes, each scaled to one hour; None with too few values."""
    steps = [(float(b.value - a.value), (b.closed_at - a.closed_at) / 3600)
             for a, b in zip(past, past[1:]) if b.closed_at > a.closed_at]
    if len(steps) + 1 < MIN_POINTS:
        return None
    return math.sqrt(sum(d * d / hours for d, hours in steps) / len(steps))


def outlook(client: KalshiClient, rung: Rung, side: str, now: float | None = None) -> Outlook | None:
    """Where the underlying stands against this contract's strike; None without a usable source."""
    now = time.time() if now is None else now
    source = source_for(series_of(rung.event_ticker))
    hours_left = rung.hours_left(now)
    if source is None or hours_left is None or hours_left <= 0:
        return None
    past = values(client, source, now)
    if not past:
        return None
    latest = past[-1]
    hourly = hourly_move(past)
    if hourly is None or (now - latest.closed_at) / 3600 > STALE_HOURS:
        return None
    if latest.value <= 0 or not 1 / _SANE_RATIO <= float(rung.strike / latest.value) <= _SANE_RATIO:
        log.warning("%s's strike %s is not on %s's scale (%s): not the same quantity", rung.ticker, rung.strike,
                    source.series, latest.value)
        return None
    move = hourly * math.sqrt(hours_left)
    distance = abs(float(rung.strike - latest.value))
    z = math.inf if move == 0 else distance / move
    return Outlook(source.series, latest.value, latest.closed_at, hourly, hours_left, z,
                   pays(rung, side, latest.value))
