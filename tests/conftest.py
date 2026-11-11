from decimal import Decimal

import pytest


def market(
    ticker,
    strike,
    yes_bid,
    yes_ask,
    *,
    event="KXAPP-26OCT",
    strike_type="greater",
    yes_bid_size="100.00",
    yes_ask_size="100.00",
    volume_24h="500.00",
    status="active",
    close_time="2030-10-31T23:59:00Z",  # after the tests' clock, so no market has run out of time
):
    """A market dict shaped like Kalshi's GET /markets response (dollar strings)."""
    yes_bid, yes_ask = Decimal(yes_bid), Decimal(yes_ask)
    word = "above" if strike_type.startswith("greater") else "below"
    m = {
        "ticker": ticker,
        "event_ticker": event,
        "strike_type": strike_type,
        "title": f"App downloads {word} {strike}?",
        "yes_sub_title": f"{word.capitalize()} {strike}",
        "status": status,
        "close_time": close_time,
        "rules_primary": f"If the app has {word} {strike} downloads by Oct 31, 2026, the market resolves to Yes.",
        "yes_bid_dollars": f"{yes_bid:.4f}",
        "yes_ask_dollars": f"{yes_ask:.4f}",
        "no_bid_dollars": f"{1 - yes_ask:.4f}",
        "no_ask_dollars": f"{1 - yes_bid:.4f}",
        "yes_bid_size_fp": yes_bid_size,
        "yes_ask_size_fp": yes_ask_size,
        "volume_24h_fp": volume_24h,
    }
    m["floor_strike" if strike_type.startswith("greater") else "cap_strike"] = strike
    return m


@pytest.fixture
def mk():
    return market


@pytest.fixture(autouse=True)
def fresh_underlying_cache():
    """The underlying's history cache lives for the process; each test starts without it."""
    from kalshi_bot import underlying
    underlying._cache.clear()
    yield
    underlying._cache.clear()
