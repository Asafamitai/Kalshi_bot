from decimal import Decimal

import pytest

from kalshi_bot import trader
from kalshi_bot.app import App
from kalshi_bot.config import Config
from kalshi_bot.ladder import find_violations
from kalshi_bot.store import Store

D = Decimal


class FakeKalshi:
    def __init__(self, markets):
        self.markets = {m["ticker"]: m for m in markets}
        self.orders = []

    def iter_open_markets(self):
        return iter(self.markets.values())

    def get_markets(self, tickers):
        return [self.markets[t] for t in tickers if t in self.markets]

    def buy_ioc(self, ticker, side, count, limit_price, client_order_id):
        self.orders.append((ticker, side, count, limit_price))
        return {
            "fill_count_fp": f"{count}.00",
            "taker_fill_cost_dollars": str(limit_price * count),
            "taker_fees_dollars": "0.09",
        }


class FakeTelegram:
    def __init__(self):
        self.sent, self.answers, self.cleared = [], [], []

    def send(self, chat_id, text, buttons=None):
        self.sent.append((text, buttons))

    def answer(self, callback_id, text=""):
        self.answers.append(text)

    def clear_buttons(self, chat_id, message_id):
        self.cleared.append(message_id)


@pytest.fixture
def ladder(mk):
    return [
        mk("APP-T175", 175, "0.02", "0.04"),
        mk("APP-T195", 195, "0.14", "0.16", yes_bid_size="40.00"),
    ]


def make_app(markets, tmp_path, **cfg):
    config = Config(telegram_chat_id="42", telegram_allowed_user_ids=frozenset({"42"}), db_path=":memory:", **cfg)
    client = FakeKalshi(markets)
    tg = FakeTelegram()
    return App(config, Store(str(tmp_path / "t.sqlite3")), client, client, tg), client, tg


def no_button(tg):
    """The (label, callback_data) of the most recent NO-side buy button."""
    return [b[0] for _, b in tg.sent if b and " NO @ " in b[0][0]][-1]


def tap(app, callback_data, user="42"):
    app.handle_callback(
        {"id": "cb", "from": {"id": int(user)}, "data": callback_data, "message": {"chat": {"id": 42}, "message_id": 7}}
    )


def test_alert_has_buy_button_sized_to_budget(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    no_alerts = [(t, b) for t, b in tg.sent if "Buy <b>NO" in t]
    assert len(no_alerts) == 1
    text, buttons = no_alerts[0]
    assert "11¢" in text and "0.86" in text
    assert buttons[0][0] == "Buy 11 NO @ 0.86"  # $10 / 0.86


def test_both_faces_of_one_inversion_are_alerted(ladder, tmp_path):
    # Buying NO on 195 or YES on 175 are two ways into the same contradiction.
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    labels = sorted(b[0][0] for _, b in tg.sent if b)
    assert labels == ["Buy 11 NO @ 0.86", "Buy 250 YES @ 0.04"]


def test_same_violation_is_not_realerted(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    first = len(tg.sent)
    assert app.scan_once() == []
    assert len(tg.sent) == first


def test_empty_book_alert_has_no_button(mk, tmp_path):
    markets = [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "1.00")]
    app, _, tg = make_app(markets, tmp_path)
    app.scan_once()
    (text, buttons) = [(t, b) for t, b in tg.sent if "Buy <b>NO" in t][0]
    assert "Empty book" in text
    assert buttons is None


def test_tap_rechecks_then_dry_runs(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    app.scan_once()
    tap(app, no_button(tg)[1])
    assert client.orders == []
    assert "Dry run" in tg.sent[-1][0] and "11 NO APP-T195 @ 0.86" in tg.sent[-1][0]


def test_tap_buys_ioc_at_rechecked_ask_when_live(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, live_trading=True)
    app.scan_once()
    _, data = no_button(tg)
    tap(app, data)
    assert client.orders == [("APP-T195", "no", 11, D("0.86"))]
    assert tg.sent[-1][0].startswith("✅ Bought 11 of 11 NO APP-T195")
    assert tg.cleared == [7]
    tap(app, data)  # double tap
    assert len(client.orders) == 1 and tg.answers[-1] == "Already handled."


def test_tap_refuses_when_contradiction_is_gone(ladder, mk, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, live_trading=True)
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")  # repriced into line
    tap(app, no_button(tg)[1])
    assert client.orders == []
    assert "contradiction is gone" in tg.sent[-1][0]


def test_tap_never_pays_more_than_the_button_showed(ladder, mk, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, live_trading=True)
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.12", "0.14")  # NO ask 0.88, still 9c
    _, data = no_button(tg)
    tap(app, data)
    assert client.orders == []
    assert "moved from 0.86 to 0.88" in tg.sent[-2][0]
    label, fresh = tg.sent[-1][1][0]  # fresh alert, fresh button
    assert label == "Buy 11 NO @ 0.88" and fresh != data


def test_tap_from_stranger_is_ignored(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, live_trading=True)
    app.scan_once()
    tap(app, no_button(tg)[1], user="999")
    assert client.orders == [] and tg.answers == ["Not authorised."]


def test_recheck_refuses_closed_market(ladder, tmp_path):
    client = FakeKalshi(ladder)
    store = Store(str(tmp_path / "t.sqlite3"))
    v = [v for v in find_violations(ladder, D("0.02"), D("0.10")) if v.side == "no"][0]
    alert = store.get_alert(store.add_alert(v))
    client.markets["APP-T175"]["status"] = "closed"
    v, problem = trader.recheck(client, alert, D("0.02"), D("0.10"))
    assert v is None and "no longer trading" in problem


def test_order_with_no_answer_disables_the_button(ladder, tmp_path):
    import requests

    app, client, tg = make_app(ladder, tmp_path, live_trading=True)
    app.scan_once()

    def timeout(*a, **k):
        client.orders.append(a)
        raise requests.ConnectionError("read timed out")

    client.buy_ioc = timeout
    _, data = no_button(tg)
    tap(app, data)
    assert "Check your Kalshi positions" in tg.sent[-1][0]
    assert tg.cleared == [7]
    tap(app, data)  # must not send a second order
    assert len(client.orders) == 1 and tg.answers[-1] == "Already handled."


def test_rejected_order_can_be_tapped_again(ladder, tmp_path):
    from kalshi_bot.kalshi import KalshiError

    app, client, tg = make_app(ladder, tmp_path, live_trading=True)
    app.scan_once()

    def reject(*a, **k):
        raise KalshiError(400, "insufficient balance")

    client.buy_ioc = reject
    _, data = no_button(tg)
    tap(app, data)
    assert "rejected" in tg.sent[-1][0] and tg.cleared == []
    assert app.store.get_alert(int(data[4:])).status == "open"


def test_alert_that_failed_to_send_is_retried_next_scan(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    real_send = tg.send

    def flaky(chat_id, text, buttons=None):
        raise RuntimeError("telegram down")

    tg.send = flaky
    app.scan_once()
    assert tg.sent == []
    tg.send = real_send
    assert len(app.scan_once()) == 2


def test_tap_refuses_when_reference_book_goes_wide(ladder, mk, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, live_trading=True)
    app.scan_once()
    client.markets["APP-T175"] = mk("APP-T175", 175, "0.01", "0.99")  # quotes pulled to placeholders
    tap(app, no_button(tg)[1])
    assert client.orders == []
    assert "no longer has a real price" in tg.sent[-1][0]
