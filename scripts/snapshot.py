"""Kalshi snapshot — RSA-PSS auth."""
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
    raw = "".join(raw.split())
    pem = base64.b64decode(raw)

PRIV_KEY = serialization.load_pem_private_key(pem, password=None)
print(f"Key ID : {KEY_ID}", file=sys.stderr)
print(f"Key OK : {bool(PRIV_KEY)}", file=sys.stderr)

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
        print(f"  body: {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()

ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines  = [f"# Kalshi Market Snapshot\n# Generated: {ts_str}\n" + "="*70]

bal = get("/portfolio/balance")
lines.append(f"\n## Balance: ${(bal.get('balance',0) if bal else 0)/100:.2f}"
             if bal else "\n## Balance: unavailable")

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

mkts = get("/markets", {"status": "open", "limit": 40})
if mkts:
    markets = mkts.get("markets", [])
    lines.append(f"\n## Open markets ({len(markets)}):")
    lines.append(f"  {'Ticker':<36} {'Yes':>5} {'No':>5} {'Vol':>8}  Closes")
    lines.append("  " + "-"*65)
    for m in markets:
        yes  = m.get("yes_bid", m.get("last_price", "?"))
        no_p = m.get("no_bid", "?")
        lines.append(f"  {m.get('ticker',''):<36} {str(yes):>4}c {str(no_p):>4}c "
                     f"{m.get('volume',0):>8,}  {str(m.get('close_time',''))[:10]}")
        lines.append(f"    {m.get('title','')[:60]}")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
