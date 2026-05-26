#!/usr/bin/env python3
"""
kalshi_snapshot.py — run this locally to get a market snapshot you can
paste into Claude for trading advice.

Setup (one time):
    pip install httpx cryptography python-dotenv

Usage:
    python snapshot.py                  # prints snapshot
    python snapshot.py | pbcopy         # macOS: copies to clipboard
    python snapshot.py | clip           # Windows: copies to clipboard
    python snapshot.py -o snapshot.txt  # saves to file

Credentials — set these as environment variables OR create a .env file
in the same directory:
    KALSHI_API_KEY_ID=your-key-id
    KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
    KALSHI_ENV=prod
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Optional: load .env if present ────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # pip install python-dotenv if you want .env support

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Config ─────────────────────────────────────────────────────────────────────
_BASE = {
    "prod": "https://trading-api.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
_PREFIX = "/trade-api/v2"


def _load_key():
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


def _sign(key, ts_ms: int, method: str, path: str) -> str:
    msg = f"{ts_ms}{method.upper()}{_PREFIX}{path}".encode()
    sig = key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def _headers(key, key_id: str, method: str, path: str) -> dict:
    ts = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": _sign(key, ts, method, path),
        "Content-Type": "application/json",
    }


def get(client, key, key_id, path, params=None):
    r = client.get(path, headers=_headers(key, key_id, "GET", path), params=params)
    r.raise_for_status()
    return r.json()


# ── Snapshot builder ───────────────────────────────────────────────────────────
def build_snapshot(market_limit: int = 40) -> str:
    env      = os.getenv("KALSHI_ENV", "prod")
    key_id   = os.getenv("KALSHI_API_KEY_ID", "")
    base_url = _BASE[env]

    if not key_id:
        sys.exit("ERROR: KALSHI_API_KEY_ID not set")

    key    = _load_key()
    client = httpx.Client(base_url=base_url, timeout=15)

    lines = [
        "# Kalshi Market Snapshot",
        f"# Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 70,
    ]

    # Balance
    try:
        bal = get(client, key, key_id, "/portfolio/balance")
        cents = bal.get("balance", 0)
        lines.append(f"\n## Balance:  ${cents/100:.2f}")
    except Exception as e:
        lines.append(f"\n## Balance:  ERROR — {e}")

    # Positions
    try:
        pos = get(client, key, key_id, "/portfolio/positions", {"limit": 50})
        positions = pos.get("market_positions", [])
        if positions:
            lines.append("\n## Open positions:")
            for p in positions:
                qty  = p.get("position", 0)
                side = "YES" if qty > 0 else "NO"
                exp  = p.get("market_exposure", 0) / 100
                lines.append(f"  {p.get('ticker','')}  {side}  qty={abs(qty)}  exposure=${exp:.2f}")
        else:
            lines.append("\n## Open positions: none")
    except Exception as e:
        lines.append(f"\n## Open positions:  ERROR — {e}")

    # Open markets
    try:
        resp    = get(client, key, key_id, "/markets", {"status": "open", "limit": market_limit})
        markets = resp.get("markets", [])
        lines.append(f"\n## Open markets  (showing {len(markets)}):")
        lines.append(f"  {'Ticker':<35} {'Yes':>5} {'No':>5} {'Vol':>8}  Closes")
        lines.append("  " + "-" * 65)
        for m in markets:
            yes   = m.get("yes_bid", m.get("last_price", "?"))
            no_p  = m.get("no_bid", "?")
            vol   = m.get("volume", 0)
            close = str(m.get("close_time", ""))[:10]
            title = m.get("title", m.get("ticker", ""))[:52]
            lines.append(f"  {m.get('ticker',''):<35} {str(yes):>4}¢ {str(no_p):>4}¢ {vol:>8,}  {close}")
            lines.append(f"    {title}")
    except Exception as e:
        lines.append(f"\n## Markets:  ERROR — {e}")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi market snapshot for Claude")
    parser.add_argument("-n", "--limit",  type=int, default=40, help="Number of markets (default 40)")
    parser.add_argument("-o", "--output", type=str, default=None, help="Save to file instead of stdout")
    args = parser.parse_args()

    snap = build_snapshot(market_limit=args.limit)

    if args.output:
        Path(args.output).write_text(snap)
        print(f"Saved to {args.output}")
    else:
        print(snap)
        print("\n" + "=" * 70)
        print("Paste the above into Claude and ask: 'What should I trade right now?'")
