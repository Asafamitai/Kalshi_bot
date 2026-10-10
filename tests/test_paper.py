import sqlite3
from decimal import Decimal

import pytest
from test_trader_app import make_app, say

from kalshi_bot import episodes, paper
from kalshi_bot.config import Config

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
    assert "of which bid-through (a pair was locked): 1, P&amp;L -$14.77" in text
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
    cfg = Config.from_env({"PAPER_TRADING": "off", "PAPER_MAX_TRADE": "25", "PAPER_BANKROLL": "1000"})
    assert not cfg.paper_trading and cfg.paper_max_trade == D("25") and cfg.paper_bankroll == D("1000")
    assert Config.from_env({}).paper_trading


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
