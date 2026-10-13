from decimal import Decimal

import pytest
import requests
from test_trader_app import Clock, FakeKalshi, FakeTelegram, button, make_app, say, tap

from kalshi_bot import live
from kalshi_bot.app import App
from kalshi_bot.config import Config
from kalshi_bot.kalshi import KalshiError
from kalshi_bot.store import Store

D = Decimal


@pytest.fixture
def ladder(mk):
    # NO on "above 195" at 0.86 while NO on "above 175" sits at 0.96/0.98: what the strict book buys.
    return [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16")]


def positions(app):
    return app.store.live_positions()


def buys(client):
    return [o for o in client.orders if o[0] != "sell"]


def sells(client):
    return [o for o in client.orders if o[0] == "sell"]


def live_messages(tg):
    return [t for t, _ in tg.sent if "מסחר אוטומטי · כסף אמיתי" in t]


def auto_app(markets, tmp_path, **cfg):
    app, client, tg = make_app(markets, tmp_path, **cfg)
    say(app, "/auto on")
    return app, client, tg


# Switching it on


def test_off_by_default_sends_no_orders(ladder, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    assert not app.store.auto_on and client.orders == [] and positions(app) == []


def test_auto_on_needs_a_kalshi_key(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path, can_trade=False)
    say(app, "/auto on")
    assert not app.store.auto_on and "אין מפתח Kalshi" in tg.sent[-1][0]


def test_auto_on_and_off(ladder, tmp_path):
    app, _, tg = auto_app(ladder, tmp_path)
    assert app.store.auto_on and "עד $10.00 לעסקה ועד $100.00 בסך הכול" in tg.sent[-1][0]
    assert "בחשבון הראשי" in tg.sent[-1][0] and "יתרה בחשבון הראשי: $1,000.00" in tg.sent[-1][0]
    say(app, "/auto off")
    assert not app.store.auto_on


def test_strangers_cannot_switch_it_on(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    say(app, "/auto on", user="7")
    assert not app.store.auto_on and tg.sent == []


# Buying


def test_buys_what_the_strict_book_buys_for_real(ladder, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)
    app.scan_once()
    (order,) = buys(client)
    assert order[:4] == ("APP-T195", "no", 11, D("0.86")) and order[4].startswith("auto-")
    (p,) = positions(app)
    assert (p["ticker"], p["side"], p["bought"], p["count"], p["price"]) == ("APP-T195", "no", 11, 11, D("0.86"))
    assert p["cost"] == D("9.56")  # 11 * 0.86 + 0.10 fee: $10 a trade
    strict = [t for t in app.store.paper_trades() if t["strategy"] == "strict"]
    assert strict[0]["safer"] == p["ticker"] and strict[0]["side"] == p["side"]
    (text,) = live_messages(tg)
    assert "11 NO" in text and "$9.56" in text and "נשאר בתקציב $90.44 מתוך $100.00" in text


def test_one_position_per_event(ladder, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    app.scan_once()
    app.scan_once()
    assert len(buys(client)) == 1 and len(positions(app)) == 1


def test_skips_a_wide_book_like_the_strict_book(mk, tmp_path):
    app, client, _ = auto_app([mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.10", "0.16")], tmp_path)
    app.scan_once()
    assert client.orders == [] and positions(app) == []


def test_partial_fill_records_what_filled(ladder, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    client.fill = 4
    app.scan_once()
    (p,) = positions(app)
    assert (p["bought"], p["count"], p["cost"]) == (4, 4, D("3.48"))


def test_unfilled_order_opens_nothing(ladder, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    client.fill = 0
    app.scan_once()
    assert len(buys(client)) == 1 and positions(app) == []
    client.fill = None
    app.scan_once()  # tried again next pass
    assert len(positions(app)) == 1


def test_budget_caps_what_it_spends(ladder, mk, tmp_path):
    other = [mk("OTH-T175", 175, "0.02", "0.04", event="KXOTH-26OCT"),
             mk("OTH-T195", 195, "0.14", "0.16", event="KXOTH-26OCT")]
    app, client, _ = auto_app(ladder + other, tmp_path, live_budget=D("12"))
    app.scan_once()
    assert sorted(p["bought"] for p in positions(app)) == [2, 11]  # $9.56, then the $2.44 left buys 2
    assert live.budget_left(positions(app), D("12")) == D("0.70")
    app.scan_once()
    assert len(buys(client)) == 2


def test_no_buy_when_the_balance_is_short(ladder, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    client.cash = D("5")
    app.scan_once()
    assert buys(client)[0][2] == 5  # what $5 buys


def test_unreadable_balance_buys_nothing(ladder, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)
    client.balance = lambda subaccount=0: None
    app.scan_once()
    assert client.orders == [] and "לא הצלחתי לקרוא את היתרה" in live_messages(tg)[-1]


def test_rejected_order_is_reported_and_trading_stays_on(ladder, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)
    client.order_error = KalshiError(400, "insufficient_balance")
    app.scan_once()
    assert positions(app) == [] and app.store.auto_on
    assert "Kalshi דחתה את ההזמנה" in live_messages(tg)[-1]


def test_unknown_outcome_switches_it_off(ladder, mk, tmp_path):
    other = [mk("OTH-T175", 175, "0.02", "0.04", event="KXOTH-26OCT"),
             mk("OTH-T195", 195, "0.14", "0.16", event="KXOTH-26OCT")]
    app, client, tg = auto_app(ladder + other, tmp_path)
    client.order_error = requests.ConnectionError("timed out")
    app.scan_once()
    assert len(buys(client)) == 1  # stopped after the first, though a second event was wanted
    assert not app.store.auto_on and positions(app) == []
    text = live_messages(tg)[-1]
    assert "המסחר האוטומטי כובה" in text and "/auto on" in text


# Selling and settling


def test_takes_profit_with_a_real_sale(ladder, mk, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02")  # NO bid 0.98
    app.scan_once()
    (sale,) = sells(client)
    assert sale[1:5] == ("APP-T195", "no", 11, D("0.98"))
    (p,) = positions(app)
    assert (p["status"], p["exit_reason"], p["count"], p["proceeds"]) == ("closed", "sold", 0, D("10.76"))
    assert "+$1.20" in live_messages(tg)[-1]  # 10.76 - 9.56


def test_sells_no_more_than_is_held(ladder, mk, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    app.scan_once()
    client.held[("APP-T195", "no")] = D(5)
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02")
    app.scan_once()
    assert sells(client)[0][3] == 5
    (p,) = positions(app)
    assert p["status"] == "open" and p["count"] == 6


def test_position_gone_from_kalshi_is_closed_without_selling(ladder, mk, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)
    app.scan_once()
    client.held.clear()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02")
    app.scan_once()
    assert sells(client) == []
    (p,) = positions(app)
    assert (p["status"], p["exit_reason"]) == ("closed", "missing")
    assert "כבר לא מוחזקים" in live_messages(tg)[-1]


def test_no_sale_while_switched_off(ladder, mk, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    app.scan_once()
    say(app, "/auto off")
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02")
    app.scan_once()
    assert sells(client) == []
    (p,) = positions(app)
    assert p["status"] == "open" and p["mark"] == D("0.98")


def test_settles_while_switched_off(ladder, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    app.scan_once()
    say(app, "/auto off")
    client.scan_markets = {}
    client.markets["APP-T195"].update(status="finalized", result="no")
    app.scan_once()
    (p,) = positions(app)
    assert (p["status"], p["exit_reason"], p["proceeds"]) == ("closed", "settled", D("11"))
    assert live.budget_left(positions(app), D("100")) == D("101.44")


def test_a_losing_settlement_comes_off_the_budget(ladder, tmp_path):
    app, client, _ = auto_app(ladder, tmp_path)
    app.scan_once()
    client.scan_markets = {}
    client.markets["APP-T195"].update(status="finalized", result="yes")
    app.scan_once()
    assert live.budget_left(positions(app), D("100")) == D("90.44")


# Reports and settings


def test_live_command_and_status(ladder, tmp_path):
    app, _, tg = auto_app(ladder, tmp_path)
    app.scan_once()
    say(app, "/live")
    assert "Automatic trading (strict, real money): ON" in tg.sent[-1][0]
    assert "11 NO APP-T195 @0.86" in tg.sent[-1][0]
    say(app, "/status")
    assert "מסחר אוטומטי (מחמירה, כסף אמיתי): <b>פעיל</b>" in tg.sent[-1][0]


def test_a_failing_step_does_not_stop_the_scan(ladder, tmp_path, monkeypatch):
    app, _, tg = auto_app(ladder, tmp_path)
    monkeypatch.setattr(live, "step", lambda *a: 1 / 0)
    app.scan_once()
    assert app.store.last_scan() is not None and "נכשל" in live_messages(tg)[-1]


def test_without_telegram_it_logs(ladder, tmp_path, caplog):
    client = FakeKalshi(ladder)
    store = Store(str(tmp_path / "t.sqlite3"))
    store.set_setting("auto", "on")
    app = App(Config(), store, client, client, None, clock=Clock())
    with caplog.at_level("INFO", logger="kalshi_bot"):
        app.scan_once()
    assert any("LIVE" in r.message for r in caplog.records) and len(store.live_positions()) == 1


def test_live_config():
    d = Config.from_env({})
    assert (d.live_max_trade, d.live_budget, d.live_subaccount) == (D("10"), D("100"), 0)
    cfg = Config.from_env({"LIVE_MAX_TRADE": "25", "LIVE_BUDGET": "300", "LIVE_SUBACCOUNT": "3"})
    assert (cfg.live_max_trade, cfg.live_budget, cfg.live_subaccount) == (D("25"), D("300"), 3)
    with pytest.raises(ValueError):
        Config.from_env({"LIVE_SUBACCOUNT": "64"})


# Subaccount


def test_everything_happens_in_the_subaccount(ladder, mk, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path, live_subaccount=3)
    assert "בתת-חשבון 3" in tg.sent[-1][0] and "יתרה בתת-חשבון 3" in tg.sent[-1][0]
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.01", "0.02")
    app.scan_once()
    assert {what for what, _ in client.subaccounts} == {"balance", "buy", "position", "sell"}
    assert {n for _, n in client.subaccounts} == {3}
    say(app, "/live")
    assert "in subaccount 3" in tg.sent[-1][0]


def test_buy_buttons_stay_in_the_primary_account(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, live_subaccount=3)
    say(app, "/trading on")
    app.scan_once()
    tap(app, button(tg, "קנה 10"))
    assert ("buy", 0) in client.subaccounts and ("buy", 3) not in client.subaccounts


def test_auto_on_says_when_the_subaccount_balance_cannot_be_read(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, live_subaccount=3)

    def no_such_subaccount(subaccount=0):
        raise KalshiError(400, "subaccount not found")
    client.balance = no_such_subaccount
    say(app, "/auto on")
    assert "לא הצלחתי לקרוא את היתרה בתת-חשבון 3" in tg.sent[-1][0] and "subaccount not found" in tg.sent[-1][0]
