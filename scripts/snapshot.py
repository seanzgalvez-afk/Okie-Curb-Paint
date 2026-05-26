"""Kalshi snapshot — RSA-PSS auth, paginates to find liquid markets."""
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
    cents = bal.get("balance", 0)
    lines.append(f"\n## Balance: ${cents/100:.2f}")
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

# Paginate through markets to find liquid ones (up to 500)
all_markets = []
cursor = None
for page_num in range(5):
    params = {"status": "open", "limit": 100}
    if cursor:
        params["cursor"] = cursor
    resp = get("/markets", params)
    if not resp:
        break
    batch = resp.get("markets", [])
    all_markets.extend(batch)
    print(f"  Page {page_num+1}: got {len(batch)} markets (total={len(all_markets)})", file=sys.stderr)
    cursor = resp.get("cursor")
    if not cursor or len(batch) == 0:
        break

# Filter: must have actual volume > 0
liquid = [
    m for m in all_markets
    if m.get("volume", 0) > 0
]
liquid.sort(key=lambda m: m.get("volume", 0), reverse=True)
top40 = liquid[:40]

lines.append(f"\n## Top markets by volume ({len(liquid)} liquid out of {len(all_markets)} scanned):")
if top40:
    lines.append(f"  {'Ticker':<38} {'Yes':>4} {'No':>4} {'Volume':>10}  Closes")
    lines.append("  " + "-"*75)
    for m in top40:
        yes_p = m.get("yes_bid", m.get("last_price", "?"))
        no_p  = m.get("no_bid", "?")
        vol   = m.get("volume", 0)
        close = str(m.get("close_time", ""))[:10]
        ticker = m.get("ticker", "")[:38]
        title  = m.get("title", "")[:70]
        lines.append(f"  {ticker:<38} {str(yes_p):>3}c {str(no_p):>3}c {vol:>10,}  {close}")
        lines.append(f"    {title}")
else:
    lines.append(f"  No markets with volume > 0 found in {len(all_markets)} scanned.")
    lines.append("  (All open markets appear to be zero-volume parlays right now.)")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
