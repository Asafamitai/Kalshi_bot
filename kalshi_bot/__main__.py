"""Command line: `scan` once and print, or `run` the bot with Telegram alerts."""

from __future__ import annotations

import argparse
import logging
from decimal import Decimal

from .app import App
from .config import Config, load_dotenv
from .format import console_line
from .ladder import build_ladders, find_violations
from .store import Store
from .telegram import Telegram


def scan(cfg: Config, min_saving: Decimal) -> None:
    markets = list(cfg.kalshi_client().iter_open_markets())
    ladders = build_ladders(markets)
    found = find_violations(markets, min_saving)
    Store(cfg.db_path).log_findings(found)
    print(
        f"{len(markets)} open markets, {len(ladders)} ladders "
        f"({sum(len(l) for l in ladders)} rungs), {len(found)} violations >= {min_saving * 100:g}¢"
    )
    for v in found:
        print(console_line(v))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m kalshi_bot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan", help="scan every open market once and print what contradicts")
    scan_p.add_argument("--min-saving", type=Decimal, help="dollars, e.g. 0.01 (default: MIN_SAVING)")
    sub.add_parser("run", help="scan every SCAN_INTERVAL_SECONDS and alert on Telegram")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    cfg = Config.from_env()

    if args.command == "scan":
        scan(cfg, args.min_saving if args.min_saving is not None else cfg.min_saving)
        return

    if cfg.live_trading and not (cfg.kalshi_key_id and cfg.kalshi_private_key):
        parser.error("LIVE_TRADING is on but KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY are not set")
    telegram = None
    if cfg.telegram_token and cfg.telegram_chat_id:
        telegram = Telegram(cfg.telegram_token)
    logging.getLogger("kalshi_bot").info(
        "starting: every %ss, min saving %s, budget $%s, %s",
        cfg.scan_interval,
        cfg.min_saving,
        cfg.budget,
        "LIVE trading" if cfg.live_trading else "dry run",
    )
    App(cfg, Store(cfg.db_path), cfg.kalshi_client(), cfg.kalshi_client(), telegram).run()


if __name__ == "__main__":
    main()
