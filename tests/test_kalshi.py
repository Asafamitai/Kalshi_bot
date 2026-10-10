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


def test_buy_ioc_body_and_signed():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(201, {"order": {"fill_count_fp": "11.00"}})])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    order = client.buy_ioc("APP-T195", "no", 11, Decimal("0.86"), "ladder-1-x")
    assert order == {"fill_count_fp": "11.00"}
    call = session.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/portfolio/orders")
    assert call["json"] == {
        "ticker": "APP-T195",
        "action": "buy",
        "side": "no",
        "count": 11,
        "no_price_dollars": "0.8600",
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "ladder-1-x",
    }
    assert "KALSHI-ACCESS-SIGNATURE" in call["headers"]


def test_order_is_not_retried_on_server_error():
    key = ed25519.Ed25519PrivateKey.generate()
    session = FakeSession([FakeResponse(503, "busy")])
    client = KalshiClient("https://example.test/trade-api/v2", key_id="kid", private_key=key, session=session)
    with pytest.raises(KalshiError):
        client.buy_ioc("APP-T195", "no", 1, Decimal("0.86"), "id")
    assert len(session.calls) == 1


def test_buy_without_key_refuses():
    with pytest.raises(RuntimeError):
        KalshiClient("https://example.test").buy_ioc("T", "no", 1, Decimal("0.5"), "id")
