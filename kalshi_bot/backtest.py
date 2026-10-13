"""Replay the strategy on recently settled markets using 1-minute candlesticks.

For each settled market, walk the minute candles in the final
`max_seconds_to_close` before close and take the first minute where the
strategy would have bought. Then score that entry against the real result.

Limits of this approximation:
- Quotes are minute-close snapshots; real fills could be worse.
- Book depth is unknown, so every entry is assumed to fill in full.
- Markets that closed *early* (e.g. a game ended) look like they were always
  going to close then, which the live bot could not have known.
"""

from __future__ import annotations

import csv
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from .client import KalshiClient, KalshiError
from .ledger import summarize
from .strategy import StrategyParams, evaluate_market, net_profit_if_win, parse_time, size_order, taker_fee, to_float

log = logging.getLogger("kalshi_bot.backtest")


def series_of(event_ticker: str) -> str:
    return event_ticker.split("-", 1)[0]


def candle_quote(candle: dict, side: str) -> float | None:
    value = (candle.get(side) or {}).get("close_dollars")
    return None if value is None else to_float(value)


def simulate_entry(market: dict, candles: list[dict], p: StrategyParams, stake: float) -> dict | None:
    """First minute in the window where the strategy fires, scored against the result."""
    for candle in sorted(candles, key=lambda c: c["end_period_ts"]):
        bid, ask = candle_quote(candle, "yes_bid"), candle_quote(candle, "yes_ask")
        if bid is None or ask is None:
            continue
        at = datetime.fromtimestamp(candle["end_period_ts"], tz=timezone.utc)
        snapshot = {
            **market,
            "status": "active",
            "yes_bid_dollars": bid,
            "yes_ask_dollars": ask,
            "no_ask_dollars": round(1 - bid, 4),
            "yes_ask_size_fp": 1e9,  # depth unknown in candles
            "yes_bid_size_fp": 1e9,
        }
        c = evaluate_market(snapshot, at, p)
        if not c:
            continue
        count = size_order(c, stake, p.fee_rate)
        if count <= 0:
            continue
        won = market["result"] == c.side
        fee = taker_fee(c.price, count, p.fee_rate)
        return {
            "ticker": c.ticker,
            "series": series_of(market["event_ticker"]),
            "title": market.get("title", ""),
            "side": c.side,
            "price": c.price,
            "count": count,
            "cost": round(c.price * count, 4),
            "fee": fee,
            "seconds_to_close": round(c.seconds_to_close),
            "result": market["result"],
            "won": won,
            "pnl": net_profit_if_win(c.price, count, p.fee_rate) if won else -round(c.price * count + fee, 4),
        }
    return None


def run_backtest(client: KalshiClient, hours: float, p: StrategyParams, stake: float = 10.0,
                 max_markets: int = 3000, series: str | None = None, min_volume: float = 1.0,
                 csv_path: str | None = None, pause: float = 0.06) -> str:
    end = int(time.time())
    start = end - int(hours * 3600)
    markets = []
    for m in client.iter_markets(status="settled", min_settled_ts=start, max_settled_ts=end,
                                 mve_filter="exclude", series_ticker=series):
        if (m.get("result") in ("yes", "no") and m.get("market_type", "binary") == "binary"
                and to_float(m.get("volume_fp")) >= min_volume):
            markets.append(m)
        if len(markets) >= max_markets:
            break
    log.info("backtesting %d settled markets from the last %.1fh", len(markets), hours)

    trades, skipped = [], 0
    for i, m in enumerate(markets, 1):
        close_ts = int(parse_time(m["close_time"]).timestamp())
        try:
            candles = client.get_candlesticks(series_of(m["event_ticker"]), m["ticker"],
                                              close_ts - p.max_seconds_to_close - 60, close_ts)
        except KalshiError as e:
            skipped += 1
            log.debug("no candles for %s: %s", m["ticker"], e)
            continue
        if (t := simulate_entry(m, candles, p, stake)):
            trades.append(t)
        if i % 200 == 0:
            log.info("  %d/%d markets, %d entries so far", i, len(markets), len(trades))
        time.sleep(pause)

    if csv_path and trades:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0]))
            w.writeheader()
            w.writerows(trades)

    by_series: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_series[t["series"]].append(t)
    series_lines = [
        f"  {s:<22}{len(g):>5} trades  win {100 * sum(t['won'] for t in g) / len(g):5.1f}%"
        f"  pnl {sum(t['pnl'] for t in g):+8.2f}"
        for s, g in sorted(by_series.items(), key=lambda kv: -len(kv[1]))[:15]
    ]
    losses = [t for t in trades if not t["won"]]
    loss_lines = [f"  {t['ticker']} {t['side']} @ {t['price']:.2f}, {t['seconds_to_close']}s before close"
                  for t in losses[:15]]
    return "\n".join([
        f"Backtest: last {hours:g}h, {len(markets)} settled markets scanned "
        f"({skipped} without candles), {len(trades)} entries, ${stake:g} per trade",
        f"Params: price {p.min_price:.2f}-{p.max_price:.2f}, "
        f"{p.min_seconds_to_close}-{p.max_seconds_to_close}s to close, max spread {p.max_spread:.2f}",
        "",
        summarize(trades),
        "",
        "Top series:", *series_lines,
        "",
        f"Losing entries ({len(losses)}):", *loss_lines,
    ])

