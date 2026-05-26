"""Kalshi snapshot — RSA-PSS auth, JSON dashboard output, WC discovery."""
import base64, json, os, sys, time, traceback
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

def score_market(m, all_volumes, today):
    """Calculate edge score (0-10) for best picks ranking."""
    score, reasons = 0.0, []
    yes_price = m.get("_yes_price")
    volume    = m.get("volume", 0) or 0
    close_time = m.get("close_time", "")

    max_vol = max(all_volumes) if all_volumes else 1
    if max_vol > 0:
        vol_score = (volume / max_vol) * 4
        score += vol_score
        if vol_score >= 2: reasons.append("High volume")

    if yes_price is not None:
        if 15 <= yes_price <= 45:
            score += 3; reasons.append("Underdog value zone")
        elif 55 <= yes_price <= 85:
            score += 2.5; reasons.append("Strong favorite zone")
        elif yes_price < 15 or yes_price > 85:
            score += 1; reasons.append("Extreme price")

    if close_time:
        try:
            close_dt  = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            days_left = (close_dt.date() - today).days
            if 0 <= days_left <= 7:
                score += 2; reasons.append("Expiring soon")
            elif days_left <= 14:
                score += 1; reasons.append("Expiring within 2 weeks")
        except (ValueError, AttributeError):
            pass

    return round(score, 2), (" + ".join(reasons) if reasons else "Liquid market")

ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines  = [f"# Kalshi Market Snapshot\n# Generated: {ts_str}\n" + "="*70]

balance_cents = 0
try:
    bal = get("/portfolio/balance")
    balance_cents = (bal.get("balance", 0) if bal else 0) or 0
    lines.append(f"\n## Balance: ${balance_cents/100:.2f}" if bal else "\n## Balance: unavailable")
except Exception:
    lines.append("\n## Balance: error")

positions_list = []
try:
    pos = get("/portfolio/positions", {"limit": 50})
    positions = (pos or {}).get("market_positions", []) or []
    lines.append("\n## Open positions: " + ("none" if not positions else ""))
    for p in positions:
        qty = p.get("position", 0)
        lines.append(f"  {p.get('ticker','')}  {'YES' if qty>0 else 'NO'}  qty={abs(qty)}")
        positions_list.append({"ticker": p.get("ticker", ""),
                                "side": "YES" if qty > 0 else "NO",
                                "qty": abs(qty)})
except Exception:
    lines.append("\n## Positions: error")

# ================================================================
# SECTION 1: Active markets
# ================================================================
markets_list = []
ordered      = []

try:
    trades_resp = get("/markets/trades", {"limit": 200})
    trades = (trades_resp or {}).get("trades", []) or []
    trade_price, trade_count, seen = {}, {}, set()
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

        # Build structured entry for JSON dashboard
        yes_bid   = cents(m.get("yes_bid_dollars"))  or m.get("_tp")
        no_bid    = cents(m.get("no_bid_dollars"))
        yes_ask   = cents(m.get("yes_ask_dollars"))
        no_ask    = cents(m.get("no_ask_dollars"))
        last_p    = cents(m.get("last_price_dollars")) or m.get("_tp")
        yes_price = yes_bid or yes_ask or last_p

        ticker_str = m.get("ticker", "")
        close_raw  = str(m.get("close_time", ""))
        close_date = close_raw[:10] if close_raw else ""

        t_up = ticker_str.upper()
        if any(x in t_up for x in ["NBA","NFL","MLB","NHL","SPORTS","GAME"]):
            category = "Sports"
        elif any(x in t_up for x in ["BTC","ETH","CRYPTO"]):
            category = "Crypto"
        elif any(x in t_up for x in ["POL","PRES","SENATE","HOUSE","GOV"]):
            category = "Politics"
        elif any(x in t_up for x in ["WC","FIFA","SOC"]):
            category = "Soccer"
        else:
            category = "Other"

        markets_list.append({
            "ticker":      ticker_str,
            "title":       m.get("title", ""),
            "yes_bid":     yes_bid,
            "no_bid":      no_bid,
            "yes_ask":     yes_ask,
            "no_ask":      no_ask,
            "last_price":  last_p,
            "volume":      m.get("volume", 0) or 0,
            "close_time":  close_date,
            "category":    category,
            "_yes_price":  yes_price,
            "_trade_count": trade_count.get(ticker_str, 0),
        })

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
    log(f"Saved text -> {out}")
else:
    print(snapshot)

# ── Best picks scoring ────────────────────────────────────────────────────────
today       = datetime.now(timezone.utc).date()
all_volumes = [m["volume"] for m in markets_list]

scored = sorted(
    [(score_market(m, all_volumes, today), m) for m in markets_list],
    key=lambda x: x[0][0], reverse=True
)

best_picks = []
for (score, reason), m in scored[:5]:
    yp = m.get("_yes_price")
    if yp is None:
        side, price = "YES", 50
    elif yp < 50:
        side, price = "YES", yp
    else:
        side, price = "NO", 100 - yp
    best_picks.append({"ticker": m["ticker"], "title": m["title"],
                        "side": side, "price": price,
                        "reason": reason, "edge_score": score})

# ── Write JSON for dashboard ──────────────────────────────────────────────────
docs_dir = Path(__file__).parent.parent / "docs"
docs_dir.mkdir(exist_ok=True)

clean_markets = [{k: v for k, v in m.items() if not k.startswith("_")}
                 for m in markets_list]

(docs_dir / "data.json").write_text(json.dumps({
    "generated":     ts_str,
    "balance_cents": balance_cents,
    "positions":     positions_list,
    "markets":       clean_markets,
    "best_picks":    best_picks,
}, indent=2))
log(f"Saved JSON -> {docs_dir / 'data.json'}")
