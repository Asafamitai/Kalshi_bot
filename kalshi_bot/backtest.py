"""Replay the strategy on recently settled markets using 1-minute candlesticks.

For each settled market, walk the minute candles in the final
`max_seconds_to_close` before close and take the first minute where the
strategy would have bought. Then score that entry against the real result.

Limits of this approximation:
- Quotes are minute-close snapshots; real fills could be worse.
- Book depth is unknown, so every entry is assumed to fill in full.
- Markets that closed *early* (e.g. a match ended) look like they were always
  going to close then, which the live bot could not have known. Scheduled
  closes land on a whole minute while early closes almost never do, so by
  default markets whose close time has non-zero seconds are skipped.
"""

from __future__ import annotations

import csv
import logging
import time
from concurrent.futures import ThreadPoolExecutor
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


def closed_on_schedule(market: dict) -> bool:
    return parse_time(market["close_time"]).second == 0


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
            "close_date": market["close_time"][:10],
            "result": market["result"],
            "won": won,
            "pnl": net_profit_if_win(c.price, count, p.fee_rate) if won else -round(c.price * count + fee, 4),
        }
    return None


def run_backtest(client: KalshiClient, hours: float, p: StrategyParams, stake: float = 10.0,
                 max_markets: int = 3000, series: str | None = None, min_volume: float = 1.0,
                 csv_path: str | None = None, workers: int = 8,
                 include_early_closes: bool = False) -> str:
    end = int(time.time())
    start = end - int(hours * 3600)
    markets = []
    for m in client.iter_markets(status="settled", min_settled_ts=start, max_settled_ts=end,
                                 mve_filter="exclude", series_ticker=series):
        if (m.get("result") in ("yes", "no") and m.get("market_type", "binary") == "binary"
                and to_float(m.get("volume_fp")) >= min_volume
                and (include_early_closes or closed_on_schedule(m))):
            markets.append(m)
        if len(markets) >= max_markets:
            break
    log.info("backtesting %d settled markets from the last %.1fh", len(markets), hours)

    # One request per event returns candles for all of its markets (crypto events have ~20-190 strikes).
    events: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        events[m["event_ticker"]].append(m)

    def fetch_market(m: dict) -> list[dict] | None:
        close_ts = int(parse_time(m["close_time"]).timestamp())
        try:
            return client.get_candlesticks(series_of(m["event_ticker"]), m["ticker"],
                                           close_ts - p.max_seconds_to_close - 60, close_ts)
        except KalshiError as e:
            log.debug("no candles for %s: %s", m["ticker"], e)
            return None

    def fetch_event(group: list[dict]) -> list[tuple[dict, list[dict] | None]]:
        closes = [int(parse_time(m["close_time"]).timestamp()) for m in group]
        start_ts, end_ts = min(closes) - p.max_seconds_to_close - 60, max(closes)
        try:
            data = client.get_event_candlesticks(series_of(group[0]["event_ticker"]),
                                                 group[0]["event_ticker"], start_ts, end_ts)
            by_ticker = dict(zip(data.get("market_tickers", []), data.get("market_candlesticks", [])))
            truncated = data.get("adjusted_end_ts", end_ts) < end_ts
        except KalshiError as e:
            log.debug("no event candles for %s: %s", group[0]["event_ticker"], e)
            by_ticker, truncated = {}, True
        return [(m, by_ticker[m["ticker"]] if m["ticker"] in by_ticker and not truncated else fetch_market(m))
                for m in group]

    trades, skipped, done = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, results in enumerate(pool.map(fetch_event, events.values()), 1):
            for m, candles in results:
                if candles is None:
                    skipped += 1
                elif (t := simulate_entry(m, candles, p, stake)):
                    trades.append(t)
            done += len(results)
            if i % 200 == 0:
                log.info("  %d/%d markets, %d entries so far", done, len(markets), len(trades))

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
    by_day: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_day[t["close_date"]].append(t)
    day_lines = [
        f"  {d}  {len(g):>5} trades  win {100 * sum(t['won'] for t in g) / len(g):5.1f}%"
        f"  pnl {sum(t['pnl'] for t in g):+8.2f}"
        for d, g in sorted(by_day.items())
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
        "By day (UTC close date):", *day_lines,
        "",
        "Top series:", *series_lines,
        "",
        f"Losing entries ({len(losses)}):", *loss_lines,
    ])

