"""Kalshi snapshot — uses trades feed for prices + active market list."""
import base64, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL   = "https://api.elections.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"

KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
if not KEY_ID:
    sys.exit("ERROR: KALSHI_API_KEY_ID not set")

key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
if key_path:
    pem = Path(key_path).read_bytes()
else:
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    pem = base64.b64decode("".join(raw.split()))

PRIV_KEY = serialization.load_pem_private_key(pem, password=None)

def auth_headers(method, path):
    ts  = str(int(time.time() * 1000))
    msg = (ts + method.upper() + API_PREFIX + path).encode()
    sig = base64.b64encode(
        PRIV_KEY.sign(msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
    ).decode()
    return {"KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig}

def get(path, params=None):
    r = httpx.get(f"{BASE_URL}{path}", headers=auth_headers("GET", path),
                  params=params, timeout=20)
    print(f"GET {path} -> {r.status_code}", file=sys.stderr)
    if not r.is_success:
        print(f"  {r.text[:300]}", file=sys.stderr)
        return None
    return r.json()

ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines  = [f"# Kalshi Market Snapshot\n# Generated: {ts_str}\n" + "="*70]

# Balance
bal = get("/portfolio/balance")
if bal:
    lines.append(f"\n## Balance: ${bal.get('balance', 0)/100:.2f}")
else:
    lines.append("\n## Balance: unavailable")

# Positions
pos = get("/portfolio/positions", {"limit": 50})
if pos:
    positions = pos.get("market_positions", [])
    lines.append("\n## Open positions:")
    if positions:
        for p in positions:
            qty = p.get("position", 0)
            lines.append(f"  {p.get('ticker','')}  {'YES' if qty>0 else 'NO'}  qty={abs(qty)}")
    else:
        lines.append("  none")

# ---- Step 1: Get recent trades (includes yes_price per trade) ----
trades_resp = get("/markets/trades", {"limit": 200})

# Build price map and trade-count map from trades
last_price   = {}  # ticker -> last yes_price traded
trade_counts = {}  # ticker -> number of recent trades
ordered_tickers = []
seen = set()

if trades_resp:
    for t in trades_resp.get("trades", []):
        tk    = t.get("ticker", "")
        price = t.get("yes_price")
        count = t.get("count", 1)
        if not tk:
            continue
        if tk not in seen:
            seen.add(tk)
            ordered_tickers.append(tk)
        if price is not None:
            last_price[tk] = price  # keeps most-recent (first in list)
        trade_counts[tk] = trade_counts.get(tk, 0) + (count or 1)

print(f"  Trades feed: {len(ordered_tickers)} unique tickers", file=sys.stderr)

# Sort tickers by trade activity (most active first)
ordered_tickers.sort(key=lambda tk: trade_counts.get(tk, 0), reverse=True)

# ---- Step 2: Fetch market details for top 40 active tickers ----
markets = []
for ticker in ordered_tickers[:40]:
    resp = get(f"/markets/{ticker}")
    if not resp:
        continue
    # API wraps in {"market": {...}} or returns market directly
    m = resp.get("market", resp) if isinstance(resp, dict) else resp
    if not isinstance(m, dict):
        continue
    # Inject trade data we already know
    m["_trade_price"]  = last_price.get(ticker)
    m["_trade_count"] = trade_counts.get(ticker, 0)
    markets.append(m)

# Sort by trade activity
markets.sort(key=lambda m: m.get("_trade_count", 0), reverse=True)

# ---- Step 3: Build snapshot output ----
lines.append(f"\n## Active markets — {len(markets)} recently traded:")
lines.append(f"  {'Ticker':<40} {'Price':>6} {'Vol':>8}  Trades  Closes    Title")
lines.append("  " + "-"*110)

for m in markets:
    ticker = m.get("ticker", "")[:40]
    title  = m.get("title", "")[:55]
    close  = str(m.get("close_time", ""))[:10]
    vol    = m.get("volume") or 0
    trades = m.get("_trade_count", 0)

    # Best price: live bid first, then last trade from feed
    raw_price = (
        m.get("yes_bid") or
        m.get("last_price") or
        m.get("yes_ask") or
        m.get("_trade_price")
    )
    price_str = f"{raw_price}c" if raw_price is not None else "  ?c"

    lines.append(f"  {ticker:<40} {price_str:>6} {vol:>8,}  {trades:>6}  {close}  {title}")

if not markets:
    lines.append("  No active markets found.")
    if trades_resp:
        lines.append("\n  Raw trades sample:")
        for t in (trades_resp.get("trades") or [])[:10]:
            lines.append(f"    {t}")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
