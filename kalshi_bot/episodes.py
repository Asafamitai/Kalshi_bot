"""How long contradictions survive: each one is an episode from the first scan
that sees it to the first scan that doesn't.

Durations are measured as last_seen - opened_at, so they are a lower bound,
resolved to the scan interval: an episode seen by a single scan lasted
somewhere between zero and one interval.

Each episode also records which rung moved to close it. The rule only says one
of the two prices is wrong; if contradictions usually close by the safer ask
rising, the safer contract was the cheap one and buying it had an edge. If they
usually close by the dominated rung's mid falling, that rung was the dear one
and buying the safer contract gained nothing.
"""

from __future__ import annotations

import statistics
import time
from decimal import Decimal

from .format import cents
from .ladder import ONE, ZERO, Rung, Violation
from .store import Store


def update(store: Store, violations: list[Violation], alertable_keys: set, now: float,
           markets: dict[str, dict] | None = None) -> tuple[int, int]:
    """Fold one successful scan into the episode table. Returns (opened, closed).

    `markets` is the scan's open markets by ticker, for the prices at close.
    """
    current = {"|".join(v.key): v for v in violations}
    open_ = store.open_episodes()
    upserts, opened = [], 0
    for key, v in current.items():
        e = open_.get(key)
        if e is None:
            opened += 1
            e = {"key": key, "side": v.side, "safer": v.safer.ticker, "opened_at": now,
                 "first_saving": str(v.saving), "max_saving": str(v.saving),
                 "max_net_saving": str(v.net_saving), "alertable": 0}
        else:
            e = dict(e)
            e["max_saving"] = str(max(float(e["max_saving"]), float(v.saving)))
            e["max_net_saving"] = str(max(float(e["max_net_saving"]), float(v.net_saving)))
        if e.get("dominated") != v.dominated.ticker or e.get("ref_safer_ask") is None:
            # Moves are measured for the pair of rungs that last contradicted,
            # from when they started to.
            e["ref_safer_ask"], e["ref_dominated_mid"] = str(v.ask), str(v.dominated_mid)
        e["dominated"] = v.dominated.ticker
        e["last_seen"] = now
        e["alertable"] = int(bool(e["alertable"]) or v.key in alertable_keys)
        upserts.append(e)
    closes = [dict(e, **closed_by(e, markets or {})) for key, e in open_.items() if key not in current]
    store.save_episodes(upserts, closes, closed_at=now)
    return opened, len(closes)


def closed_by(e: dict, markets: dict[str, dict]) -> dict:
    """Which rung moved to close the contradiction: the safer ask rising, or the dominated mid falling."""
    out = {"close_safer_ask": None, "close_dominated_mid": None, "closed_by": "unknown"}
    ms, md = markets.get(e["safer"]), markets.get(e["dominated"])
    if ms is None or md is None:
        out["closed_by"] = "expired"  # a rung stopped trading
        return out
    side = e["side"]
    safer, dominated = Rung.from_market(ms), Rung.from_market(md)
    if safer is None or dominated is None or not ZERO < safer.ask(side) < ONE or dominated.is_empty(side):
        out["closed_by"] = "book"  # one of the books emptied: no price to compare
        return out
    out["close_safer_ask"], out["close_dominated_mid"] = str(safer.ask(side)), str(dominated.mid(side))
    if e.get("ref_safer_ask") is None:
        return out
    rise = safer.ask(side) - Decimal(e["ref_safer_ask"])
    fall = Decimal(e["ref_dominated_mid"]) - dominated.mid(side)
    if rise <= 0 and fall <= 0:
        out["closed_by"] = "book"  # neither price closed it: the dominated book got too wide to count
    else:
        out["closed_by"] = "safer" if rise > fall else "dominated" if fall > rise else "both"
    return out


def _share(durations: list[float], at_least: float) -> str:
    if not durations:
        return "-"
    return f"{sum(d >= at_least for d in durations) / len(durations):.0%}"


def report(store: Store, since_hours: float = 24 * 7, now: float | None = None) -> str:
    now = now or time.time()
    since = now - since_hours * 3600
    closed = store.closed_episodes(since)
    still_open = store.open_episodes()
    lines = [f"Episodes closed in the last {since_hours:g}h: {len(closed)} "
             f"(still open now: {len(still_open)})"]
    for label, rows in (("all", closed), ("alertable", [e for e in closed if e["alertable"]])):
        durations = [e["last_seen"] - e["opened_at"] for e in rows]
        if not durations:
            lines.append(f"  {label:9}: none")
            continue
        lines.append(
            f"  {label:9}: n={len(durations)}  median {statistics.median(durations) / 60:.0f} min  "
            f"survived >=5m {_share(durations, 300)}  >=15m {_share(durations, 900)}  "
            f">=1h {_share(durations, 3600)}  >=4h {_share(durations, 14400)}"
        )
    alertable = [e for e in closed if e["alertable"]]
    if alertable:
        lines.append("  " + _moves(alertable))
    return "\n".join(lines)


def _moves(rows: list[dict]) -> str:
    """How the alertable contradictions closed: the contract to buy rising, or the reference falling."""
    known = [e for e in rows if e.get("closed_by") in ("safer", "dominated", "both")]
    other = {k: sum(e.get("closed_by") == k for e in rows) for k in ("expired", "book")}
    tail = f"expired {other['expired']}, book emptied or widened {other['book']}"
    if not known:
        return f"closed by: no prices yet ({tail})"
    share = {k: sum(e["closed_by"] == k for e in known) / len(known) for k in ("safer", "dominated", "both")}
    rise = statistics.mean(Decimal(e["close_safer_ask"]) - Decimal(e["ref_safer_ask"]) for e in known)
    fall = statistics.mean(Decimal(e["ref_dominated_mid"]) - Decimal(e["close_dominated_mid"]) for e in known)
    return (f"closed by: safer ask rose {share['safer']:.0%}, dominated mid fell {share['dominated']:.0%}, "
            f"both {share['both']:.0%} (n={len(known)}, mean rise {cents(rise)}, mean fall {cents(fall)}; {tail})")
