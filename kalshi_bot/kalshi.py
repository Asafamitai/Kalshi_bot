"""Minimal Kalshi Trade API v2 client: public market reads plus signed orders."""

from __future__ import annotations

import base64
import time
from decimal import Decimal
from typing import Iterator
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
ORDERS_PATH = "/portfolio/events/orders"

_RETRY_STATUS = {429, 500, 502, 503, 504}


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Kalshi API {status}: {body[:300]}")
        self.status = status
        self.body = body


def load_private_key(pem: str | bytes):
    if isinstance(pem, str):
        pem = pem.encode()
    return serialization.load_pem_private_key(pem, password=None)


class KalshiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        key_id: str | None = None,
        private_key=None,
        timeout: float = 20,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.private_key = private_key
        self.timeout = timeout
        self.session = session or requests.Session()

    @property
    def can_trade(self) -> bool:
        return bool(self.key_id and self.private_key)

    def auth_headers(self, method: str, url: str) -> dict:
        """Sign timestamp + METHOD + path (no query string), per Kalshi's API-key scheme."""
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + urlparse(url).path).encode()
        if isinstance(self.private_key, Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            signature = self.private_key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _request(self, method: str, path: str, *, params=None, json=None, signed=False) -> dict:
        url = self.base_url + path
        # Orders are only retried on 429: a 5xx may have been accepted already.
        retry_on = _RETRY_STATUS if method == "GET" else {429}
        for attempt in range(5):
            headers = self.auth_headers(method, url) if signed else {}
            resp = self.session.request(
                method, url, params=params, json=json, headers=headers, timeout=self.timeout
            )
            if resp.status_code in retry_on and attempt < 4:
                time.sleep(min(2**attempt, 10))
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text)
            return resp.json()
        raise AssertionError("unreachable")

    def iter_open_markets(self) -> Iterator[dict]:
        """Every open market, excluding multivariate combos, page by page."""
        cursor = None
        while True:
            params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/markets", params=params)
            yield from data.get("markets") or []
            cursor = data.get("cursor")
            if not cursor:
                return

    def get_markets(self, tickers: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(tickers), 50):
            data = self._request("GET", "/markets", params={"tickers": ",".join(tickers[i : i + 50])})
            out.extend(data.get("markets") or [])
        return out

    def balance(self) -> Decimal | None:
        """Cash available, in dollars."""
        data = self._request("GET", "/portfolio/balance", signed=True)
        value = data.get("balance_dollars")
        return Decimal(str(value)) if value is not None else None

    def buy(self, ticker: str, side: str, count: int, limit_price: Decimal, client_order_id: str) -> dict:
        """Buy up to `count` at `limit_price` or better; whatever doesn't fill at once is cancelled.

        Goes through the V2 endpoint (the legacy POST /portfolio/orders answers
        410 since 2026-08-24). V2 quotes only the YES book: buying YES is a bid
        at p, buying NO at p is an ask (selling YES) at 1 - p.
        """
        if not self.can_trade:
            raise RuntimeError("Kalshi API key is not configured")
        book_side, yes_price = ("bid", limit_price) if side == "yes" else ("ask", 1 - limit_price)
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": f"{count:.2f}",
            "price": str(yes_price.quantize(Decimal("0.0001"))),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        try:
            data = self._request("POST", ORDERS_PATH, json=body, signed=True)
        except KalshiError as e:
            text = e.body.lower()
            if e.status == 409 or "duplicate" in text or "already exists" in text:
                # Same client_order_id as an earlier attempt: that one went out.
                return {"already": True, "filled": Decimal(0)}
            raise
        avg_yes = data.get("average_fill_price")
        avg_price = None
        if avg_yes is not None:
            avg_price = Decimal(str(avg_yes)) if side == "yes" else 1 - Decimal(str(avg_yes))
        return {
            "already": False,
            "order_id": data.get("order_id"),
            "filled": Decimal(str(data.get("fill_count") or 0)),
            "avg_price": avg_price,
            "avg_fee": Decimal(str(data["average_fee_paid"])) if data.get("average_fee_paid") else None,
        }
