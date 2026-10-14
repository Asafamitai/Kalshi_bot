import math
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import requests
from test_live import auto_app, buys

from kalshi_bot import underlying
from kalshi_bot.config import Config
from kalshi_bot.ladder import Rung
from kalshi_bot.zoom import Past

D = Decimal
NOW = 1_800_000_000.0  # the tests' clock
DAY = 86400


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def wti_ladder(mk, days_left=10):
    # A WTI weekly ladder: strict buys NO on "above 195" at 0.86 (it pays if WTI ends at or below 195).
    end = iso(NOW + days_left * DAY)
    return [mk("KXWTIW-26OCT31-T175", 175, "0.02", "0.04", event="KXWTIW-26OCT31", close_time=end),
            mk("KXWTIW-26OCT31-T195", 195, "0.14", "0.16", event="KXWTIW-26OCT31", close_time=end)]


def settled(latest, days=20, swing=1, last_at=NOW - 3600):
    """KXWTI's daily settlements, one a day, the last at `last_at`: latest, latest + swing, latest, ..., latest."""
    out = []
    for i in range(days):
        back = days - 1 - i  # days before the last
        t = last_at - back * DAY
        value = latest + (swing if back % 2 else 0)
        out.append({"event_ticker": f"KXWTI-{i}", "ticker": f"KXWTI-{i}-T1", "close_time": iso(t),
                    "expiration_value": str(value)})
    return out


def calm_app(mk, tmp_path, history, **cfg):
    cfg.setdefault("underlying_min_z", 2)
    app, client, tg = auto_app(wti_ladder(mk), tmp_path, **cfg)
    client.settled = history
    return app, client, tg


def strict(app):
    return [t for t in app.store.paper_trades() if t["strategy"] == "strict"]


# Which series measures what


@pytest.mark.parametrize("series, source", [
    ("KXUST10AM", "KX10YRRATEH"), ("KXNOTE10W", "KX10YRRATEH"), ("KXTNOTED", "KX10YRRATEH"),
    ("KXNOTE30", "KX30YRRATEH"), ("KX2YRRATEH", "KX2YRRATEH"),
    ("KXWTIMIN", "KXWTI"), ("KXWTIW", "KXWTI"), ("WTIMAX", "KXWTI"), ("KXBRENTW", "KXBRENTD"),
    ("KXAAAGASMAXM", "KXAAAGASD"), ("KXAAAGASMINM", "KXAAAGASD"), ("KXAAAGASW", "KXAAAGASD"),
    ("KXAAAGASMAXCA", "KXAAAGASDCA"), ("KXAAAGASDTX", "KXAAAGASDTX"), ("KXAAAGASWNJ", "KXAAAGASDNJ"),
    ("KXBTCMAXY", "KXBTCD"), ("KXETHMINMON", "KXETHD"), ("KXSOLD26", "KXSOLD"), ("KXRIPPLEMAXY", "KXXRPD"),
    ("KXXRPMAXY", "KXXRPD"), ("KXDOGEMAXW", "KXDOGED"), ("KXINXY", "KXINX"), ("KXNDQDIRY", "KXNASDAQ100"),
    ("KXGOLDDIRY", "KXGOLDD"), ("KXSILVERMON", "KXSILVERD"), ("KXNGASMAX", "KXNATGASD"), ("KXBRENTW", "KXBRENTD"),
    ("KXUSDNOKAW", "KXUSDNOKAD"), ("KXUSDCADAW", "KXUSDCADAD"), ("USDJPYMAX", "KXUSDJPYAD"),
])
def test_series_find_their_source(series, source):
    assert underlying.source_for(series).series == source


@pytest.mark.parametrize("series", [
    "KXOPENVREQ", "KXVOTEGENERAL", "KXNCAAF1HSPREAD", "KXAMZNCC", "KXAPP",
    # A known asset name followed by something that isn't a price: dominance, holdings, the DOGE agency...
    "KXBTCDOM", "KXBTCVSETH", "KXSOLAR", "KXSOLICITORGENERAL", "KXSOLDATHOLDINGS", "KXGOLDCARD",
    "KXGOLDENBACHELOR", "KXDOGESAVINGS", "KXWTIVSBRENT", "KXSILVERSTATES", "KXCOPPEROPEN", "KXETHFLIP",
])
def test_series_without_a_source(series):
    assert underlying.source_for(series) is None


def test_hourly_move_scales_each_change_by_its_gap():
    # A 2-unit move over 4 hours is 1 unit/sqrt(hour); so is a 1-unit move over 1 hour.
    past = [Past(f"e{i}", t * 3600.0, D(v)) for i, (t, v) in enumerate([(0, 0), (4, 2), (5, 1), (9, 3), (10, 2),
                                                                      (14, 4), (15, 3), (19, 5)])]
    assert underlying.hourly_move(past) == pytest.approx(1.0)
    assert underlying.hourly_move(past[:5]) is None  # too few values


def test_outlook_numbers(mk):
    rung = Rung.from_market(wti_ladder(mk)[1])  # above 195, 10 days left
    from test_trader_app import FakeKalshi
    client = FakeKalshi([])
    client.settled = settled(185)
    o = underlying.outlook(client, rung, "no", NOW)
    hourly = math.sqrt(1 / 24)  # +-1 a day
    assert (o.source, o.latest, o.status_quo) == ("KXWTI", D("185"), True)
    assert o.hourly == pytest.approx(hourly, rel=0.05)
    assert o.z == pytest.approx(10 / (hourly * math.sqrt(240)), rel=0.05)  # ~3.2


def test_a_strike_on_another_scale_is_not_that_underlying(mk):
    # A WTI series whose strikes are percentages: 4.5 against WTI at 185 is not the same quantity.
    m = mk("KXWTIW-26OCT31-T4", 4.5, "0.14", "0.16", event="KXWTIW-26OCT31", close_time=iso(NOW + 10 * DAY))
    from test_trader_app import FakeKalshi
    client = FakeKalshi([])
    client.settled = settled(185)
    assert underlying.outlook(client, Rung.from_market(m), "no", NOW) is None


# The check in the strategy


def test_far_from_the_strike_is_bought(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(185))  # z ~3.2
    app.scan_once()
    assert len(buys(client)) == 1 and len(strict(app)) == 1


def test_too_close_to_the_strike_is_skipped(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(190))  # z ~1.6
    app.scan_once()
    assert buys(client) == [] and strict(app) == []


def test_paying_only_if_it_moves_is_skipped(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(205))  # WTI above 195: NO pays only if it falls back
    app.scan_once()
    assert buys(client) == [] and strict(app) == []


def test_a_lower_bar_buys_closer(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(190), underlying_min_z=1.5)
    app.scan_once()
    assert len(buys(client)) == 1


def test_no_underlying_no_buy(mk, tmp_path):
    ladder = [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16")]
    app, client, _ = auto_app(ladder, tmp_path, underlying_min_z=2)  # KXAPP: nothing measures it
    app.scan_once()
    assert buys(client) == [] and strict(app) == []
    assert len([t for t in app.store.paper_trades() if t["strategy"] == "single"]) == 1  # only strict is calm-only


def test_a_stale_source_is_not_trusted(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(185, last_at=NOW - 5 * DAY))
    app.scan_once()
    assert buys(client) == []


def test_too_little_history_is_not_trusted(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(185, days=5))
    app.scan_once()
    assert buys(client) == []


def test_an_unreadable_source_buys_nothing_and_is_asked_again(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(185))

    def broken(*a, **k):
        raise requests.ConnectionError("timed out")
    client.settled_markets = broken
    app.scan_once()
    assert buys(client) == []
    del client.settled_markets  # back to the class's method
    app.scan_once()
    assert len(buys(client)) == 1


def test_one_read_serves_the_real_trade_and_its_paper_twin(mk, tmp_path):
    app, client, _ = calm_app(mk, tmp_path, settled(185))
    reads = []
    real = client.settled_markets
    client.settled_markets = lambda *a, **k: reads.append(a[0]) or real(*a, **k)
    app.scan_once()
    assert len(buys(client)) == 1 and len(strict(app)) == 1 and reads.count("KXWTI") == 1


def test_a_week_left_is_still_required(mk, tmp_path):
    app, client, _ = auto_app(wti_ladder(mk, days_left=5), tmp_path, underlying_min_z=2)
    client.settled = settled(185)
    app.scan_once()
    assert buys(client) == []


def test_calm_config():
    d = Config.from_env({})
    assert (d.calm_min_hours_left, d.underlying_min_z) == (168, 2.0)
    cfg = Config.from_env({"CALM_MIN_HOURS_LEFT": "72", "UNDERLYING_MIN_Z": "1.5"})
    assert (cfg.calm_min_hours_left, cfg.underlying_min_z) == (72, 1.5)
