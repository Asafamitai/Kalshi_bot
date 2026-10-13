from decimal import Decimal

import pytest
from test_live import auto_app, positions, say
from test_trader_app import alerts_sent, make_app
from test_trader_app import say as say_to_alert_bot

from kalshi_bot import alerts, zoom
from kalshi_bot.config import Config
from kalshi_bot.fees import fee_per_contract
from kalshi_bot.ladder import Rung, find_violations

D = Decimal


@pytest.fixture
def ladder(mk):
    # NO on "above 195" at 0.86 while NO on "above 175" sits at 0.96/0.98.
    return [mk("APP-T175", 175, "0.02", "0.04"), mk("APP-T195", 195, "0.14", "0.16")]


def settled(event, value, close="2030-10-01T00:00:00Z"):
    """A settled market of an earlier event in the series, shaped like Kalshi's."""
    return {"ticker": f"{event}-T1", "event_ticker": event, "status": "finalized", "result": "no",
            "expiration_value": value, "close_time": close}


# The reference rung must trade


def test_a_reference_rung_that_barely_trades_is_tracked_not_alerted(mk, tmp_path):
    thin = [mk("APP-T175", 175, "0.02", "0.04", volume_24h="3.24"), mk("APP-T195", 195, "0.14", "0.16")]
    app, _, tg = make_app(thin, tmp_path)
    app.scan_once()
    assert alerts_sent(tg) == []
    assert len(app.store.open_episodes()) == 2  # still tracked


def test_the_reference_floor_is_configurable(mk):
    v = next(v for v in find_violations([mk("APP-T175", 175, "0.02", "0.04", volume_24h="3.24"),
                                         mk("APP-T195", 195, "0.14", "0.16")], D("0.01"), D("0.10"))
             if v.side == "no")
    now = 1_800_000_000.0
    assert not alerts.alertable(v, Config(), now)
    assert alerts.alertable(v, Config.from_env({"MIN_REF_VOLUME_24H": "3"}), now)
    assert Config.from_env({}).min_ref_volume_24h == D("20")


def test_live_trading_skips_a_thin_reference(mk, tmp_path):
    thin = [mk("APP-T175", 175, "0.02", "0.04", volume_24h="3.24"), mk("APP-T195", 195, "0.14", "0.16")]
    app, client, _ = auto_app(thin, tmp_path)
    app.scan_once()
    assert client.orders == [] and positions(app) == []


# Reading the series' history


@pytest.mark.parametrize("strike_type,value,yes", [
    ("greater", "24.1", False), ("greater", "24.2", True), ("greater_or_equal", "24.1", True),
    ("less", "24.1", False), ("less", "24.0", True), ("less_or_equal", "24.1", True),
])
def test_pays_follows_the_strike_type(mk, strike_type, value, yes):
    rung = Rung.from_market(mk("X", D("24.1"), "0.30", "0.33", strike_type=strike_type))
    assert zoom.pays(rung, "yes", D(value)) is yes
    assert zoom.pays(rung, "no", D(value)) is not yes


def test_history_is_one_number_per_settled_event_oldest_first():
    markets = [settled("KXAPP-12OCT", "190", "2030-10-12T00:00:00Z"),
               settled("KXAPP-10OCT", "1,250", "2030-10-10T00:00:00Z"),
               settled("KXAPP-10OCT", "1,250", "2030-10-10T00:00:00Z"),  # another rung of the same event
               settled("KXAPP-08OCT", "Above 5", "2030-10-08T00:00:00Z"),  # not a number
               settled("KXAPP-26OCT", "180", "2030-10-26T00:00:00Z")]  # the event being zoomed on
    past = zoom.history(markets, skip_event="KXAPP-26OCT")
    assert [(p.event, p.value) for p in past] == [("KXAPP-10OCT", D("1250")), ("KXAPP-12OCT", D("190"))]
    assert zoom.parse_value("3.5%") == D("3.5") and zoom.parse_value(None) is None


# The zoom


def test_zoom_reads_the_ladder_the_reference_and_the_history(ladder, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    # NO on "above 195" pays at 195 or below; NO on "above 175" only at 175 or below.
    client.settled = [settled(f"KXAPP-{d:02d}OCT", v, f"2030-10-{d:02d}T00:00:00Z")
                      for d, v in [(1, "185"), (3, "170"), (5, "200"), (7, "190"), (9, "180"), (11, "210")]]
    z = zoom.build(client, app.cfg, "APP-T195", "no", now=1_800_000_000.0)
    assert [r.ticker for r in z.ladder] == ["APP-T175", "APP-T195"]
    assert z.reference.ticker == "APP-T175"  # found as the scan would, with none given
    assert z.paid == D("0.86") + fee_per_contract(D("0.86"))  # the ask and its fee
    assert zoom.hit_rate(z, z.past) == (4, 6)
    assert zoom.band(z) == (D("0.85") - D("0.97"), 3, 6)  # 185, 190, 180 fall between the strikes

    text = zoom.render(z, app.cfg)
    assert "🔍 <b>זום אין</b>" in text and "▶" in text and "◆" in text
    assert "היה משלם ב-<b>4/6</b> (67%)" in text
    assert "בהיסטוריה 3/6 (50%)" in text and "המדרגה היקרה היא השגויה" in text
    # Cheap next to its sibling, yet dear on its own: 67% doesn't pay for 86.8c.
    assert "❌ <b>שורה תחתונה</b>" in text and "תוחלת −20.2¢ לחוזה" in text
    assert "כמעט לא נסחרת" not in text


def test_zoom_warns_about_a_thin_reference_and_too_little_history(mk, tmp_path):
    thin = [mk("APP-T175", 175, "0.02", "0.04", volume_24h="3.24"), mk("APP-T195", 195, "0.14", "0.16")]
    app, client, _ = make_app(thin, tmp_path)
    text = zoom.render(zoom.build(client, app.cfg, "APP-T195", "no", now=1_800_000_000.0), app.cfg)
    assert "מדרגת הייחוס כמעט לא נסחרת" in text
    assert "אין מספיק אירועים מוכרעים" in text and "שורה תחתונה" not in text


def test_zoom_with_no_cheaper_contradiction_says_so(ladder, tmp_path):
    app, client, _ = make_app(ladder, tmp_path)
    text = zoom.render(zoom.build(client, app.cfg, "APP-T175", "no", now=1_800_000_000.0), app.cfg)
    assert "אין עכשיו מדרגה יקרה יותר" in text


# In the bots


def zooms(tg):
    return [t for t, _ in tg.sent if t.startswith("🔍")]


def test_every_live_buy_gets_a_zoom(ladder, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)
    app.scan_once()
    (p,) = positions(app)
    (text,) = zooms(tg)
    assert f"<b>{p['bought']} NO · Above 195</b>" in text and "▶" in text


def test_a_failed_zoom_never_stops_the_scan(ladder, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)

    def broken(*_):
        raise RuntimeError("boom")

    client.settled_markets = broken
    app.scan_once()
    assert len(positions(app)) == 1
    assert zooms(tg) == ["🔍 לא הצלחתי לעשות זום: boom"]


def test_zoom_command_on_open_positions_and_on_a_ticker(ladder, tmp_path):
    app, client, tg = auto_app(ladder, tmp_path)
    say(app, "/zoom")
    assert "אין פוזיציות פתוחות" in tg.sent[-1][0]
    app.scan_once()
    tg.sent.clear()
    say(app, "/zoom")
    assert len(zooms(tg)) == 1 and "ב-86.9¢ כולל עמלה · עלות $9.56" in zooms(tg)[0]
    say(app, "/zoom app-t175 yes")
    assert "<b>YES · Above 175</b>" in tg.sent[-1][0]
    say(app, "/zoom APP-NOPE")
    assert tg.sent[-1][0] == "🔍 השוק APP-NOPE לא נמצא."


def test_zoom_command_on_the_alert_bot(ladder, tmp_path):
    app, _, tg = make_app(ladder, tmp_path)
    say_to_alert_bot(app, "/zoom APP-T195 no")
    assert "<b>NO · Above 195</b>" in tg.sent[-1][0]
