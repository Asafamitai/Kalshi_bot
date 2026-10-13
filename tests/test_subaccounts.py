from decimal import Decimal

import pytest
import requests
from test_live import make_app, say

from kalshi_bot import __main__ as cli
from kalshi_bot import subaccounts
from kalshi_bot.kalshi import KalshiError

D = Decimal


@pytest.fixture
def ladder(mk):
    return [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16")]


def reply(tg):
    return tg.sent[-1][0]


def test_starts_in_the_primary_account(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    assert subaccounts.number(app.store, app.cfg) == 0
    say(app, "/subaccount")
    assert "עובד בחשבון הראשי" in reply(tg) and "/subaccount create" in reply(tg)


def test_create_names_it_and_trading_moves_there(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/subaccount create")
    assert "נפתח תת-חשבון KALSHI_LADDER_AUTO_BOT במספר 1" in reply(tg)
    assert subaccounts.number(app.store, app.cfg) == 1
    say(app, "/auto on")
    app.scan_once()
    assert ("buy", 1) in client.subaccounts


def test_create_only_once(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/subaccount create")
    say(app, "/subaccount create")
    assert client.created == 1 and "כבר עובד בתת-חשבון KALSHI_LADDER_AUTO_BOT (1)" in reply(tg)


def test_create_refused_by_kalshi(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)

    def refuse():
        raise KalshiError(403, "advanced API tier required")
    client.create_subaccount = refuse
    say(app, "/subaccount create")
    assert "Kalshi לא פתחה תת-חשבון" in reply(tg) and subaccounts.number(app.store, app.cfg) == 0


def test_the_setting_overrides_what_was_created(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path, live_subaccount=5)
    say(app, "/subaccount create")
    assert "כבר עובד בתת-חשבון KALSHI_LADDER_AUTO_BOT (5)" in reply(tg)


def test_fund_and_withdraw(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/subaccount create")
    say(app, "/subaccount fund 100")
    assert "הועברו $100.00 מהחשבון הראשי לתת-חשבון KALSHI_LADDER_AUTO_BOT (1)" in reply(tg)
    say(app, "/subaccount withdraw $25.50")
    assert "הועברו $25.50 מתת-חשבון KALSHI_LADDER_AUTO_BOT (1) לחשבון הראשי" in reply(tg)
    assert client.transfers == [(0, 1, D("100")), (1, 0, D("25.50"))]


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "1.005"])
def test_bad_amounts_move_nothing(ladder, tmp_path, amount):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/subaccount create")
    say(app, f"/subaccount fund {amount}")
    assert "סכום לא תקין" in reply(tg) and not getattr(client, "transfers", [])


def test_no_transfer_without_a_subaccount(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/subaccount fund 100")
    assert "אין תת-חשבון" in reply(tg) and not getattr(client, "transfers", [])


@pytest.mark.parametrize("error, text", [
    (KalshiError(400, "insufficient balance"), "Kalshi דחתה את העברת"),
    (KalshiError(503, "busy"), "לא ברור אם העברת"),
    (requests.ConnectionError("timed out"), "אין תשובה מ-Kalshi"),
])
def test_failed_transfers_say_so(ladder, tmp_path, error, text):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/subaccount create")
    client.order_error = error
    say(app, "/subaccount fund 100")
    assert text in reply(tg)


def test_needs_a_kalshi_key(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path, can_trade=False)
    say(app, "/subaccount create")
    assert "אין מפתח Kalshi" in reply(tg)


def test_strangers_get_silence(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/subaccount create", user="42")  # the alert bot's user isn't this bot's
    assert tg.sent == [] and not getattr(client, "created", 0)


def test_command_line(ladder, tmp_path, monkeypatch, capsys):
    from test_trader_app import FakeKalshi
    client = FakeKalshi(ladder)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.delenv("KALSHI_LADDER_AUTO_BOT_SUBACCOUNT", raising=False)
    monkeypatch.setattr(cli.Config, "kalshi_client", lambda self: client)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    cli.main(["subaccount", "create"])
    cli.main(["subaccount", "fund", "40"])
    cli.main(["subaccount"])
    out = capsys.readouterr().out
    assert "במספר 1" in out and "הועברו $40.00" in out and "יתרה בתת-חשבון KALSHI_LADDER_AUTO_BOT (1)" in out
    assert client.transfers == [(0, 1, D("40"))]
    with pytest.raises(SystemExit):
        cli.main(["subaccount", "fund"])
