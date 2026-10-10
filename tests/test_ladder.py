from decimal import Decimal

from kalshi_bot.ladder import build_ladders, find_violations

D = Decimal
MIN = D("0.02")


def test_real_example_safer_no_is_cheaper(mk):
    # NO on "above 195" offered at 0.86 while NO on "above 175" sits at 0.96 / 0.98.
    markets = [
        mk("APP-T175", 175, "0.02", "0.04"),
        mk("APP-T185", 185, "0.05", "0.07"),
        mk("APP-T195", 195, "0.14", "0.16"),
    ]
    found = [v for v in find_violations(markets, MIN) if v.side == "no" and v.safer.ticker == "APP-T195"]
    assert len(found) == 1
    v = found[0]
    assert v.ask == D("0.86")
    assert v.dominated.ticker == "APP-T175"
    # Measured against the dominated mid (0.97), not its ask (0.98).
    assert v.dominated_mid == D("0.97")
    assert v.saving == D("0.11")
    assert v.bid_through  # 0.86 < the 175 NO bid of 0.96
    assert v.buyable


def test_consistent_ladder_has_no_violations(mk):
    markets = [
        mk("A-T100", 100, "0.80", "0.82"),
        mk("A-T105", 105, "0.60", "0.62"),
        mk("A-T110", 110, "0.30", "0.33"),
    ]
    assert find_violations(markets, MIN) == []


def test_wide_dominated_market_is_not_a_saving(mk):
    # NO ask inverts (0.90 at 110 vs 0.97 at 100), but the 100 rung is wide:
    # NO bid 0.70 / ask 0.97, mid 0.835, which is below 0.90. Nobody pays 0.97.
    markets = [
        mk("A-T100", 100, "0.03", "0.30", yes_ask_size="5.00"),
        mk("A-T110", 110, "0.10", "0.12"),
    ]
    assert [v for v in find_violations(markets, MIN) if v.side == "no"] == []


def test_yes_side_violation(mk):
    # YES on "above 100" must cost at least as much as YES on "above 110".
    markets = [
        mk("A-T100", 100, "0.38", "0.40"),
        mk("A-T110", 110, "0.50", "0.52"),
    ]
    yes = [v for v in find_violations(markets, MIN) if v.side == "yes"]
    assert len(yes) == 1
    v = yes[0]
    assert (v.safer.ticker, v.dominated.ticker) == ("A-T100", "A-T110")
    assert v.ask == D("0.40") and v.saving == D("0.11")


def test_below_ladder_is_mirrored(mk):
    # YES on "below 110" pays whenever YES on "below 100" does: it is the safer YES.
    markets = [
        mk("B-T100", 100, "0.40", "0.42", strike_type="less"),
        mk("B-T110", 110, "0.20", "0.22", strike_type="less"),
    ]
    yes = [v for v in find_violations(markets, MIN) if v.side == "yes"]
    assert [(v.safer.ticker, v.dominated.ticker) for v in yes] == [("B-T110", "B-T100")]
    assert yes[0].saving == D("0.19")


def test_empty_books_are_flagged_not_buyable(mk):
    # 195 has a NO ask (YES bid 0.14) but no NO bid behind it (YES ask 1.00).
    markets = [
        mk("APP-T175", 175, "0.02", "0.04"),
        mk("APP-T195", 195, "0.14", "1.00"),
    ]
    (v,) = [v for v in find_violations(markets, MIN) if v.side == "no"]
    assert v.empty_books == ["APP-T195"]
    assert not v.buyable


def test_one_dollar_ask_is_empty(mk):
    markets = [mk("A-T100", 100, "0.00", "0.02"), mk("A-T110", 110, "0.01", "0.03")]
    ladder = build_ladders(markets)[0]
    assert ladder[0].is_empty("no")  # NO ask 1.00: nobody is selling NO


def test_prefers_real_dominated_book_over_bigger_empty_one(mk):
    markets = [
        mk("A-T100", 100, "0.00", "0.10"),  # empty: NO 0.90 / 1.00, "mid" 0.95
        mk("A-T105", 105, "0.06", "0.08"),  # real: NO 0.92 / 0.94, mid 0.93
        mk("A-T110", 110, "0.12", "0.14"),  # NO ask 0.88
    ]
    (v,) = [v for v in find_violations(markets, MIN) if v.side == "no" and v.safer.ticker == "A-T110"]
    # 7c against the empty book loses to 5c against a real one.
    assert v.dominated.ticker == "A-T105"
    assert v.saving == D("0.05")
    assert v.buyable


def test_ladders_split_by_event_type_close_and_rules(mk):
    markets = [
        mk("A-T100", 100, "0.02", "0.04"),
        mk("A-T110", 110, "0.14", "0.16", event="OTHER"),
        mk("A-T120", 120, "0.14", "0.16", strike_type="greater_or_equal"),
        mk("A-T130", 130, "0.14", "0.16", close_time="2026-11-30T23:59:00Z"),
    ]
    other_subject = mk("A-T140", 140, "0.14", "0.16")
    other_subject["rules_primary"] = "If the rival app has above 140 downloads, Yes."
    assert build_ladders(markets + [other_subject]) == []


def test_between_and_unstruck_markets_are_ignored(mk):
    between = mk("A-B100", 100, "0.50", "0.52")
    between["strike_type"] = "between"
    no_strike = mk("A-T110", 110, "0.14", "0.16")
    del no_strike["floor_strike"]
    assert build_ladders([between, no_strike, mk("A-T120", 120, "0.1", "0.2")]) == []


def test_min_saving_threshold(mk):
    markets = [mk("A-T100", 100, "0.10", "0.12"), mk("A-T110", 110, "0.12", "0.14")]
    # NO@110 ask 0.88 vs NO@100 mid 0.89: a 1c contradiction.
    assert [v.saving for v in find_violations(markets, D("0.01")) if v.side == "no"] == [D("0.01")]
    assert find_violations(markets, D("0.02")) == []
