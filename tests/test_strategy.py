from datetime import datetime, timedelta, timezone

from kalshi_bot.backtest import closed_on_schedule, simulate_entry
from kalshi_bot.ledger import Ledger, settlement_payout
from kalshi_bot.strategy import (Candidate, StrategyParams, evaluate_market, find_candidates,
                                 net_profit_if_win, size_order, taker_fee)

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
P = StrategyParams()


def market(yes_bid="0.90", yes_ask="0.92", seconds=120, **kw):
    m = {
        "ticker": "KXBTC15M-TEST",
        "event_ticker": "KXBTC15M-26OCT131200",
        "title": "test",
        "status": "active",
        "market_type": "binary",
        "close_time": (NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_ask_dollars": f"{1 - float(yes_bid):.4f}",
        "yes_ask_size_fp": "100.00",
        "yes_bid_size_fp": "50.00",
        "volume_fp": "500.00",
    }
    m.update(kw)
    return m


def test_fee_rounds_up_to_cent():
    assert taker_fee(0.90, 1) == 0.01          # 0.0063 -> 1c
    assert taker_fee(0.90, 100) == 0.63
    assert taker_fee(0.50, 100) == 1.75


def test_buys_yes_when_yes_is_favorite():
    c = evaluate_market(market(), NOW, P)
    assert c and c.side == "yes" and c.price == 0.92 and c.available == 100


def test_buys_no_when_no_is_favorite():
    c = evaluate_market(market(yes_bid="0.06", yes_ask="0.08"), NOW, P)
    assert c and c.side == "no" and c.price == 0.94 and c.available == 50


def test_rejects_cheap_and_too_expensive():
    assert evaluate_market(market(yes_bid="0.70", yes_ask="0.72"), NOW, P) is None
    assert evaluate_market(market(yes_bid="0.98", yes_ask="0.99"), NOW, P) is None


def test_time_window():
    assert evaluate_market(market(seconds=301), NOW, P) is None
    assert evaluate_market(market(seconds=3), NOW, P) is None
    assert evaluate_market(market(seconds=300), NOW, P) is not None


def test_rejects_wide_spread_and_empty_book():
    assert evaluate_market(market(yes_bid="0.01", yes_ask="0.95"), NOW, P) is None
    assert evaluate_market(market(yes_bid="0.00", yes_ask="0.90"), NOW, P) is None


def test_rejects_parlays_and_inactive():
    assert evaluate_market(market(ticker="KXMVECROSS-X"), NOW, P) is None
    assert evaluate_market(market(status="closed"), NOW, P) is None


def test_candidates_sorted_by_time_to_close():
    ms = [market(seconds=200, ticker="B"), market(seconds=60, ticker="A")]
    assert [c.ticker for c in find_candidates(ms, NOW, P)] == ["A", "B"]


def test_size_respects_budget_book_and_fees():
    c = Candidate("T", "", "yes", 0.90, available=100, seconds_to_close=60, close_time="")
    n = size_order(c, 10.0)
    assert n == 11 and n * 0.90 + taker_fee(0.90, n) <= 10.0
    assert size_order(Candidate("T", "", "yes", 0.90, 3, 60, ""), 10.0) == 3
    # 1 contract at 99c: 1c profit minus 1c fee -> not worth it
    assert size_order(Candidate("T", "", "yes", 0.99, 100, 60, ""), 1.0) == 0
    assert net_profit_if_win(0.90, 11) == round(1.1 - taker_fee(0.90, 11), 4)


def test_settlement_payout():
    assert settlement_payout({"result": "yes"}, "yes") == 1.0
    assert settlement_payout({"result": "yes"}, "no") == 0.0
    assert settlement_payout({"result": "", "status": "active"}, "yes") is None


class FakeClient:
    def __init__(self, markets):
        self.markets = markets

    def get_market(self, ticker):
        return self.markets[ticker]


def test_ledger_settles_and_reports(tmp_path):
    led = Ledger(str(tmp_path / "t.json"))
    closed = (NOW - timedelta(minutes=1)).isoformat()
    led.add({"ticker": "W", "side": "yes", "price": 0.9, "count": 10, "cost": 9.0, "fee": 0.07,
             "close_time": closed, "status": "open"})
    led.add({"ticker": "L", "side": "no", "price": 0.9, "count": 10, "cost": 9.0, "fee": 0.07,
             "close_time": closed, "status": "open"})
    led.settle(FakeClient({"W": {"result": "yes"}, "L": {"result": "yes"}}), NOW)
    pnl = {t["ticker"]: t["pnl"] for t in Ledger(led.path).trades}
    assert pnl == {"W": 0.93, "L": -9.07}
    assert led.open_exposure() == 0


def test_backtest_takes_first_qualifying_minute():
    close = NOW + timedelta(minutes=5)
    m = market(close_time=close.isoformat().replace("+00:00", "Z"), result="no")
    ts = int(NOW.timestamp())
    candles = [
        {"end_period_ts": ts, "yes_bid": {"close_dollars": "0.50"}, "yes_ask": {"close_dollars": "0.52"}},
        {"end_period_ts": ts + 60, "yes_bid": {"close_dollars": "0.10"}, "yes_ask": {"close_dollars": "0.12"}},
        {"end_period_ts": ts + 120, "yes_bid": {"close_dollars": "0.03"}, "yes_ask": {"close_dollars": "0.05"}},
    ]
    t = simulate_entry(m, candles, P, stake=10)
    assert t["side"] == "no" and t["price"] == 0.90 and t["seconds_to_close"] == 240 and t["won"]


def test_early_close_detection():
    assert closed_on_schedule({"close_time": "2026-10-13T09:00:00Z"})
    assert not closed_on_schedule({"close_time": "2026-10-13T08:38:26Z"})
