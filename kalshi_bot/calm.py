"""Calm markets only: the strict strategy buys only where the outcome is unlikely to flip before it resolves.

Two checks, each switched off by a 0:

- the market resolves at least CALM_MIN_HOURS_LEFT away (a week by default,
  which also leaves out game-day sports and daily crypto);
- its underlying (a yield, an oil or gas price) sits at least
  UNDERLYING_MIN_Z expected moves from the strike, on the side the contract
  pays on (see underlying.py). A market with no known underlying, or one
  whose history can't be read, is not bought. That is a read of the source's
  settled markets, so it runs only on what is about to be bought, and the
  answer is kept for a while so the paper twin doesn't ask again.
"""

from __future__ import annotations

import logging
import time

from . import underlying
from .config import Config
from .kalshi import KalshiClient
from .ladder import Violation

log = logging.getLogger("kalshi_bot")


def steady(v: Violation, cfg: Config, now: float | None = None) -> bool:
    """The check the scan's own data answers: enough time before the market resolves."""
    if cfg.calm_min_hours_left <= 0:
        return True
    left = v.safer.hours_left(time.time() if now is None else now)
    return left is None or left >= cfg.calm_min_hours_left


def outcome_steady(client: KalshiClient, v: Violation, cfg: Config, now: float) -> bool:
    """The underlying sits far enough from the strike, on the side the contract pays on."""
    if cfg.underlying_min_z <= 0:
        return True
    o = underlying.outlook(client, v.safer, v.side, now)
    if o is None:
        log.info("calm: no usable underlying for %s, not bought", v.safer.ticker)
        return False
    ok = o.status_quo and o.z >= cfg.underlying_min_z
    log.info("calm: %s %s strike %s vs %s at %s, expected move %.4g in %.0fh, z %.2f, %s",
             v.side.upper(), v.safer.ticker, v.safer.strike, o.source, o.latest, o.expected_move, o.hours_left,
             o.z, "ok" if ok else "pays only if it moves" if not o.status_quo else "too close")
    return ok
