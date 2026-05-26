"""Kalshi snapshot — finds active markets via the trades feed."""
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

# --- Find active markets via recent trades ---
# This bypasses the pagination problem (sports parlays dominate page 1-N)
trades_resp = get("/markets/trades", {"limit": 100})
active_tickers = []
seen = set()
if trades_resp:
    for t in trades_resp.get("trades", []):
        tk = t.get("ticker", "")
        if tk and tk not in seen:
            seen.add(tk)
            active_tickers.append(tk)

print(f"  Found {len(active_tickers)} recently-traded tickers", file=sys.stderr)

# Fetch full market details for each active ticker
markets = []
for ticker in active_tickers[:40]:
    resp = get(f"/markets/{ticker}")
    if resp:
        m = resp.get("market", resp)  # some endpoints wrap in 'market'
        if isinstance(m, dict):
            markets.append(m)

# Sort by volume descending
markets.sort(key=lambda m: m.get("volume", 0), reverse=True)

lines.append(f"\n## Active markets (from recent trades feed, {len(markets)} markets):")
if markets:
    lines.append(f"  {'Ticker':<38} {'Yes':>4} {'No':>4} {'Volume':>10}  Closes")
    lines.append("  " + "-"*75)
    for m in markets:
        yes_p = m.get("yes_bid", m.get("last_price", "?"))
        no_p  = m.get("no_bid", "?")
        vol   = m.get("volume", 0)
        close = str(m.get("close_time", ""))[:10]
        ticker = m.get("ticker", "")[:38]
        title  = m.get("title", "")[:70]
        lines.append(f"  {ticker:<38} {str(yes_p):>3}c {str(no_p):>3}c {vol:>10,}  {close}")
        lines.append(f"    {title}")
else:
    # Fallback: show raw trades if market fetch failed
    lines.append("  Could not fetch market details. Raw recent trades:")
    if trades_resp:
        for t in (trades_resp.get("trades", []) or [])[:20]:
            lines.append(f"  {t.get('ticker',''):<38} price={t.get('yes_price','?')}c  count={t.get('count','?')}")
    else:
        lines.append("  No trades data available.")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
