"""How long contradictions survive: each one is an episode from the first scan
that sees it to the first scan that doesn't.

Durations are measured as last_seen - opened_at, so they are a lower bound,
resolved to the scan interval: an episode seen by a single scan lasted
somewhere between zero and one interval.
"""

from __future__ import annotations

import statistics
import time

from .ladder import Violation
from .store import Store


def update(store: Store, violations: list[Violation], alertable_keys: set, now: float) -> tuple[int, int]:
    """Fold one successful scan into the episode table. Returns (opened, closed)."""
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
        e["dominated"] = v.dominated.ticker
        e["last_seen"] = now
        e["alertable"] = int(bool(e["alertable"]) or v.key in alertable_keys)
        upserts.append(e)
    closes = [e for key, e in open_.items() if key not in current]
    store.save_episodes(upserts, closes, closed_at=now)
    return opened, len(closes)


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
    return "\n".join(lines)
