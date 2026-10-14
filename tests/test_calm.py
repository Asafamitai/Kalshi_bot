from decimal import Decimal

import pytest
import requests
from test_live import auto_app, buys, positions

from kalshi_bot import calm
from kalshi_bot.config import Config
from kalshi_bot.kalshi import KalshiError

D = Decimal


def ladder(mk, **rung195):
    # Strict buys NO on "above 195" at 0.86 against NO "above 175" at 0.96/0.98.
    return [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16", **rung195)]


def strict(app):
    return [t for t in app.store.paper_trades() if t["strategy"] == "strict"]


def single(app):
    return [t for t in app.store.paper_trades() if t["strategy"] == "single"]


def test_a_calm_market_is_bought(mk, tmp_path):
    app, client, _ = auto_app(ladder(mk, prev_yes_bid="0.12", prev_yes_ask="0.14"), tmp_path)  # moved 2c
    app.scan_once()
    assert len(buys(client)) == 1 and len(strict(app)) == 1


def test_a_big_day_on_the_contract_bought_is_skipped(mk, tmp_path):
    # YES 195 was 0.04/0.06 a day ago and is 0.14/0.16 now: a 10c move.
    app, client, _ = auto_app(ladder(mk, prev_yes_bid="0.04", prev_yes_ask="0.06"), tmp_path)
    app.scan_once()
    assert buys(client) == [] and strict(app) == [] and len(single(app)) == 1  # only strict is calm-only


def test_a_big_day_on_the_reference_rung_is_skipped(mk, tmp_path):
    markets = [mk("APP-T175", 175, "0.02", "0.04", prev_yes_bid="0.10", prev_yes_ask="0.12"),
               mk("APP-T195", 195, "0.14", "0.16")]
    app, client, _ = auto_app(markets, tmp_path)
    app.scan_once()
    assert buys(client) == [] and strict(app) == []


def test_no_book_a_day_ago_is_not_calm(mk, tmp_path):
    app, client, _ = auto_app(ladder(mk, prev_yes_bid="0", prev_yes_ask="0"), tmp_path)
    app.scan_once()
    assert buys(client) == [] and strict(app) == []


def test_a_wide_week_is_skipped(mk, tmp_path):
    app, client, _ = auto_app(ladder(mk), tmp_path)
    client.history = {"APP-T195": [("0.02", "0.04"), ("0.14", "0.16")]}  # mid 0.03 then 0.15: 12c
    app.scan_once()
    assert buys(client) == [] and strict(app) == [] and len(single(app)) == 1


def test_the_mid_now_counts_toward_the_range(mk, tmp_path):
    app, client, _ = auto_app(ladder(mk), tmp_path)
    client.history = {"APP-T195": [("0.03", "0.05")]}  # history alone is flat at 0.04; now 0.15
    app.scan_once()
    assert buys(client) == []


def test_an_unreadable_history_buys_nothing_and_is_asked_again(mk, tmp_path):
    app, client, _ = auto_app(ladder(mk), tmp_path)

    def broken(*a, **k):
        raise requests.ConnectionError("timed out")
    client.candlesticks = broken
    app.scan_once()
    assert buys(client) == [] and strict(app) == []
    del client.candlesticks  # back to the class's method
    app.scan_once()
    assert len(buys(client)) == 1


def test_one_history_read_serves_the_real_trade_and_its_paper_twin(mk, tmp_path):
    app, client, _ = auto_app(ladder(mk), tmp_path)
    app.scan_once()
    assert len(buys(client)) == 1 and len(strict(app)) == 1 and client.history_reads == 1


def test_zero_turns_each_check_off(mk, tmp_path):
    app, client, _ = auto_app(ladder(mk, prev_yes_bid="0.04", prev_yes_ask="0.06"), tmp_path,
                              calm_max_move_24h=D("0"), calm_max_range=D("0"))
    client.history = {"APP-T195": [("0.02", "0.04")]}
    app.scan_once()
    assert len(buys(client)) == 1 and client.__dict__.get("history_reads", 0) == 0


def test_history_is_asked_for_hourly_over_the_configured_days(mk, tmp_path):
    seen = []

    class Client:
        def candlesticks(self, ticker, start, end, minutes=60):
            seen.append((ticker, end - start, minutes))
            return []
    assert calm.history_bounds(Client(), "APP-T195", Config(calm_history_days=2), 1_800_000_000.0) is None
    assert seen == [("APP-T195", 2 * 86400, 60)]


def test_calm_config():
    d = Config.from_env({})
    assert (d.calm_max_move_24h, d.calm_max_range, d.calm_history_days, d.calm_min_hours_left) == \
        (D("0.05"), D("0.10"), 3, 168)
    cfg = Config.from_env({"CALM_MAX_MOVE_24H": "0.03", "CALM_MAX_RANGE": "0.08", "CALM_HISTORY_DAYS": "5",
                           "CALM_MIN_HOURS_LEFT": "72"})
    assert (cfg.calm_max_move_24h, cfg.calm_max_range, cfg.calm_history_days, cfg.calm_min_hours_left) == \
        (D("0.03"), D("0.08"), 5, 72)
