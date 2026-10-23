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
# A full scan reads over a hundred pages, and a second bot may be scanning
# too: Kalshi's rate limit answers 429 to a burst. Retries wait 1, 2, 4, ...
# up to 30 seconds (about a minute and a half in all), or as long as its
# Retry-After says, and pages are read a little apart.
_ATTEMPTS = 8
_PAGE_PAUSE = 0.5


def _retry_wait(resp, attempt: int) -> float:
    """Kalshi's Retry-After, if it sent one; else 1, 2, 4, ... up to 30 seconds."""
    try:
        return min(float((getattr(resp, "headers", None) or {}).get("Retry-After")), 60.0)
    except (TypeError, ValueError):
        return float(min(2**attempt, 30))


def _sub(fields: dict, subaccount: int) -> dict:
    """Point a request at a numbered subaccount (1-63); 0, the primary account, is Kalshi's default."""
    if subaccount:
        fields["subaccount"] = subaccount
    return fields


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
        for attempt in range(_ATTEMPTS):
            headers = self.auth_headers(method, url) if signed else {}
            resp = self.session.request(
                method, url, params=params, json=json, headers=headers, timeout=self.timeout
            )
            if resp.status_code in retry_on and attempt < _ATTEMPTS - 1:
                time.sleep(_retry_wait(resp, attempt))
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
            time.sleep(_PAGE_PAUSE)

    def get_markets(self, tickers: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(tickers), 50):
            data = self._request("GET", "/markets", params={"tickers": ",".join(tickers[i : i + 50])})
            out.extend(data.get("markets") or [])
        return out

    def event_markets(self, event_ticker: str) -> list[dict]:
        """Every market of one event: its whole ladder."""
        data = self._request("GET", "/markets", params={"event_ticker": event_ticker, "limit": 1000})
        return data.get("markets") or []

    def settled_markets(self, series_ticker: str, since: float, pages: int = 5) -> list[dict]:
        """The series' markets settled since `since` (epoch seconds), at most `pages` pages of 1000."""
        out, cursor = [], None
        for _ in range(pages):
            params = {"series_ticker": series_ticker, "status": "settled", "min_close_ts": int(since), "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/markets", params=params)
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def balance(self, subaccount: int = 0) -> Decimal | None:
        """Cash available, in dollars; in a numbered subaccount (1-63) if one is given."""
        data = self._request("GET", "/portfolio/balance", params=_sub({}, subaccount), signed=True)
        value = data.get("balance_dollars")
        return Decimal(str(value)) if value is not None else None

    def create_subaccount(self) -> int:
        """A new numbered subaccount (Kalshi numbers them 1-63 and keeps no names); its number."""
        data = self._request("POST", "/portfolio/subaccounts", json={"exchange_index": 0}, signed=True)
        n = data.get("subaccount_number", data.get("subaccount"))
        if n is None:
            raise KalshiError(200, f"no subaccount number in Kalshi's answer: {data}")
        return int(n)

    def transfer(self, from_subaccount: int, to_subaccount: int, dollars: Decimal, transfer_id: str) -> dict:
        """Move cash between two subaccounts (0 is the primary account).

        Kalshi applies one `transfer_id` (a bare UUID; anything else is a 400)
        once: a repeat answers 409, so the 429 retry in _request can't move the
        money twice.
        """
        cents = dollars * 100
        if cents != cents.to_integral_value() or cents <= 0:
            raise ValueError(f"not a positive amount in whole cents: {dollars}")
        body = {"client_transfer_id": transfer_id, "from_subaccount": from_subaccount,
                "to_subaccount": to_subaccount, "amount_cents": int(cents), "exchange_index": 0}
        return self._request("POST", "/portfolio/subaccounts/transfer", json=body, signed=True)

    def position(self, ticker: str, side: str, subaccount: int = 0) -> Decimal | None:
        """Contracts held on `side` of `ticker`; None if Kalshi's answer can't be read.

        Kalshi keeps one signed position per market: YES positive, NO negative.
        """
        data = self._request("GET", "/portfolio/positions", params=_sub({"ticker": ticker}, subaccount), signed=True)
        rows = [r for r in data.get("market_positions") or [] if r.get("ticker") == ticker]
        if not rows:
            return Decimal(0)
        value = rows[0].get("position_fp", rows[0].get("position"))
        if value is None:
            return None
        signed = Decimal(str(value))
        held = signed if side == "yes" else -signed
        return max(held, Decimal(0))

    def buy(self, ticker: str, side: str, count: int, limit_price: Decimal, client_order_id: str,
            subaccount: int = 0) -> dict:
        """Buy up to `count` at `limit_price` or better; whatever doesn't fill at once is cancelled.

        Goes through the V2 endpoint (the legacy POST /portfolio/orders answers
        410 since 2026-08-24). V2 quotes only the YES book: buying YES is a bid
        at p, buying NO at p is an ask (selling YES) at 1 - p.
        """
        return self._order(ticker, side, "buy", count, limit_price, client_order_id, subaccount)

    def sell(self, ticker: str, side: str, count: int, limit_price: Decimal, client_order_id: str,
             subaccount: int = 0) -> dict:
        """Sell up to `count` held on `side` at `limit_price` or better, immediate or cancel.

        The mirror of buy: selling YES at p is an ask at p, selling NO at p is
        a bid (buying YES) at 1 - p, which Kalshi nets against the NO held.
        Check `position` first: selling more than is held opens the other side.
        """
        return self._order(ticker, side, "sell", count, limit_price, client_order_id, subaccount)

    def _order(self, ticker: str, side: str, action: str, count: int, limit_price: Decimal,
               client_order_id: str, subaccount: int = 0) -> dict:
        if not self.can_trade:
            raise RuntimeError("Kalshi API key is not configured")
        yes_price = limit_price if side == "yes" else 1 - limit_price
        book_side = "bid" if (side == "yes") == (action == "buy") else "ask"
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": f"{count:.2f}",
            "price": str(yes_price.quantize(Decimal("0.0001"))),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        _sub(body, subaccount)
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
