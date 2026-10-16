"""CLI: python -m kalshi_bot {run,backtest,report} ..."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .backtest import run_backtest
from .bot import Bot, BotConfig, RiskLimits
from .client import DEMO_BASE, PROD_BASE, KalshiClient
from .ledger import Ledger, report
from .strategy import StrategyParams


def add_strategy_args(ap: argparse.ArgumentParser) -> None:
    d = StrategyParams()
    ap.add_argument("--min-price", type=float, default=d.min_price, help="min ask to buy (default 0.85)")
    ap.add_argument("--max-price", type=float, default=d.max_price, help="max ask to buy (default 0.97)")
    ap.add_argument("--max-seconds", type=int, default=d.max_seconds_to_close, help="enter within N seconds of close (default 300)")
    ap.add_argument("--min-seconds", type=int, default=d.min_seconds_to_close, help="skip if closing in under N seconds (default 5)")
    ap.add_argument("--max-spread", type=float, default=d.max_spread, help="max yes bid/ask spread (default 0.05)")
    ap.add_argument("--min-volume", type=float, default=d.min_volume, help="min lifetime volume of the market")
    ap.add_argument("--stake", type=float, default=10.0, help="max dollars per trade incl. fees (default 10)")


def strategy_from(args) -> StrategyParams:
    return StrategyParams(
        min_price=args.min_price, max_price=args.max_price,
        max_seconds_to_close=args.max_seconds, min_seconds_to_close=args.min_seconds,
        max_spread=args.max_spread, min_volume=args.min_volume,
    )


def make_client() -> KalshiClient:
    base = DEMO_BASE if os.environ.get("KALSHI_ENV", "prod").lower() == "demo" else PROD_BASE
    return KalshiClient(
        base_url=base,
        key_id=os.environ.get("KALSHI_KEY_ID"),
        private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH"),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kalshi_bot", description="Buy >=85c sides that close within 5 minutes.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="scan and trade in a loop (paper by default)")
    add_strategy_args(run)
    run.add_argument("--live", action="store_true", help="place REAL orders (needs API keys)")
    run.add_argument("--once", action="store_true", help="run a single scan and exit")
    run.add_argument("--poll", type=float, default=15.0, help="seconds between scans (default 15)")
    run.add_argument("--max-exposure", type=float, default=50.0, help="max dollars in open positions")
    run.add_argument("--max-daily-loss", type=float, default=25.0, help="stop for the day after this loss")
    run.add_argument("--ledger", default=None, help="trade ledger path")

    bt = sub.add_parser("backtest", help="replay the strategy on recently settled markets")
    add_strategy_args(bt)
    bt.add_argument("--hours", type=float, default=24, help="look back this many hours (default 24)")
    bt.add_argument("--max-markets", type=int, default=100_000)
    bt.add_argument("--series", default=None, help="only this series ticker, e.g. KXBTCD")
    bt.add_argument("--csv", default=None, help="write every simulated trade to this CSV")
    bt.add_argument("--include-early-closes", action="store_true",
                    help="also score markets that closed early (look-ahead bias: the live bot can't see these coming)")

    rp = sub.add_parser("report", help="summarize a trade ledger")
    rp.add_argument("--ledger", default="data/paper_trades.json")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.cmd == "report":
        print(report(Ledger(args.ledger)))
        return 0

    client = make_client()
    params = strategy_from(args)

    if args.cmd == "backtest":
        print(run_backtest(client, args.hours, params, stake=args.stake, max_markets=args.max_markets,
                           series=args.series, csv_path=args.csv,
                           include_early_closes=args.include_early_closes))
        return 0

    if args.live:
        print("LIVE MODE: real money orders will be placed. Ctrl+C within 5s to abort.", file=sys.stderr)
        import time
        time.sleep(5)
    cfg = BotConfig(
        live=args.live,
        poll_seconds=args.poll,
        ledger_path=args.ledger or ("data/live_trades.json" if args.live else "data/paper_trades.json"),
        strategy=params,
        risk=RiskLimits(max_cost_per_trade=args.stake, max_open_exposure=args.max_exposure,
                        max_daily_loss=args.max_daily_loss),
    )
    try:
        Bot(client, cfg).run(once=args.once)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
