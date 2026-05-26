"""Kalshi snapshot — active markets + targeted WC search."""
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
        print(f"GET {path} {params} -> {r.status_code}", file=sys.stderr)
        if not r.is_success:
            print(f"  {r.text[:200]}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"ERROR {path}: {e}", file=sys.stderr)
        return None

def cents(d):
    try:
        return int(round(float(d) * 100))
    except (TypeError, ValueError):
        return None

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
    traceback.print_exc(file=sys.stderr)

# ================================================================
# SECTION 2: World Cup — targeted search based on known categories
# ================================================================
lines.append("\n" + "="*70)
lines.append("## FIFA WORLD CUP 2026")
lines.append("="*70)

try:
    # Based on app categories: Games, Futures, Awards, Group Winner,
    # Group Qualifiers, Group Goals, Group Stage Specials, Furthest Stage,
    # Stage of Elimination, Squad Selection, Tournament Goals,
    # World Cup Specials, Country Goals, Host Nation

    # Try every plausible series ticker variant
    WC_SERIES = [
        # Most likely
        "KXWC26", "KXWC2026", "KXFIFAWC26", "KXFIFAWC", "KXWC",
        # Category-based
        "KXWC26FUTURES", "KXWC26GAMES", "KXWC26AWARDS",
        "KXWC26GROUPWIN", "KXWC26GROUP", "KXWC26QUALS",
        "KXWC26STAGE", "KXWC26GOALS", "KXWC26SPECIAL",
        "KXWC26HOST", "KXWC26SQUAD", "KXWC26COUNTRY",
        # Alt formats
        "KXFIFA", "KXFIFA26", "KXWORLDCUP", "KXWORLDCUP26",
        "KXSOCCER", "KXSOCCER26", "KXFOOTBALL", "KXFOOTBALL26",
        "KXWC26WIN", "KXWC26CHAMP", "KXWC26ELIM", "KXWC26FURTHER",
        # Try with 2026 appended differently
        "KX2026WC", "KXWC26TOURN", "KXWC26SPEC",
    ]
    STATUSES = ["open", "active", "closed", ""]  # try all status values

    wc_markets = []
    found_via  = None
    seen_tk    = set()
    hits       = []  # log all hits for diagnostics

    for series in WC_SERIES:
        for status in STATUSES:
            params = {"series_ticker": series, "limit": 100}
            if status:
                params["status"] = status
            resp = get("/markets", params)
            batch = (resp or {}).get("markets", []) or []
            if batch:
                hits.append(f"{series}/{status}: {len(batch)} markets")
                for m in batch:
                    tk = m.get("ticker", "")
                    if tk and tk not in seen_tk:
                        seen_tk.add(tk); wc_markets.append(m)
                if not found_via:
                    found_via = f"{series} (status={status or 'any'})"

    # Also try fetching specific likely event tickers directly
    WC_EVENTS = [
        "KXWC26FUTURES", "KXWC2026FUTURES", "KXFIFAWC26FUTURES",
        "KXWC26WINNER", "KXWC26CHAMP", "KXWC2026WINNER",
        "KXWC26GROUPAWINNER", "KXWC26GROUPBWINNER",
        "KXWC2026", "KXFIFAWC2026",
    ]
    for et in WC_EVENTS:
        resp = get(f"/events/{et}")
        if resp:
            event = (resp or {}).get("event", {})
            ms = (event.get("markets") or []) if isinstance(event, dict) else []
            for m in ms:
                tk = m.get("ticker", "")
                if tk and tk not in seen_tk:
                    seen_tk.add(tk); wc_markets.append(m)
            if ms:
                hits.append(f"event/{et}: {len(ms)} markets")
                if not found_via: found_via = f"event/{et}"

    lines.append(f"  Series/event hits: {hits if hits else 'none'}")

    if wc_markets:
        by_event = {}
        for m in wc_markets:
            ev = m.get("event_ticker", "other")
            by_event.setdefault(ev, []).append(m)
        lines.append(f"  FOUND {len(wc_markets)} WC markets via: {found_via}")
        for ev, ms in sorted(by_event.items()):
            lines.append(f"\n### {ev}")
            lines.append(f"  {'Title':<52} {'YES':>4} {'NO':>4}  Closes")
            lines.append("  " + "-"*72)
            for m in sorted(ms, key=lambda x: x.get("title", "")):
                title = m.get("title", "")[:52]
                close = str(m.get("close_time", ""))[:10]
                y = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars"))
                n = cents(m.get("no_bid_dollars"))
                lines.append(f"  {title:<52} {str(y)+'c' if y else '?':>4} {str(n)+'c' if n else '?':>4}  {close}")
    else:
        lines.append("  Tried all variants. WC markets not accessible via this API key.")
        lines.append("  This may be a permissions issue or the markets use a private endpoint.")

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
