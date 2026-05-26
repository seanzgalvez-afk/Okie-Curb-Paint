"""
Kalshi REST API client with RSA-256 authentication.

Kalshi API docs: https://trading-api.kalshi.com/trade-api/v2/swagger.json
Auth reference:  https://trading-api.kalshi.com/docs
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

# ── Base URLs ──────────────────────────────────────────────────────────────────
_BASE = {
    "prod": "https://trading-api.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
_PREFIX = {
    "prod": "/trade-api/v2",
    "demo": "/trade-api/v2",
}


def _load_private_key():
    """Load RSA private key from env var or file path."""
    raw = os.getenv("KALSHI_PRIVATE_KEY")
    if raw:
        raw = raw.strip()
        # Accept base64-encoded single-line OR raw PEM
        if not raw.startswith("-----"):
            import base64 as _b64
            raw = _b64.b64decode(raw).decode()
        pem = raw.replace("\\n", "\n").encode()
    else:
        path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
        pem = Path(path).expanduser().read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def _sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    """
    Build the Kalshi request signature.
    Message = str(timestamp_ms) + method.upper() + path  (no query string)
    """
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


class KalshiClient:
    """Thin synchronous wrapper around the Kalshi v2 REST API."""

    def __init__(self, env: str | None = None):
        env = env or os.getenv("KALSHI_ENV", "demo")
        self.base_url = _BASE[env]
        self._api_prefix = _PREFIX[env]
        self.api_key_id = os.getenv("KALSHI_API_KEY_ID", "")
        self._private_key = _load_private_key()
        self._http = httpx.Client(base_url=self.base_url, timeout=15)

    # ── Auth ───────────────────────────────────────────────────────────────────
    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        ts = int(time.time() * 1000)
        # Kalshi expects the full path including the /trade-api/v2 prefix
        full_path = self._api_prefix + path
        sig = _sign(self._private_key, ts, method, full_path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        headers = self._auth_headers("GET", path)
        r = self._http.get(path, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> Any:
        headers = self._auth_headers("POST", path)
        r = self._http.post(path, headers=headers, json=body)
        r.raise_for_status()
        return r.json()

    # ── Markets ────────────────────────────────────────────────────────────────
    def get_markets(
        self,
        status: str = "open",
        limit: int = 100,
        cursor: str | None = None,
        **kwargs,
    ) -> dict:
        """List markets. status: 'open' | 'closed' | 'settled'"""
        params = {"status": status, "limit": limit, **kwargs}
        if cursor:
            params["cursor"] = cursor
        return self._get("/markets", params)

    def get_market(self, ticker: str) -> dict:
        """Get a single market by ticker."""
        return self._get(f"/markets/{ticker}")

    def get_market_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Current order book for a market."""
        return self._get(f"/markets/{ticker}/orderbook", {"depth": depth})

    def get_market_history(
        self,
        ticker: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int = 100,
    ) -> dict:
        """Price history (candlestick-style) for a market."""
        params: dict = {"limit": limit}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        return self._get(f"/markets/{ticker}/history", params)

    def get_trades(self, ticker: str | None = None, limit: int = 50) -> dict:
        """Recent trades, optionally filtered to a single market."""
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("/markets/trades", params)

    # ── Events ─────────────────────────────────────────────────────────────────
    def get_events(self, status: str = "open", limit: int = 100) -> dict:
        return self._get("/events", {"status": status, "limit": limit})

    def get_event(self, event_ticker: str) -> dict:
        return self._get(f"/events/{event_ticker}")

    # ── Portfolio ──────────────────────────────────────────────────────────────
    def get_balance(self) -> dict:
        return self._get("/portfolio/balance")

    def get_positions(self, limit: int = 100) -> dict:
        return self._get("/portfolio/positions", {"limit": limit})

    def get_fills(self, limit: int = 50) -> dict:
        return self._get("/portfolio/fills", {"limit": limit})

    def get_orders(self, status: str = "resting", limit: int = 50) -> dict:
        return self._get("/portfolio/orders", {"status": status, "limit": limit})

    # ── Exchange info ──────────────────────────────────────────────────────────
    def get_exchange_status(self) -> dict:
        return self._get("/exchange/status")
