"""Kalshi snapshot — email/password token auth (no RSA keys needed)."""
import os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import httpx

EMAIL    = os.environ.get("KALSHI_EMAIL", "")
PASSWORD = os.environ.get("KALSHI_PASSWORD", "")
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

if not EMAIL or not PASSWORD:
    sys.exit("ERROR: KALSHI_EMAIL and KALSHI_PASSWORD must be set")

# ── Login ──────────────────────────────────────────────────────────────────────
print("Logging in...", file=sys.stderr)
r = httpx.post(f"{BASE_URL}/log_in",
               json={"email": EMAIL, "password": PASSWORD}, timeout=15)
print(f"Login → {r.status_code}", file=sys.stderr)
if r.status_code != 200:
    print(f"Login failed: {r.text[:300]}", file=sys.stderr)
    sys.exit(1)

token = r.json().get("token", "")
if not token:
    sys.exit(f"No token in response: {r.json()}")

print("Login OK ✓", file=sys.stderr)
auth = {"Authorization": f"Bearer {token}"}

def get(path: str, params: dict | None = None):
    r = httpx.get(f"{BASE_URL}{path}", headers=auth, params=params, timeout=15)
    print(f"GET {path} → {r.status_code}", file=sys.stderr)
    if not r.is_success:
        print(f"  body: {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()

# ── Fetch ──────────────────────────────────────────────────────────────────────
ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines  = [f"# Kalshi Market Snapshot\n# Generated: {ts_str}\n{'='*70}"]

bal = get("/portfolio/balance")
lines.append(f"\n## Balance: ${(bal.get('balance', 0) if bal else 0)/100:.2f}"
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
        lines.append(f"  {m.get('ticker',''):<36} {str(yes):>4}¢ {str(no_p):>4}¢ "
                     f"{m.get('volume',0):>8,}  {str(m.get('close_time',''))[:10]}")
        lines.append(f"    {m.get('title','')[:60]}")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved → {out}", file=sys.stderr)
else:
    print(snapshot)
