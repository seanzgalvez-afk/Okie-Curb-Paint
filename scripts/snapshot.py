"""Kalshi snapshot — discover WC via /series endpoint + brute force event fetch."""
import base64, os, sys, time, traceback
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
    try:
        r = httpx.get(f"{BASE_URL}{path}", headers=auth_headers("GET", path),
                      params=params, timeout=15)
        if not r.is_success:
            return None
        return r.json()
    except Exception:
        return None

def log(msg): print(msg, file=sys.stderr)
def cents(d):
    try: return int(round(float(d) * 100))
    except: return None

ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines  = [f"# Kalshi Market Snapshot\n# Generated: {ts_str}\n" + "="*70]

try:
    bal = get("/portfolio/balance")
    lines.append(f"\n## Balance: ${(bal or {}).get('balance', 0)/100:.2f}" if bal else "\n## Balance: unavailable")
except Exception:
    lines.append("\n## Balance: error")

try:
    pos = get("/portfolio/positions", {"limit": 50})
    positions = (pos or {}).get("market_positions", []) or []
    lines.append("\n## Open positions: " + ("none" if not positions else ""))
    for p in positions:
        qty = p.get("position", 0)
        lines.append(f"  {p.get('ticker','')}  {'YES' if qty>0 else 'NO'}  qty={abs(qty)}")
except Exception:
    lines.append("\n## Positions: error")

# ================================================================
# SECTION 1: Active markets
# ================================================================
try:
    trades_resp = get("/markets/trades", {"limit": 200})
    trades = (trades_resp or {}).get("trades", []) or []
    trade_price, trade_count, ordered, seen = {}, {}, [], set()
    for t in trades:
        tk = t.get("ticker", "")
        if not tk: continue
        if tk not in seen:
            seen.add(tk); ordered.append(tk)
        p = cents(t.get("yes_price_dollars"))
        if p is not None: trade_price[tk] = p
        trade_count[tk] = trade_count.get(tk, 0) + 1
    ordered.sort(key=lambda tk: trade_count.get(tk, 0), reverse=True)
    markets_active = []
    for ticker in ordered[:20]:
        try:
            resp = get(f"/markets/{ticker}")
            if not resp: continue
            m = resp.get("market", resp) if isinstance(resp, dict) else resp
            if not isinstance(m, dict): continue
            m["_tc"] = trade_count.get(ticker, 0)
            m["_tp"] = trade_price.get(ticker)
            markets_active.append(m)
        except Exception: continue
    markets_active.sort(key=lambda m: m.get("_tc", 0), reverse=True)
    lines.append("\n" + "="*70)
    lines.append("## MOST ACTIVE RIGHT NOW")
    lines.append("="*70)
    lines.append(f"  {'Ticker':<42} {'YES':>4} {'NO':>4}  Trades  Closes  Title")
    lines.append("  " + "-"*95)
    for m in markets_active:
        y = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars")) or m.get("_tp")
        n = cents(m.get("no_bid_dollars"))
        lines.append(f"  {m.get('ticker','')[:42]:<42} {str(y)+'c' if y else '?':>4} "
                     f"{str(n)+'c' if n else '?':>4}  {m.get('_tc',0):>6}  "
                     f"{str(m.get('close_time',''))[:10]}  {m.get('title','')[:40]}")
except Exception as e:
    lines.append(f"\n## Active markets ERROR: {e}")

# ================================================================
# SECTION 2: World Cup discovery
# ================================================================
lines.append("\n" + "="*70)
lines.append("## FIFA WORLD CUP 2026 — DISCOVERY")
lines.append("="*70)

try:
    # Step 1: Try the /series endpoint to list all series
    lines.append("\n### /series endpoint scan:")
    for series_path in ["/series", "/series/?limit=100", "/series?limit=100"]:
        resp = get(series_path)
        if resp:
            lines.append(f"  /series returned: {str(resp)[:500]}")
            break
    else:
        lines.append("  /series endpoint: no response")

    # Step 2: Brute-force specific event tickers
    # Based on known Kalshi patterns like KXNBAGAME-DATE-MATCHUP
    # WC futures might be just the series name as event ticker
    lines.append("\n### Direct event ticker fetch attempts:")
    GUESSES = [
        "KXWC26", "KXWC2026", "KXFIFAWC26", "KXFIFAWC2026",
        "KXWC26FUTURES", "KXWC2026FUTURES", "KXFIFAWC26FUTURES",
        "KXWC26WINNER", "KXWC26CHAMP", "KXWC26CHAMPION",
        "KXWC26GROUPA", "KXWC26GROUPB", "KXWC26GROUPC",
        "KXWC26-GROUPA", "KXWC26-GROUPB",
        "KXWC26GROUPAWINNER", "KXWC26GROUPBWINNER",
        "KXWC26FURTHEST", "KXWC26ELIM", "KXWC26STAGE",
        "KXWC26HOST", "KXWC26USA", "KXWC26AWARDS",
        "KXWC26GOALS", "KXWC26TOURGOALS", "KXWC26SQUAD",
        "KXWC26SPEC", "KXWC26SPECIAL",
        "KXSOCWC26", "KXSOC26", "KXSOCCERWC26",
        # Try without KX prefix
        "WC26FUTURES", "FIFAWC26",
    ]
    found_events = []
    for et in GUESSES:
        resp = get(f"/events/{et}")
        if resp:
            lines.append(f"  HIT: /events/{et} -> {str(resp)[:200]}")
            found_events.append(et)
        else:
            lines.append(f"  miss: {et}")

    # Step 3: Use known series tickers from trades to find WC pattern
    # Extract series from known active tickers
    lines.append("\n### Series tickers found in trades feed:")
    known_series = set()
    for ticker in ordered[:40]:
        # Extract series by taking everything before the date pattern
        parts = ticker.split("-")
        if parts:
            known_series.add(parts[0])
    for s in sorted(known_series):
        lines.append(f"  {s}")

except Exception as e:
    lines.append(f"  ERROR: {e}")
    traceback.print_exc(file=sys.stderr)

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
