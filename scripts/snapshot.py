"""Kalshi snapshot — RSA-PSS auth, liquid markets by volume."""
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
                  params=params, timeout=15)
    print(f"GET {path} -> {r.status_code}", file=sys.stderr)
    if not r.is_success:
        print(f"  {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()

ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines  = [f"# Kalshi Market Snapshot\n# Generated: {ts_str}\n" + "="*70]

# Balance
bal = get("/portfolio/balance")
lines.append(f"\n## Balance: ${(bal.get('balance',0) if bal else 0)/100:.2f}"
             if bal else "\n## Balance: unavailable")

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

# Markets sorted by volume descending — skip zero-volume parlays
all_markets = []
for series_ticker in [None]:  # None = all categories
    params = {"status": "open", "limit": 100}
    if series_ticker:
        params["series_ticker"] = series_ticker
    resp = get("/markets", params)
    if resp:
        all_markets.extend(resp.get("markets", []))

# Sort by volume descending, filter out zero-volume
liquid = sorted(
    [m for m in all_markets if m.get("volume", 0) > 0],
    key=lambda m: m.get("volume", 0),
    reverse=True
)[:40]

zero_vol = [m for m in all_markets if m.get("volume", 0) == 0]

if liquid:
    lines.append(f"\n## Top markets by volume ({len(liquid)} liquid):")
    lines.append(f"  {'Ticker':<40} {'Yes':>4} {'No':>4} {'Volume':>9}  Closes")
    lines.append("  " + "-"*75)
    for m in liquid:
        yes  = m.get("yes_bid", m.get("last_price", "?"))
        no_p = m.get("no_bid", "?")
        vol  = m.get("volume", 0)
        close = str(m.get("close_time", ""))[:10]
        ticker = m.get("ticker", "")[:40]
        title  = m.get("title", "")[:65]
        lines.append(f"  {ticker:<40} {str(yes):>3}c {str(no_p):>3}c {vol:>9,}  {close}")
        lines.append(f"    {title}")
else:
    lines.append(f"\n## No liquid markets found (all {len(zero_vol)} markets have 0 volume)")
    lines.append("  Try checking Kalshi directly — the API may be returning limited data.")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
