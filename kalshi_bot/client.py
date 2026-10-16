"""Minimal Kalshi Trade API v2 client (market data + orders)."""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any, Iterator
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiError(RuntimeError):
    pass


class KalshiClient:
    def __init__(
        self,
        base_url: str = PROD_BASE,
        key_id: str | None = None,
        private_key_path: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self._key = None
        if private_key_path:
            with open(private_key_path, "rb") as f:
                self._key = serialization.load_pem_private_key(f.read(), password=None)

    @property
    def authenticated(self) -> bool:
        return bool(self.key_id and self._key)

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.authenticated:
            raise KalshiError("This endpoint needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        ts = str(int(time.time() * 1000))
        # Signed message is timestamp + METHOD + full URL path (no query string).
        msg = (ts + method.upper() + urlparse(self.base_url + path).path).encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _request(self, method: str, path: str, *, params=None, json=None, auth=False) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            headers = self._auth_headers(method, path) if auth else {}
            try:
                resp = self.session.request(
                    method, self.base_url + path, params=params, json=json,
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException:
                if attempt == self.max_retries:
                    raise
                time.sleep(2 ** attempt)
                continue
            # Never retry order placement on 5xx: the order may have gone through.
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and method == "GET")
            if retryable and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise KalshiError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json() if resp.content else {}
        raise KalshiError(f"{method} {path}: retries exhausted")

    # ---- market data (public) ----

    def iter_markets(self, **params) -> Iterator[dict]:
        params = {"limit": 1000, **{k: v for k, v in params.items() if v is not None}}
        while True:
            data = self._request("GET", "/markets", params=params)
            yield from data.get("markets", [])
            cursor = data.get("cursor")
            if not cursor or not data.get("markets"):
                return
            params["cursor"] = cursor

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")["market"]

    def get_event(self, event_ticker: str) -> dict:
        return self._request("GET", f"/events/{event_ticker}")["event"]

    def get_candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                         period_minutes: int = 1) -> list[dict]:
        data = self._request(
            "GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_minutes},
        )
        return data.get("candlesticks", [])

    def get_event_candlesticks(self, series_ticker: str, event_ticker: str, start_ts: int, end_ts: int,
                               period_minutes: int = 1) -> dict:
        """Candles for every market in an event: {market_tickers, market_candlesticks, adjusted_end_ts}."""
        return self._request(
            "GET", f"/series/{series_ticker}/events/{event_ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_minutes},
        )

    # ---- portfolio (authenticated) ----

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance", auth=True)

    def get_positions(self) -> list[dict]:
        return self._request("GET", "/portfolio/positions", auth=True).get("market_positions", [])

    def buy_ioc(self, ticker: str, side: str, count: int, price: float) -> dict:
        """Limit buy that fills what it can at `price` or better, then cancels the rest."""
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "action": "buy",
            "type": "limit",
            "count": count,
            f"{side}_price_dollars": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel",
        }
        return self._request("POST", "/portfolio/orders", json=body, auth=True)["order"]
