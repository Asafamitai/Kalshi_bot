import copy
from decimal import Decimal

import pytest
import requests

from kalshi_bot import episodes
from kalshi_bot.app import App
from kalshi_bot.config import Config
from kalshi_bot.kalshi import KalshiError
from kalshi_bot.store import Store

D = Decimal
HOUR = 3600


class FakeKalshi:
    """`scan_markets` is what a full scan reads; `markets` is the live book for re-reads."""

    def __init__(self, markets, can_trade=True):
        self.markets = {m["ticker"]: m for m in markets}
        self.scan_markets = None
        self.orders = []
        self.can_trade = can_trade
        self.cash = D("1000")

    def iter_open_markets(self):
        source = self.scan_markets if self.scan_markets is not None else self.markets
        return iter(copy.deepcopy(list(source.values())))

    def get_markets(self, tickers):
        return [copy.deepcopy(self.markets[t]) for t in tickers if t in self.markets]

    def balance(self):
        return self.cash

    def buy(self, ticker, side, count, limit_price, client_order_id):
        self.orders.append((ticker, side, count, limit_price, client_order_id))
        return {"already": False, "order_id": "o1", "filled": D(count), "avg_price": limit_price}


class FakeTelegram:
    def __init__(self):
        self.sent, self.answers, self.cleared = [], [], []

    def send(self, chat_id, text, buttons=None):
        self.sent.append((text, buttons))

    def answer(self, callback_id, text=""):
        self.answers.append(text)

    def clear_buttons(self, chat_id, message_id):
        self.cleared.append(message_id)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def ladder(mk):
    # NO on "above 195" at 0.86 while NO on "above 175" sits at 0.96/0.98: an 11c saving.
    # The mirror trade, YES on 175 at 0.04, is outside the 0.30-0.90 price band.
    return [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16")]


def make_app(markets, tmp_path, can_trade=True, **cfg):
    config = Config(telegram_chat_id="42", telegram_allowed_user_ids=frozenset({"42"}), **cfg)
    client = FakeKalshi(markets, can_trade=can_trade)
    tg = FakeTelegram()
    app = App(config, Store(str(tmp_path / "t.sqlite3")), client, client, tg, clock=Clock())
    return app, client, tg


def alerts_sent(tg):
    return [(t, b) for t, b in tg.sent if "אפשרות חדשה" in t]


def button(tg, label_start):
    for _, rows in reversed(tg.sent):
        for row in rows or []:
            for label, data in row:
                if label.startswith(label_start):
                    return data
    raise AssertionError(f"no button starting with {label_start!r}")


def tap(app, data, user="42"):
    app.handle_callback(
        {"id": "cb", "from": {"id": int(user)}, "data": data, "message": {"chat": {"id": 42}, "message_id": 7}}
    )


def say(app, text, user="42"):
    app.handle_message({"from": {"id": int(user)}, "chat": {"id": 42}, "text": text})


# Alerts


def test_alert_in_hebrew_with_sized_buttons(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    (text, rows), = alerts_sent(tg)
    assert "קנה <b>NO · Above 195</b> ב-<b>0.86</b>" in text
    assert "חיסכון מול האמצע <b>11¢</b>" in text
    assert "נטו אחרי עמלה <b>10.2¢</b>" in text  # 11c - 0.07*0.86*0.14
    labels = [label for label, _ in rows[0]]
    assert labels == ["קנה 10 · $8.69", "קנה 50 · $43.43", "הכל 100 · $86.85"]


def test_price_band_filters_the_cheap_mirror_trade(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    assert [t.count("קנה <b>YES") for t, _ in alerts_sent(tg)] == [0]


def test_thin_or_unloved_markets_are_tracked_not_alerted(mk, tmp_path):
    thin = [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16", yes_bid_size="20.00")]
    app, _, tg = make_app(thin, tmp_path)
    app.scan_once()
    assert alerts_sent(tg) == []
    assert len(app.store.open_episodes()) == 2  # still tracked (NO 195 and YES 175)


def test_cooldown_then_realert(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    app.clock.now += 6 * HOUR
    app.scan_once()
    assert len(alerts_sent(tg)) == 1
    app.clock.now += 7 * HOUR
    app.scan_once()
    assert len(alerts_sent(tg)) == 2


def test_cooldown_survives_a_restart(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    app.scan_once()
    restarted = App(app.cfg, Store(str(tmp_path / "t.sqlite3")), client, client, tg, clock=app.clock)
    restarted.scan_once()
    assert len(alerts_sent(tg)) == 1


def test_flicker_is_dropped_by_the_second_read(ladder, mk, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    client.scan_markets = dict(client.markets)
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")  # fixed by the time we re-read
    app.scan_once()
    assert alerts_sent(tg) == []


def test_at_most_n_alerts_per_pass_best_first(mk, tmp_path):
    markets = []
    for i, ask_no in enumerate(["0.86", "0.80", "0.84"]):
        ev = f"EV{i}"
        markets += [mk(f"{ev}-T175", 175, "0.02", "0.04", event=ev),
                    mk(f"{ev}-T195", 195, str(1 - D(ask_no)), str(1 - D(ask_no) + D("0.02")), event=ev)]
    app, _, tg = make_app(markets, tmp_path, max_alerts_per_pass=2)
    app.scan_once()
    sent = [t for t, _ in alerts_sent(tg)]
    assert len(sent) == 2
    assert "ב-<b>0.80</b>" in sent[0] and "ב-<b>0.84</b>" in sent[1]


def test_empty_book_alert_has_a_warning_and_no_buttons(mk, tmp_path):
    # NO ask 0.86 with no NO bid behind it (YES ask 1.00).
    markets = [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "1.00")]
    app, _, tg = make_app(markets, tmp_path)
    app.scan_once()
    (text, buttons), = alerts_sent(tg)
    assert "ספר ריק" in text and buttons is None


def test_failed_send_is_retried_next_pass(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    real = tg.send
    tg.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down"))
    app.scan_once()
    tg.send = real
    app.scan_once()
    assert len(alerts_sent(tg)) == 1


# Buying


def test_tap_with_trading_off_is_a_dry_run(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    app.scan_once()
    tap(app, button(tg, "קנה 10"))
    assert client.orders == []
    assert "המסחר כבוי" in tg.sent[-1][0] and "10 NO APP-T195 ב-0.86" in tg.sent[-1][0]


def test_tap_buys_with_deterministic_order_id(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()
    data = button(tg, "קנה 50")
    tap(app, data)
    alert_id = data.split(":")[1]
    assert client.orders == [("APP-T195", "no", 50, D("0.86"), f"lk-{alert_id}-50")]
    assert tg.sent[-1][0].startswith("✅ נקנו 50 מתוך 50 NO APP-T195")
    assert tg.cleared == [7]
    tap(app, data)  # double tap
    assert len(client.orders) == 1 and tg.answers[-1] == "כבר טופל."


def test_tap_all_buys_the_depth_at_the_ask(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()
    tap(app, button(tg, "הכל"))
    assert client.orders[0][2] == 100


def test_trading_cannot_be_switched_on_without_a_key(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path, can_trade=False)
    say(app, "/trading on")
    assert "אין מפתח" in tg.sent[-1][0]
    assert not app.store.trading_on


def test_tap_refuses_when_contradiction_is_gone(ladder, mk, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")
    tap(app, button(tg, "קנה 10"))
    assert client.orders == [] and "הסתירה נסגרה" in tg.sent[-1][0]


def test_tap_never_pays_more_than_the_button_showed(ladder, mk, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()
    old = button(tg, "קנה 10")
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.12", "0.14")  # NO ask 0.88, still 9c
    tap(app, old)
    assert client.orders == []
    assert "עלה מ-0.86 ל-0.88" in tg.sent[-2][0]
    assert "ב-<b>0.88</b>" in tg.sent[-1][0] and button(tg, "קנה 10") != old


def test_tap_refuses_when_reference_book_goes_wide(ladder, mk, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()
    client.markets["APP-T175"] = mk("APP-T175", 175, "0.01", "0.99")
    tap(app, button(tg, "קנה 10"))
    assert client.orders == [] and "אין מחיר אמיתי" in tg.sent[-1][0]


def test_tap_refuses_when_cash_is_short(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()
    client.cash = D("5")
    tap(app, button(tg, "קנה 10"))
    assert client.orders == [] and "היתרה" in tg.sent[-1][0]


def test_order_with_no_answer_disables_the_button(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()

    def timeout(*a, **k):
        client.orders.append(a)
        raise requests.ConnectionError("read timed out")

    client.buy = timeout
    data = button(tg, "קנה 10")
    tap(app, data)
    assert "בדוק את הפוזיציות" in tg.sent[-1][0] and tg.cleared == [7]
    tap(app, data)
    assert len(client.orders) == 1 and tg.answers[-1] == "כבר טופל."


def test_rejected_order_can_be_tapped_again(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on")
    app.scan_once()

    def reject(*a, **k):
        raise KalshiError(400, "insufficient balance")

    client.buy = reject
    data = button(tg, "קנה 10")
    tap(app, data)
    assert "דחתה" in tg.sent[-1][0] and tg.cleared == []
    assert app.store.get_alert(int(data.split(":")[1])).status == "open"


def test_strangers_are_ignored(ladder, tmp_path):
    app, client, tg = make_app(ladder, tmp_path)
    say(app, "/trading on", user="999")
    assert not app.store.trading_on and tg.sent == []
    say(app, "/trading on")
    app.scan_once()
    tap(app, button(tg, "קנה 10"), user="999")
    assert client.orders == [] and tg.answers == ["Not authorised."]


def test_status_command(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    app.scan_once()
    say(app, "/status")
    assert "מסחר: <b>כבוי</b>" in tg.sent[-1][0] and "2 סתירות" in tg.sent[-1][0]


# Episodes


def test_episodes_open_and_close(ladder, mk, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    app.scan_once()
    app.clock.now += 300
    app.scan_once()
    client.markets["APP-T195"] = mk("APP-T195", 195, "0.02", "0.03")
    client.markets["APP-T175"] = mk("APP-T175", 175, "0.02", "0.04")
    app.clock.now += 300
    app.scan_once()
    closed = app.store.closed_episodes()
    assert len(closed) == 2 and all(e["last_seen"] - e["opened_at"] == 300 for e in closed)
    assert sum(e["alertable"] for e in closed) == 1  # only the NO side passed the filters
    text = episodes.report(app.store, now=app.clock.now)
    assert "n=2" in text and "median 5 min" in text
