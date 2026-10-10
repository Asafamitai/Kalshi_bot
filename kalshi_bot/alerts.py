"""Which tracked contradictions are worth a message, and a second read before sending."""

from __future__ import annotations

from .config import Config
from .kalshi import KalshiClient
from .ladder import Rung, Violation
from .store import Store


def option_key(v: Violation) -> str:
    """The thing being proposed is "buy this contract", whatever it is compared against."""
    return f"{v.safer.ticker}|{v.side}"


def alertable(v: Violation, cfg: Config) -> bool:
    return (
        v.net_saving >= cfg.min_net_saving
        and cfg.min_price <= v.ask <= cfg.max_price
        and v.ask_size >= cfg.min_depth
        and v.safer.volume_24h >= cfg.min_volume_24h
    )


def due(v: Violation, store: Store, cfg: Config, now: float) -> bool:
    last = store.last_sent(option_key(v))
    return last is None or now - last >= cfg.cooldown_hours * 3600


def confirm(client: KalshiClient, candidates: list[Violation], cfg: Config) -> list[Violation]:
    """Re-read both rungs of every candidate; keep what still holds, at today's prices.

    A contradiction that is gone a few seconds later was a flicker, not an option.
    """
    if not candidates:
        return []
    tickers = sorted({t for v in candidates for t in (v.safer.ticker, v.dominated.ticker)})
    fresh = {m["ticker"]: m for m in client.get_markets(tickers)}
    kept = []
    for v in candidates:
        ms, md = fresh.get(v.safer.ticker), fresh.get(v.dominated.ticker)
        if not ms or not md or ms.get("status") != "active" or md.get("status") != "active":
            continue
        safer, dominated = Rung.from_market(ms), Rung.from_market(md)
        if safer is None or dominated is None or not dominated.is_priced(v.side, cfg.max_spread):
            continue
        again = Violation(v.side, safer, dominated)
        if again.saving >= cfg.min_saving and alertable(again, cfg):
            kept.append(again)
    return kept


def pick(violations: list[Violation], store: Store, cfg: Config, client: KalshiClient, now: float) -> list[Violation]:
    """Best net saving first, cooldown respected, confirmed, at most max_alerts_per_pass."""
    candidates = [v for v in violations if alertable(v, cfg) and due(v, store, cfg, now)]
    candidates.sort(key=lambda v: v.net_saving, reverse=True)
    # Confirm a few spares so flickers don't leave the pass short.
    confirmed = confirm(client, candidates[: cfg.max_alerts_per_pass * 2], cfg)
    confirmed.sort(key=lambda v: v.net_saving, reverse=True)
    return confirmed[: cfg.max_alerts_per_pass]
