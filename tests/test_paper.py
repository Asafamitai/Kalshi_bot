import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from test_trader_app import Clock, FakeKalshi, FakeTelegram, alerts_sent, make_app, say

from kalshi_bot import episodes, paper
from kalshi_bot.app import App
from kalshi_bot.config import Config
from kalshi_bot.ladder import Rung, find_violations
from kalshi_bot.store import Store

D = Decimal


@pytest.fixture
def ladder(mk):
    # NO on "above 195" at 0.86 while NO on "above 175" bids 0.96: the safer ask is
    # below the dominated bid, so NO 195 + YES 175 (at 0.04) is a locked pair.
    return [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16")]


def book(app, strategy):
    return [t for t in app.store.paper_trades() if t["strategy"] == strategy]


def legs(t):
    return sorted((leg["ticker"], leg["side"], leg["count"], leg["price"]) for leg in t["legs"])


def pnl(t):
    return sum(leg["payout"] for leg in t["legs"]) - sum(leg["cost"] for leg in t["legs"])


def resolve(client, **results):
    """The markets stop trading and resolve: ticker=result."""
    client.scan_markets = {}
    for ticker, result in results.items():
        client.markets[ticker.replace("_", "-")].update(status="finalized", result=result)


# Opening


def test_strict_buys_what_single_buys_on_a_tight_market(ladder, tmp_path):
    app, _, _ = make_app(ladder, tmp_path)
    app.scan_once()
    (single,), (strict,) = book(app, "single"), book(app, "strict")
    assert legs(strict) == legs(single) == [("APP-T195", "no", 17, D("0.86"))]


def test_strict_skips_a_wide_book_on_the_contract_bought(mk, tmp_path):
    # NO 195 asks 0.90 but bids 0.84: still 0.07 under NO 175's mid, but six cents wide.
    app, _, _ = make_app([mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.10", "0.16")], tmp_path)
    app.scan_once()
    assert len(book(app, "single")) == 1 and book(app, "strict") == []


def test_strict_keeps_to_the_alert_price_range(mk):
    # NO 195 at 0.21: inside single's 0.10-0.95, below the alerts' 0.30.
    cfg = Config()
    (v,) = [v for v in find_violations([mk("APP-T175", 175, "0.70", "0.72"), mk("APP-T195", 195, "0.79", "0.80")],
                                       cfg.min_saving, cfg.max_spread) if v.side == "no"]
    assert v.ask == D("0.21") and paper.single_filters(v, cfg) and not paper.strict_filters(v, cfg)


def test_single_stops_opening_by_default_and_sees_out_what_it_holds(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    assert len(book(app, "single")) == 1
    app.cfg = Config(telegram_chat_id="42", telegram_allowed_user_ids=frozenset({"42"}))
    assert app.cfg.paper_books == ("strict", "pair")
    resolve(client, APP_T175="no", APP_T195="no")
    app.scan_once()
    (single,) = book(app, "single")
    assert single["status"] == "closed" and single["exit_reason"] == "settled"
    client.markets = {m["ticker"]: m for m in (mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16"))}
    client.scan_markets = None
    app.scan_once()  # the same contradiction again: single no longer buys it
    assert len(book(app, "single")) == 1 and len(book(app, "strict")) == 2


def test_paper_books_config():
    assert Config.from_env({}).paper_books == ("strict", "pair")
    assert Config.from_env({"PAPER_BOOKS": "Single, pair"}).paper_books == ("single", "pair")


def test_single_buys_fifteen_dollars_of_the_safer_contract(ladder, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    (t,) = book(app, "single")
    assert legs(t) == [("APP-T195", "no", 17, D("0.86"))]
    assert t["legs"][0]["cost"] == D("14.77")  # 17 * 0.86 + 0.15 fee; 18 would cost 15.63
    assert client.orders == []  # paper only


def test_pair_buys_both_legs_and_one_trade_per_event(ladder, tmp_path):
    app, _, _ = make_app(ladder, tmp_path)
    app.scan_once()
    # YES 175 + NO 195 is also how the YES side of the same inversion pairs up: one trade.
    (t,) = book(app, "pair")
    assert legs(t) == [("APP-T175", "yes", 16, D("0.04")), ("APP-T195", "no", 16, D("0.86"))]
    assert sum(leg["cost"] for leg in t["legs"]) == D("14.59")  # 13.90 + 0.69: locked at 16 - 14.59
    app.scan_once()
    assert len(book(app, "pair")) == 1 and len(book(app, "single")) == 1


def test_single_trades_what_is_too_thin_to_alert(mk, tmp_path):
    # 20 contracts at the ask and nothing traded today: no alert, but a paper trade.
    thin = [mk("APP-T175", 175, "0.02", "0.04", volume_24h="0.00"),
            mk("APP-T195", 195, "0.14", "0.16", yes_bid_size="20.00", volume_24h="0.00")]
    app, _, tg = make_app(thin, tmp_path)
    app.scan_once()
    assert alerts_sent(tg) == []
    (t,) = book(app, "single")
    assert legs(t) == [("APP-T195", "no", 17, D("0.86"))] and not t["alertable"]


def test_alert_grade_trades_are_marked(ladder, tmp_path):
    app, _, _ = make_app(ladder, tmp_path)
    app.scan_once()
    assert book(app, "single")[0]["alertable"] == 1


@pytest.mark.parametrize("dominated, safer, size, bought", [
    (("0.02", "0.04"), ("0.14", "0.16"), "10.00", True),   # NO 0.86 against 0.96 / 0.98, 10 deep: the floor
    (("0.02", "0.04"), ("0.14", "0.16"), "9.00", False),   # 9 deep
    (("0.80", "0.82"), ("0.88", "0.90"), "100.00", True),  # NO 0.12 against 0.18 / 0.20
    (("0.80", "0.82"), ("0.91", "0.93"), "100.00", False), # NO 0.09: under the price floor
    (("0.01", "0.02"), ("0.04", "0.06"), "100.00", False), # NO 0.96 against 0.98 / 0.99: over the cap
])
def test_single_filters(mk, dominated, safer, size, bought):
    markets = [mk("APP-T175", 175, *dominated),
               mk("APP-T195", 195, *safer, yes_bid_size=size, volume_24h="0.00")]
    (v,) = [v for v in find_violations(markets, D("0.01"), D("0.10")) if v.side == "no"]
    assert paper.single_filters(v, Config()) is bought


def test_no_pair_when_the_safer_ask_is_only_below_the_mid(mk, tmp_path):
    # NO 110 at 0.88 against NO 100 at 0.86 / 0.94: 2c under the mid, not under the bid.
    markets = [mk("A-T100", 100, "0.06", "0.14"), mk("A-T110", 110, "0.12", "0.14")]
    app, _, _ = make_app(markets, tmp_path)
    app.scan_once()
    assert len(book(app, "single")) == 1 and book(app, "pair") == []


def test_only_with_cash_left(mk, tmp_path):
    markets = []
    for ev in ("EV1", "EV2"):
        markets += [mk(f"{ev}-T175", 175, "0.02", "0.04", event=ev), mk(f"{ev}-T195", 195, "0.14", "0.16", event=ev)]
    app, _, _ = make_app(markets, tmp_path, paper_bankroll=D("20"))
    app.scan_once()
    assert len(book(app, "single")) == 1 and len(book(app, "pair")) == 1
    assert paper.cash(app.store.paper_trades(), "single", D("20")) == D("5.23")


def test_flicker_is_not_bought(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    client.scan_markets = dict(client.markets)
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")  # gone on the second read
    app.scan_once()
    assert app.store.paper_trades() == []


def test_paper_trading_can_be_switched_off(ladder, tmp_path):
    app, _, _ = make_app(ladder, tmp_path, paper_trading=False)
    app.scan_once()
    assert app.store.paper_trades() == []


# Closing


def test_single_takes_profit_at_80_percent_of_the_maximum(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    # Bought at 0.86, so the maximum profit is 0.14 and the target bid 0.972.
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")  # NO bid 0.97
    app.scan_once()
    assert book(app, "single")[0]["status"] == "open"
    assert book(app, "single")[0]["legs"][0]["mark"] == D("0.97")
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02")  # NO bid 0.98
    app.scan_once()
    (t,) = book(app, "single")
    assert (t["status"], t["exit_reason"]) == ("closed", "sold")
    assert t["legs"][0]["payout"] == D("16.63")  # 17 * 0.98 - 0.03 fee
    assert book(app, "pair")[0]["status"] == "open"  # the pair is held to settlement


def test_no_take_profit_without_the_depth_to_sell(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02", yes_ask_size="10.00")  # 10 NO bid at 0.98
    app.scan_once()
    assert book(app, "single")[0]["status"] == "open"


@pytest.mark.parametrize("r175, r195, single, pair", [
    ("no", "no", D("2.23"), D("1.41")),      # below 175: NO 195 wins, YES 175 loses
    ("yes", "no", D("2.23"), D("17.41")),    # between: both legs of the pair win
    ("yes", "yes", D("-14.77"), D("1.41")),  # above 195: the single contract loses, the pair doesn't
])
def test_settlement(ladder, tmp_path, r175, r195, single, pair):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    resolve(client, APP_T175=r175, APP_T195=r195)
    app.scan_once()
    (s,), (p,) = book(app, "single"), book(app, "pair")
    assert (s["status"], s["exit_reason"], pnl(s)) == ("closed", "settled", single)
    assert (p["status"], p["exit_reason"], pnl(p)) == ("closed", "settled", pair)


def test_closed_but_unresolved_waits(ladder, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    resolve(client, APP_T175="", APP_T195="")
    for t in ("APP-T175", "APP-T195"):
        client.markets[t]["status"] = "closed"
    app.scan_once()
    assert all(t["status"] == "open" for t in app.store.paper_trades())


def test_settlement_value_when_there_is_no_yes_or_no(ladder, tmp_path):
    assert paper.settlement({"status": "finalized", "result": "", "settlement_value_dollars": "0.4"}, "no") == D("0.6")
    assert paper.settlement({"status": "determined", "result": ""}, "yes") is None


# Report


def test_report(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    app.scan_once()
    resolve(client, APP_T175="yes", APP_T195="yes")
    app.scan_once()
    say(app, "/paper")
    text = tg.sent[-1][0]
    assert "single: 1 trades, 0 open, cash $485.23" in text
    assert "closed 1: won 0, lost 1, P&amp;L -$14.77 on $14.77 (-100.0%); sold 0, settled 1" in text
    assert "of which pass the alert filters: 1, P&amp;L -$14.77" in text
    assert "of which bid-through (a pair was locked): 1, P&amp;L -$14.77" in text
    assert "single buys at 0.10-0.95, depth &gt;= 10, 24h volume &gt;= 0" in text
    assert "pair: 1 trades, 0 open, cash $501.41" in text
    assert "16 NO APP-T195 @0.86 + 16 YES APP-T175 @0.04  $14.59  settled +$1.41" in text


def test_report_while_open(ladder, tmp_path):
    app, _, _ = make_app(ladder, tmp_path)
    app.scan_once()
    app.scan_once()  # marks at the bid: NO 195 bids 0.84
    text = paper.report(app.store, app.cfg)
    assert "open: cost $14.77, at the bid $14.28 (-$0.49)" in text
    assert "open: cost $14.59, locked at least +$1.41" in text


def test_config():
    cfg = Config.from_env({"PAPER_TRADING": "off", "PAPER_MAX_TRADE": "25", "PAPER_BANKROLL": "1000",
                           "PAPER_MIN_PRICE": "0.2", "PAPER_MAX_PRICE": "0.9", "PAPER_MIN_DEPTH": "5",
                           "PAPER_MIN_VOLUME_24H": "7"})
    assert not cfg.paper_trading and cfg.paper_max_trade == D("25") and cfg.paper_bankroll == D("1000")
    assert (cfg.paper_min_price, cfg.paper_max_price, cfg.paper_min_depth, cfg.paper_min_volume_24h) == (
        D("0.2"), D("0.9"), D("5"), D("7"))
    d = Config.from_env({})
    assert d.paper_trading and (d.paper_min_price, d.paper_max_price, d.paper_min_depth) == (
        D("0.10"), D("0.95"), D("10"))


def test_paper_trades_from_before_the_alertable_column(ladder, tmp_path):
    conn = sqlite3.connect(tmp_path / "t.sqlite3")
    conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, "
                 "opened_at REAL NOT NULL, event TEXT NOT NULL, side TEXT NOT NULL, safer TEXT NOT NULL, "
                 "dominated TEXT NOT NULL, saving TEXT NOT NULL, bid_through INTEGER NOT NULL, "
                 "status TEXT NOT NULL DEFAULT 'open', closed_at REAL, exit_reason TEXT)")
    conn.execute("INSERT INTO paper_trades (strategy, opened_at, event, side, safer, dominated, saving, bid_through) "
                 "VALUES ('single', 1, 'OLD', 'no', 'OLD-T2', 'OLD-T1', '0.02', 0)")
    conn.commit()
    conn.close()
    store = Store(str(tmp_path / "t.sqlite3"))
    assert store.paper_trades()[0]["alertable"] == 0
    app, _, _ = make_app(ladder, tmp_path)
    app.scan_once()
    assert [t["alertable"] for t in book(app, "single")] == [0, 1]


# Which rung moved


def closed_by(app):
    return {e["key"]: e["closed_by"] for e in app.store.closed_episodes()}


def test_closed_by_the_safer_ask_rising(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")  # NO 195 ask 0.86 -> 0.98
    app.scan_once()
    # Seen from the YES side, the same move is the dominated rung (YES 195) falling.
    assert closed_by(app) == {"APP-T195|no": "safer", "APP-T175|yes": "dominated"}
    report = episodes.report(app.store, now=app.clock.now)
    assert "closed by: safer ask rose 100%, dominated mid fell 0%, both 0% (n=1, mean rise 12¢, mean fall 0¢" in report


def test_closed_by_the_dominated_mid_falling(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    client.markets["APP-T175"] = mk("APP-T175", 175, "0.13", "0.15")  # NO 175 mid 0.97 -> 0.86
    app.scan_once()
    assert closed_by(app) == {"APP-T195|no": "dominated", "APP-T175|yes": "safer"}
    (e,) = [e for e in app.store.closed_episodes() if e["key"] == "APP-T195|no"]
    prices = [D(e[k]) for k in ("ref_safer_ask", "ref_dominated_mid", "close_safer_ask", "close_dominated_mid")]
    assert prices == [D("0.86"), D("0.97"), D("0.86"), D("0.86")]


def test_closed_by_markets_that_stopped_trading(ladder, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    client.scan_markets = {}
    app.scan_once()
    assert set(closed_by(app).values()) == {"expired"}
    assert "expired 1, book emptied or widened 0" in episodes.report(app.store, now=app.clock.now)


@pytest.mark.parametrize("yes_bid, yes_ask", [
    ("0.30", "0.45"),  # NO 100 widens to 0.55 / 0.70: its mid rose, nothing fell, but it is too wide to count
    ("0.00", "0.40"),  # NO 100 empties: no NO ask at all
])
def test_closed_by_a_book_not_a_price(mk, tmp_path, yes_bid, yes_ask):
    # NO 110 at 0.55 against NO 100 at 0.60 / 0.62.
    app, client, _ = make_app([mk("A-T100", 100, "0.38", "0.40"), mk("A-T110", 110, "0.45", "0.47")], tmp_path)
    app.scan_once()
    client.markets["A-T100"] = mk("A-T100", 100, yes_bid, yes_ask)
    app.scan_once()
    assert closed_by(app)["A-T110|no"] == "book"


def test_single_skips_a_contract_with_no_bid_behind_its_ask(mk, tmp_path):
    markets = [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "1.00")]
    app, _, _ = make_app(markets, tmp_path)
    app.scan_once()
    assert book(app, "single") == [] and book(app, "pair") == []


def test_an_older_database_gains_the_new_columns(ladder, mk, tmp_path):
    conn = sqlite3.connect(tmp_path / "t.sqlite3")
    cols = ("key TEXT, side TEXT, safer TEXT, dominated TEXT, opened_at REAL, last_seen REAL, "
            "first_saving TEXT, max_saving TEXT, max_net_saving TEXT, alertable INTEGER")
    conn.executescript(f"CREATE TABLE episodes_open ({cols}); CREATE TABLE episodes_closed ({cols}, closed_at REAL);"
                       "INSERT INTO episodes_open VALUES ('APP-T195|no', 'no', 'APP-T195', 'APP-T175', 1, 1, "
                       "'0.11', '0.11', '0.10', 1);")
    conn.commit()
    conn.close()
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()  # still open: its reference prices are taken now
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")
    app.scan_once()
    assert closed_by(app)["APP-T195|no"] == "safer"


# Paper trading's own Telegram bot


def make_paper_app(markets, tmp_path, **cfg):
    cfg.setdefault("paper_books", paper.STRATEGIES)
    config = Config(telegram_chat_id="42", telegram_allowed_user_ids=frozenset({"42"}),
                    paper_telegram_token="tok", paper_telegram_chat_id="77",
                    paper_telegram_allowed_user_ids=frozenset({"77"}), **cfg)
    client = FakeKalshi(markets)
    tg, paper_tg = FakeTelegram(), FakeTelegram()
    app = App(config, Store(str(tmp_path / "t.sqlite3")), client, client, tg, clock=Clock(), paper_telegram=paper_tg)
    return app, client, tg, paper_tg


def test_paper_bot_hears_what_each_pass_bought(ladder, tmp_path):
    app, _, tg, paper_tg = make_paper_app(ladder, tmp_path)
    app.scan_once()
    (text, buttons), = paper_tg.sent
    assert buttons is None and text.startswith("📝 <b>מסחר על נייר</b>\n<b>נקנו</b>")
    link = '<a href="https://kalshi.com/markets/kxapp?event_ticker=KXAPP-26OCT">'
    assert f"• בודדת · 17 NO {link}APP-T195</a> ב-0.86 · $14.77 · חיסכון 11.0¢ · עובר את סינון ההתראות" in text
    assert "$14.59 · חיסכון 11.0¢ · נעול לפחות +$1.41" in text
    assert text.endswith("קופה בודדת $485.23 · קופה מחמירה $485.23 · קופה זוג $485.41")
    assert len(alerts_sent(tg)) == 1 and not any("מסחר על נייר" in t for t, _ in tg.sent)
    app.scan_once()
    assert len(paper_tg.sent) == 1  # nothing new, nothing sent


def test_paper_bot_hears_what_closed(ladder, mk, tmp_path):
    app, client, _, paper_tg = make_paper_app(ladder, tmp_path)
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02")  # NO bid 0.98: take profit
    app.scan_once()
    assert "<b>נסגרו</b>\n• ✅ בודדת · APP-T195 · נמכר ביעד הרווח · <b>+$1.86</b>" in paper_tg.sent[-1][0]
    resolve(client, APP_T175="yes", APP_T195="yes")
    app.scan_once()
    text = paper_tg.sent[-1][0]
    assert "• ✅ זוג · " in text and " · הוכרע · <b>+$1.41</b>" in text and "נקנו" not in text


def test_paper_bot_losing_trade(ladder, tmp_path):
    app, client, _, paper_tg = make_paper_app(ladder, tmp_path)
    app.scan_once()
    resolve(client, APP_T175="yes", APP_T195="yes")
    app.scan_once()
    assert "• ❌ בודדת · APP-T195 · הוכרע · <b>-$14.77</b>" in paper_tg.sent[-1][0]


def test_paper_bot_commands(ladder, tmp_path):
    app, _, tg, paper_tg = make_paper_app(ladder, tmp_path)
    app.scan_once()
    app.handle_paper_message({"from": {"id": 77}, "text": "/paper"})
    assert paper_tg.sent[-1][0].startswith("<pre>Paper trading:") and "single: 1 trades" in paper_tg.sent[-1][0]
    app.handle_paper_message({"from": {"id": 77}, "text": "hi"})
    assert "בוט המסחר האוטומטי" in paper_tg.sent[-1][0]
    sent = len(paper_tg.sent)
    app.handle_paper_message({"from": {"id": 42}, "text": "/paper"})  # the alert bot's user isn't this bot's
    assert len(paper_tg.sent) == sent


def test_a_failed_paper_message_does_not_stop_the_scan(ladder, tmp_path):
    app, _, tg, paper_tg = make_paper_app(ladder, tmp_path)
    paper_tg.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down"))
    app.scan_once()
    assert len(app.store.paper_trades()) == 3 and len(alerts_sent(tg)) == 1


def test_long_passes_are_split_under_telegrams_limit(mk, tmp_path):
    markets = []
    for i in range(40):
        ev = f"KXLONGEVENTNAME-26OCT-{i:02d}"
        markets += [mk(f"{ev}-T175", 175, "0.02", "0.04", event=ev), mk(f"{ev}-T195", 195, "0.14", "0.16", event=ev)]
    app, _, _, paper_tg = make_paper_app(markets, tmp_path, paper_bankroll=D("10000"))
    app.scan_once()
    texts = [t for t, _ in paper_tg.sent]
    assert len(texts) > 1 and all(len(t) <= 4096 for t in texts)
    assert sum(t.count("\n• ") for t in texts) + sum(t.startswith("• ") for t in texts) == 120


def test_paper_bot_config():
    cfg = Config.from_env({"KALSHI_LADDER_AUTO_BOT_TOKEN": "SECRET", "KALSHI_LADDER_AUTO_BOT_CHAT_ID": "77",
                           "KALSHI_LADDER_BOT_CHAT_ID": "42"})
    assert cfg.paper_telegram and cfg.paper_telegram_chat_id == "77"
    assert cfg.paper_telegram_allowed_user_ids == frozenset({"77"}) and cfg.telegram_allowed_user_ids == frozenset({"42"})
    assert "SECRET" not in repr(cfg)
    assert not Config.from_env({"KALSHI_LADDER_AUTO_BOT_TOKEN": "t"}).paper_telegram  # no chat to send to
    both = Config.from_env({"KALSHI_LADDER_AUTO_BOT_CHAT_ID": "77", "KALSHI_LADDER_AUTO_BOT_ALLOWED_USER_IDS": "1, 2"})
    assert both.paper_telegram_allowed_user_ids == frozenset({"1", "2"})


# Markets that end within hours


NOW = 1_800_000_000  # the tests' clock


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ending_in(mk, hours):
    """The fixture ladder, in a market that stops trading `hours` after the tests' clock."""
    end = iso(NOW + hours * 3600)
    return [mk("APP-T175", 175, "0.02", "0.04", close_time=end), mk("APP-T195", 195, "0.14", "0.16", close_time=end)]


def test_nothing_but_pairs_in_a_market_that_ends_within_a_day(mk, tmp_path):
    app, _, tg = make_app(ending_in(mk, 3), tmp_path)
    app.scan_once()
    assert alerts_sent(tg) == []
    assert book(app, "single") == [] and book(app, "strict") == []
    assert len(book(app, "pair")) == 1  # a locked pair pays whatever happens


def test_a_day_left_is_enough(mk, tmp_path):
    app, _, tg = make_app(ending_in(mk, 25), tmp_path)
    app.scan_once()
    assert len(alerts_sent(tg)) == 1 and len(book(app, "strict")) == 1 and len(book(app, "single")) == 1


def test_the_expected_resolution_counts_when_it_comes_first(ladder, tmp_path):
    for m in ladder:
        m["expected_expiration_time"] = iso(NOW + 2 * 3600)  # a game tonight in a market open for years
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    assert alerts_sent(tg) == [] and book(app, "strict") == []


def test_min_hours_left_zero_switches_it_off(mk, tmp_path):
    # Even past its expected resolution, as a market still open while it waits for a result.
    app, _, tg = make_app(ending_in(mk, -1), tmp_path, min_hours_left=0)
    app.scan_once()
    assert len(alerts_sent(tg)) == 1


def test_end_time_is_whichever_comes_first(mk):
    m = mk("A-T1", 1, "0.40", "0.42", close_time=iso(NOW + 7200))
    assert Rung.from_market(m).ends_at == NOW + 7200
    m["expected_expiration_time"] = iso(NOW + 3600)
    assert Rung.from_market(m).hours_left(NOW) == 1
    del m["close_time"], m["expected_expiration_time"]
    assert Rung.from_market(m).ends_at is None  # unknown: not held against it
    assert Config.from_env({"MIN_HOURS_LEFT": "6"}).min_hours_left == 6 and Config().min_hours_left == 24


# The price after buying


def test_price_after_buying(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    (t,) = book(app, "strict")
    assert t["legs"][0]["entry_mid"] == D("0.85")  # NO 195 at 0.84 / 0.86
    assert [leg["entry_mid"] for leg in book(app, "pair")[0]["legs"]] == [D("0.85"), D("0.03")]
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.10", "0.12")  # NO now 0.88 / 0.90
    app.clock.now += 10 * 60
    app.scan_once()
    assert book(app, "strict")[0]["legs"][0]["mid_15m"] is None  # too early
    app.clock.now += 6 * 60
    app.scan_once()
    leg = book(app, "strict")[0]["legs"][0]
    assert leg["mid_15m"] == D("0.89") and leg["mid_1h"] is None
    text = paper.report(app.store, app.cfg)
    assert "price after buying (mid move per contract, + is good): 15m +4.0¢ (n=1)" in text
    app.clock.now += 50 * 60  # 66 minutes in
    app.scan_once()
    assert book(app, "strict")[0]["legs"][0]["mid_1h"] == D("0.89")
    assert "15m +4.0¢ (n=1) · 1h +4.0¢ (n=1)" in paper.report(app.store, app.cfg)


def test_a_late_pass_leaves_a_horizon_blank(ladder, tmp_path):
    app, _, _ = make_app(ladder, tmp_path)
    app.scan_once()
    app.clock.now += 40 * 60  # past twice 15 minutes: no longer a 15-minute price
    app.scan_once()
    leg = book(app, "strict")[0]["legs"][0]
    assert leg["mid_15m"] is None and leg["mid_1h"] is None


def test_paper_legs_from_before_the_price_columns(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.sqlite3")
    conn.execute("CREATE TABLE paper_legs (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER NOT NULL, "
                 "ticker TEXT NOT NULL, side TEXT NOT NULL, count INTEGER NOT NULL, price TEXT NOT NULL, "
                 "cost TEXT NOT NULL, mark TEXT, status TEXT NOT NULL DEFAULT 'open', exit_price TEXT, "
                 "payout TEXT, closed_at REAL)")
    conn.commit()
    conn.close()
    Store(str(tmp_path / "t.sqlite3"))
    have = {r[1] for r in sqlite3.connect(tmp_path / "t.sqlite3").execute("PRAGMA table_info(paper_legs)")}
    assert {"entry_mid", "mid_15m", "mid_1h", "mid_4h"} <= have
