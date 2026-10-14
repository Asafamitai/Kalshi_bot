"""Calm markets only: the strict strategy's guard against prices that are still on the move.

A contradiction in a market whose price swings is often a rung that hasn't
caught up yet, and the cheap side is cheap for a reason. Three checks, each
switched off by a 0:

- the mid of both rungs moved at most CALM_MAX_MOVE_24H in the last day
  (Kalshi gives every market its book of 24 hours ago, so this costs nothing);
- the market resolves at least CALM_MIN_HOURS_LEFT away (a week by default,
  which also leaves out game-day sports and daily crypto);
- the contract bought stayed within CALM_MAX_RANGE over CALM_HISTORY_DAYS of
  hourly candles. That one is a call per candidate, so it runs only on what
  is about to be bought, and its answer is kept for a few minutes so the paper
  twin doesn't ask again.

A rung with no book a day ago, or a history that can't be read, is not calm.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import requests

from .config import Config
from .kalshi import KalshiClient, KalshiError
from .ladder import ONE, ZERO, Violation, to_decimal

log = logging.getLogger("kalshi_bot")

_CACHE_SECONDS = 600
_cache: dict[str, tuple[float, tuple[Decimal, Decimal] | None]] = {}


def steady(v: Violation, cfg: Config, now: float | None = None) -> bool:
    """The checks the scan's own data answers: the 24-hour move of both rungs, and the horizon."""
    now = time.time() if now is None else now
    if cfg.calm_min_hours_left > 0:
        left = v.safer.hours_left(now)
        if left is not None and left < cfg.calm_min_hours_left:
            return False
    if cfg.calm_max_move_24h > 0:
        for rung in (v.safer, v.dominated):
            moved = rung.move_24h()
            if moved is None or moved > cfg.calm_max_move_24h:
                return False
    return True


def price_bounds(candles: list[dict]) -> tuple[Decimal, Decimal] | None:
    """Lowest and highest YES mid at the close of each candle with a two-sided book; None if there are none."""
    mids = []
    for c in candles:
        bid = to_decimal((c.get("yes_bid") or {}).get("close_dollars"))
        ask = to_decimal((c.get("yes_ask") or {}).get("close_dollars"))
        if ZERO < bid < ask < ONE:
            mids.append((bid + ask) / 2)
    return (min(mids), max(mids)) if mids else None


def history_bounds(client: KalshiClient, ticker: str, cfg: Config, now: float) -> tuple[Decimal, Decimal] | None:
    """The contract's mid bounds over CALM_HISTORY_DAYS; cached for a few minutes. None if unreadable or empty."""
    hit = _cache.get(ticker)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        candles = client.candlesticks(ticker, now - cfg.calm_history_days * 86400, now)
    except (KalshiError, requests.RequestException) as e:
        log.warning("could not read the price history of %s: %s", ticker, e)
        return None  # not cached: try again next pass
    found = price_bounds(candles)
    _cache[ticker] = (now, found)
    return found


def settled_history(client: KalshiClient, v: Violation, cfg: Config, now: float) -> bool:
    """The contract bought stayed within CALM_MAX_RANGE over CALM_HISTORY_DAYS, its mid now included."""
    if cfg.calm_max_range <= 0 or cfg.calm_history_days <= 0:
        return True
    bounds = history_bounds(client, v.safer.ticker, cfg, now)
    if bounds is None:
        return False
    lo, hi = bounds
    if not v.safer.is_empty("yes"):
        mid = v.safer.mid("yes")
        lo, hi = min(lo, mid), max(hi, mid)
    return hi - lo <= cfg.calm_max_range
