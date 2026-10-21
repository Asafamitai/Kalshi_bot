"""Command line: `scan` once and print, `run` the bot, `report` on how long contradictions last,
`paper` on what paper trading would have made, `live` on automatic trading with real money,
`zoom` for a closer look at a position: its ladder, its reference rung, and the series' history."""

from __future__ import annotations

import argparse
import fcntl
import logging

from . import alerts, episodes, live, paper, subaccounts, zoom
from .app import App
from .config import Config, load_dotenv
from .format import console_line
from .ladder import build_ladders, find_violations
from .store import Store
from .telegram import Telegram


def scan(cfg: Config) -> None:
    markets = list(cfg.kalshi_client().iter_open_markets())
    ladders = build_ladders(markets)
    found = find_violations(markets, cfg.min_saving, cfg.max_spread)
    passing = [v for v in found if alerts.alertable(v, cfg)]
    print(
        f"{len(markets)} open markets, {len(ladders)} ladders ({sum(len(l) for l in ladders)} rungs): "
        f"{len(found)} violations >= {cfg.min_saving * 100:g}c against books <= {cfg.max_spread * 100:g}c wide, "
        f"{len(passing)} pass the alert filters"
    )
    print("\nPass the alert filters:")
    for v in sorted(passing, key=lambda v: v.net_saving, reverse=True):
        print(console_line(v))
    print("\nTracked only:")
    for v in found:
        if v not in passing:
            print(console_line(v))


def telegram_chats(token: str) -> None:
    """Who has written to the bot lately, to find its chat id. Stop `run` first:
    Telegram allows one reader of updates at a time."""
    for u in Telegram(token).updates(None, timeout=0):
        msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        if chat:
            print(f"chat_id={chat.get('id')}  type={chat.get('type')}  "
                  f"name={chat.get('title') or chat.get('first_name')}  from_user_id={sender.get('id')}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m kalshi_bot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="scan every open market once and print what contradicts")
    sub.add_parser("run", help="scan every SCAN_INTERVAL_SECONDS and alert on Telegram")
    rep = sub.add_parser("report", help="how long contradictions survived")
    rep.add_argument("--hours", type=float, default=24 * 7)
    pap = sub.add_parser("paper", help="the paper trading books: single contract vs locked pair")
    pap.add_argument("--latest", type=int, default=20, help="how many recent trades to list")
    liv = sub.add_parser("live", help="automatic trading with real money: positions, P&L, budget")
    liv.add_argument("--latest", type=int, default=20, help="how many recent positions to list")
    zm = sub.add_parser("zoom", help="a closer look at a market, or at every open automatic trading position")
    zm.add_argument("ticker", nargs="?", help="the market; without one, every open automatic trading position")
    zm.add_argument("--side", choices=["yes", "no"], default="yes")
    acct = sub.add_parser("subaccount", help=f"the Kalshi subaccount automatic trading uses ({subaccounts.NAME})")
    acct.add_argument("action", nargs="?", default="show", choices=["show", "create", "fund", "withdraw"],
                      help="show balances, create it, fund it from the primary account, or withdraw back")
    acct.add_argument("amount", nargs="?", help="dollars, for fund and withdraw")
    chats = sub.add_parser("telegram-chats", help="list chats that wrote to the bot, to find KALSHI_LADDER_BOT_CHAT_ID")
    chats.add_argument("--paper", action="store_true",
                       help="the automatic trading bot instead, to find KALSHI_LADDER_AUTO_BOT_CHAT_ID")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    cfg = Config.from_env()

    if args.command == "scan":
        scan(cfg)
    elif args.command == "report":
        print(episodes.report(Store(cfg.db_path), since_hours=args.hours))
    elif args.command == "paper":
        print(paper.report(Store(cfg.db_path), cfg, latest=args.latest))
    elif args.command == "live":
        print(live.report(Store(cfg.db_path), cfg, latest=args.latest))
    elif args.command == "zoom":
        client = cfg.kalshi_client()
        if args.ticker:
            try:
                zooms = [zoom.build(client, cfg, args.ticker.upper(), args.side)]
            except LookupError as e:
                parser.exit(1, f"{e}\n")
        else:
            held = [p for p in Store(cfg.db_path).live_positions() if p["status"] == "open"]
            if not held:
                print("no open automatic trading positions; `zoom TICKER` for a market")
            zooms = [zoom.for_position(client, cfg, p) for p in held]
        print("\n\n".join(zoom.plain(zoom.render(z, cfg)) for z in zooms))
    elif args.command == "subaccount":
        store, client = Store(cfg.db_path), cfg.kalshi_client()
        if args.action == "create":
            print(subaccounts.create(store, client, cfg))
        elif args.action in ("fund", "withdraw"):
            if not args.amount:
                parser.error(f"{args.action} needs an amount in dollars")
            print(subaccounts.transfer(store, client, cfg, args.amount, back=args.action == "withdraw"))
        else:
            print(subaccounts.show(store, client, cfg))
    elif args.command == "telegram-chats":
        name, token = ("KALSHI_LADDER_AUTO_BOT_TOKEN", cfg.paper_telegram_token) if args.paper \
            else ("KALSHI_LADDER_BOT_TOKEN", cfg.telegram_token)
        if not token:
            parser.error(f"{name} is not set")
        telegram_chats(token)
    else:
        # One bot per database: a second copy would send every alert twice.
        lock = open(cfg.db_path + ".lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            parser.exit(1, f"another bot is already running on {cfg.db_path}\n")
        telegram = None
        if cfg.telegram_token and cfg.telegram_chat_id:
            telegram = Telegram(cfg.telegram_token)
        log = logging.getLogger("kalshi_bot")
        paper_telegram = None
        if cfg.paper_telegram and cfg.paper_telegram_token == cfg.telegram_token:
            # Two readers of one bot's updates make Telegram refuse both.
            log.warning("KALSHI_LADDER_AUTO_BOT_TOKEN is the alert bot's token: automatic trading needs a bot of its own")
        elif cfg.paper_telegram:
            paper_telegram = Telegram(cfg.paper_telegram_token)
        store = Store(cfg.db_path)
        if store.auto_on and paper_telegram is None:
            log.warning("automatic trading is on but KALSHI_LADDER_AUTO_BOT is not configured: "
                        "its messages go to this log, and it can't be switched off from Telegram")
        account = subaccounts.number(store, cfg)
        log.info(
            "starting: every %ss, net saving >= %s, price %s-%s, depth >= %s, trading %s, auto %s (budget %s, "
            "%s a trade, subaccount %s), paper %s, telegram %s, auto telegram %s",
            cfg.scan_interval, cfg.min_net_saving, cfg.min_price, cfg.max_price, cfg.min_depth,
            "on" if store.trading_on else "off", "on" if store.auto_on else "off", cfg.live_budget,
            cfg.live_max_trade, account, "on" if cfg.paper_trading else "off", "on" if telegram else "off",
            "on" if paper_telegram else "off",
        )
        App(cfg, store, cfg.kalshi_client(), cfg.kalshi_client(), telegram, paper_telegram=paper_telegram).run()


if __name__ == "__main__":
    main()
