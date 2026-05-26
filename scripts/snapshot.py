"""Kalshi snapshot — RSA-PSS auth. Prices via trades feed + market _dollars fields."""
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

def cents(dollars_str):
    """Convert '0.2700' -> 27  (returns None if missing/invalid)"""
    try:
        return int(round(float(dollars_str) * 100))
    except (TypeError, ValueError):
        return None

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
    positions = pos.get("market_positions", []) or []
    lines.append("\n## Open positions:")
    if positions:
        for p in positions:
            qty = p.get("position", 0)
            lines.append(f"  {p.get('ticker','')}  {'YES' if qty>0 else 'NO'}  qty={abs(qty)}")
    else:
        lines.append("  none")

# ---- Trades feed: find recently active tickers + last traded price ----
trades_resp = get("/markets/trades", {"limit": 200})
trades = (trades_resp or {}).get("trades", []) or []

trade_price  = {}  # ticker -> last yes_price in cents
trade_count  = {}  # ticker -> # of recent trades
ordered      = []  # tickers in recency order
seen         = set()

for t in trades:
    tk = t.get("ticker", "")
    if not tk:
        continue
    if tk not in seen:
        seen.add(tk)
        ordered.append(tk)
    p = cents(t.get("yes_price_dollars"))
    if p is not None:
        trade_price[tk] = p
    trade_count[tk] = trade_count.get(tk, 0) + 1

# Sort by most recent (already in recency order from API) then by count
ordered.sort(key=lambda tk: trade_count.get(tk, 0), reverse=True)

print(f"  Trades feed: {len(ordered)} unique tickers", file=sys.stderr)

# ---- Fetch market details for top 40 ----
markets = []
for ticker in ordered[:40]:
    resp = get(f"/markets/{ticker}")
    if not resp:
        continue
    m = resp.get("market", resp) if isinstance(resp, dict) else resp
    if not isinstance(m, dict):
        continue
    m["_trade_price"] = trade_price.get(ticker)
    m["_trade_count"] = trade_count.get(ticker, 0)
    markets.append(m)

markets.sort(key=lambda m: m.get("_trade_count", 0), reverse=True)

# ---- Output ----
lines.append(f"\n## Active markets — {len(markets)} recently traded (sorted by activity):")
lines.append(f"  {'Ticker':<42} {'YES':>4} {'NO':>4} {'OI':>8}  Trades  Closes    Title")
lines.append("  " + "-"*120)

for m in markets:
    ticker = m.get("ticker", "")[:42]
    title  = m.get("title", "")[:52]
    close  = str(m.get("close_time", ""))[:10]
    oi     = m.get("open_interest_fp") or "0"
    try:
        oi_str = f"{float(oi):>8,.0f}"
    except (TypeError, ValueError):
        oi_str = "       ?"

    # YES price: live bid preferred, then last trade, then trade feed price
    yes_c = (
        cents(m.get("yes_bid_dollars")) or
        cents(m.get("last_price_dollars")) or
        m.get("_trade_price")
    )
    no_c = cents(m.get("no_bid_dollars"))
    trades_n = m.get("_trade_count", 0)

    yes_str = f"{yes_c:>3}¢" if yes_c is not None else "  ??"
    no_str  = f"{no_c:>3}¢" if no_c  is not None else "  ??"

    lines.append(f"  {ticker:<42} {yes_str} {no_str} {oi_str}  {trades_n:>6}  {close}  {title}")

if not markets:
    lines.append("  No active markets found.")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
