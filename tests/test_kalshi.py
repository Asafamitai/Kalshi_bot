import base64
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshi_bot.kalshi import KalshiClient, KalshiError


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return self.responses.pop(0)


@pytest.mark.parametrize("kind", ["rsa", "ed25519"])
def test_signature_covers_timestamp_method_and_path(kind):
    if kind == "rsa":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        key = ed25519.Ed25519PrivateKey.generate()
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key)
    h = client.auth_headers("post", "https://example.test/trade-api/v2/portfolio/orders?x=1")
    message = (h["KALSHI-ACCESS-TIMESTAMP"] + "POST" + "/trade-api/v2/portfolio/orders").encode()
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    if kind == "rsa":
        key.public_key().verify(
            sig,
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    else:
        key.public_key().verify(sig, message)
    assert h["KALSHI-ACCESS-KEY"] == "kid"


def test_iter_open_markets_follows_cursor():
    session = FakeSession(
        [
            FakeResponse(200, {"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "c1"}),
            FakeResponse(200, {"markets": [{"ticker": "C"}], "cursor": ""}),
        ]
    )
    client = KalshiClient("https://example.test/trade-api/v2", session=session)
    assert [m["ticker"] for m in client.iter_open_markets()] == ["A", "B", "C"]
    assert session.calls[0]["params"] == {"status": "open", "limit": 1000, "mve_filter": "exclude"}
    assert session.calls[1]["params"]["cursor"] == "c1"


def test_settled_markets_reads_the_series_since_a_time_and_stops_at_the_page_limit():
    session = FakeSession([FakeResponse(200, {"markets": [{"ticker": "A"}], "cursor": "c1"}),
                           FakeResponse(200, {"markets": [{"ticker": "B"}], "cursor": "c2"})])
    client = KalshiClient("https://example.test/trade-api/v2", session=session)
    assert [m["ticker"] for m in client.settled_markets("KXAPP", 1_700_000_000.5, pages=2)] == ["A", "B"]
    assert session.calls[0]["params"] == {"series_ticker": "KXAPP", "status": "settled",
                                          "min_close_ts": 1_700_000_000, "limit": 1000}
    assert session.calls[1]["params"]["cursor"] == "c1" and len(session.calls) == 2


def test_buy_no_goes_to_v2_as_a_yes_ask():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(201, {"order_id": "o1", "fill_count": "11.00", "remaining_count": "0.00",
                                              "average_fill_price": "0.1400", "average_fee_paid": "0.0090"})])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    result = client.buy("APP-T195", "no", 11, Decimal("0.86"), "lk-1-10")
    call = session.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/trade-api/v2/portfolio/events/orders")
    assert call["json"] == {
        "ticker": "APP-T195",
        "client_order_id": "lk-1-10",
        "side": "ask",  # buying NO at 0.86 is selling YES at 0.14
        "count": "11.00",
        "price": "0.1400",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
    }
    assert "KALSHI-ACCESS-SIGNATURE" in call["headers"]
    assert result["filled"] == Decimal("11.00") and result["avg_price"] == Decimal("0.8600")
    assert not result["already"]


def test_buy_yes_is_a_bid_at_the_same_price():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(201, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "0.00"})])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    client.buy("APP-T175", "yes", 3, Decimal("0.40"), "lk-2-all")
    assert session.calls[0]["json"]["side"] == "bid"
    assert session.calls[0]["json"]["price"] == "0.4000"


def test_duplicate_client_order_id_means_already_sent():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(409, '{"error":{"code":"duplicate_client_order_id"}}')])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    assert client.buy("APP-T195", "no", 1, Decimal("0.86"), "lk-1-10")["already"] is True


def test_order_is_not_retried_on_server_error():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(503, "busy")])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    with pytest.raises(KalshiError):
        client.buy("APP-T195", "no", 1, Decimal("0.86"), "id")
    assert len(session.calls) == 1


def test_buy_without_key_refuses():
    with pytest.raises(RuntimeError):
        KalshiClient("https://example.test").buy("T", "no", 1, Decimal("0.5"), "id")


def test_get_markets_batches_tickers():
    session = FakeSession([FakeResponse(200, {"markets": [{"ticker": "A"}]}), FakeResponse(200, {"markets": []})])
    client = KalshiClient("https://example.test/trade-api/v2", session=session)
    client.get_markets([f"T{i}" for i in range(60)])
    assert [len(c["params"]["tickers"].split(",")) for c in session.calls] == [50, 10]


@pytest.mark.parametrize("side, book_side, yes_price", [("no", "bid", "0.0200"), ("yes", "ask", "0.9800")])
def test_sell_mirrors_buy(side, book_side, yes_price):
    # Selling NO at 0.98 is buying YES at 0.02; selling YES at 0.98 is an ask at 0.98.
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(201, {"order_id": "o2", "fill_count": "11.00", "average_fill_price": yes_price})])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    result = client.sell("APP-T195", side, 11, Decimal("0.98"), "auto-x")
    assert session.calls[0]["json"]["side"] == book_side and session.calls[0]["json"]["price"] == yes_price
    assert result["filled"] == Decimal("11.00") and result["avg_price"] == Decimal("0.98")


@pytest.mark.parametrize("body, yes, no", [
    ({"market_positions": [{"ticker": "APP-T195", "position_fp": "-11.00"}]}, Decimal(0), Decimal(11)),
    ({"market_positions": [{"ticker": "APP-T195", "position": 4}]}, Decimal(4), Decimal(0)),
    ({"market_positions": []}, Decimal(0), Decimal(0)),
    ({"market_positions": [{"ticker": "APP-T195"}]}, None, None),
])
def test_position_is_signed_yes_positive(body, yes, no):
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(200, body), FakeResponse(200, body)])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    assert (client.position("APP-T195", "yes"), client.position("APP-T195", "no")) == (yes, no)
    call = session.calls[0]
    assert call["url"].endswith("/portfolio/positions") and call["params"] == {"ticker": "APP-T195"}
    assert "KALSHI-ACCESS-SIGNATURE" in call["headers"]


def test_subaccount_goes_on_orders_balance_and_positions():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(201, {"order_id": "o1", "fill_count": "1.00"}),
                           FakeResponse(200, {"balance_dollars": "12.50"}),
                           FakeResponse(200, {"market_positions": []}),
                           FakeResponse(201, {"order_id": "o2", "fill_count": "0.00"})])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    client.buy("APP-T195", "no", 1, Decimal("0.86"), "auto-a", subaccount=3)
    assert client.balance(subaccount=3) == Decimal("12.50")
    client.position("APP-T195", "no", subaccount=3)
    client.buy("APP-T195", "no", 1, Decimal("0.86"), "lk-1-10")  # the primary account: no field at all
    assert session.calls[0]["json"]["subaccount"] == 3
    assert session.calls[1]["params"] == {"subaccount": 3}
    assert session.calls[2]["params"] == {"ticker": "APP-T195", "subaccount": 3}
    assert "subaccount" not in session.calls[3]["json"]


def test_create_subaccount_returns_its_number():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(201, {"subaccount_number": 4})])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    assert client.create_subaccount() == 4
    call = session.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/portfolio/subaccounts")
    assert "KALSHI-ACCESS-SIGNATURE" in call["headers"]


def test_transfer_in_cents_with_an_id():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(200, {})])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    client.transfer(0, 4, Decimal("25.50"), "auto-t1")
    call = session.calls[0]
    assert call["url"].endswith("/portfolio/subaccounts/transfer")
    assert call["json"] == {"client_transfer_id": "auto-t1", "from_subaccount": 0, "to_subaccount": 4,
                            "amount_cents": 2550, "exchange_index": 0}
    with pytest.raises(ValueError):
        client.transfer(0, 4, Decimal("0.005"), "auto-t2")


class Limited(FakeResponse):
    def __init__(self, retry_after=None):
        super().__init__(429, '{"error":{"code":"too_many_requests"}}')
        self.headers = {"Retry-After": retry_after} if retry_after else {}


def test_rate_limit_waits_longer_before_giving_up(monkeypatch):
    # Seven 429s in a row, then the page: a burst no longer fails the scan after 15 seconds.
    waits = []
    monkeypatch.setattr("kalshi_bot.kalshi.time.sleep", waits.append)
    session = FakeSession([Limited() for _ in range(7)] + [FakeResponse(200, {"markets": [{"ticker": "A"}]})])
    client = KalshiClient("https://example.test/trade-api/v2", session=session)
    assert [m["ticker"] for m in client.iter_open_markets()] == ["A"]
    assert waits == [1, 2, 4, 8, 16, 30, 30]


def test_rate_limit_honours_retry_after_and_still_gives_up(monkeypatch):
    waits = []
    monkeypatch.setattr("kalshi_bot.kalshi.time.sleep", waits.append)
    session = FakeSession([Limited("3")] + [Limited() for _ in range(7)])
    client = KalshiClient("https://example.test/trade-api/v2", session=session)
    with pytest.raises(KalshiError) as e:
        client.get_markets(["A"])
    assert e.value.status == 429 and len(session.calls) == 8 and waits[0] == 3.0


def test_pages_are_read_a_little_apart(monkeypatch):
    waits = []
    monkeypatch.setattr("kalshi_bot.kalshi.time.sleep", waits.append)
    session = FakeSession([FakeResponse(200, {"markets": [{"ticker": "A"}], "cursor": "c1"}),
                           FakeResponse(200, {"markets": [{"ticker": "B"}], "cursor": ""})])
    client = KalshiClient("https://example.test/trade-api/v2", session=session)
    assert [m["ticker"] for m in client.iter_open_markets()] == ["A", "B"]
    assert waits == [0.1]


def test_candlesticks_go_to_the_series_path():
    session = FakeSession([FakeResponse(200, {"candlesticks": [{"end_period_ts": 1}], "ticker": "KXW-26-T63"})])
    client = KalshiClient("https://example.test/trade-api/v2", session=session)
    assert client.candlesticks("KXW-26-T63", 100.5, 200.9) == [{"end_period_ts": 1}]
    call = session.calls[0]
    assert call["url"].endswith("/series/KXW/markets/KXW-26-T63/candlesticks")
    assert call["params"] == {"start_ts": 100, "end_ts": 200, "period_interval": 60}
