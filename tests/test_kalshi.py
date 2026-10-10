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
