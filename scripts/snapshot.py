"""Kalshi snapshot — active markets + full World Cup section."""
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
        print(f"  {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()

def cents(d):
    try:
        return int(round(float(d) * 100))
    except (TypeError, ValueError):
        return None

def price_str(m):
    """Return 'YES XXc / NO XXc' or best available price string."""
    y = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars"))
    n = cents(m.get("no_bid_dollars"))
    y_s = f"{y}¢" if y is not None else " ?"
    n_s = f"{n}¢" if n is not None else " ?"
    return f"YES {y_s:>4} / NO {n_s:>4}"

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

# ================================================================
# SECTION 1: Active markets via recent trades
# ================================================================
trades_resp = get("/markets/trades", {"limit": 200})
trades = (trades_resp or {}).get("trades", []) or []

trade_price = {}
trade_count = {}
ordered = []
seen = set()
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

ordered.sort(key=lambda tk: trade_count.get(tk, 0), reverse=True)

markets_active = []
for ticker in ordered[:20]:  # top 20 most active
    resp = get(f"/markets/{ticker}")
    if not resp:
        continue
    m = resp.get("market", resp) if isinstance(resp, dict) else resp
    if not isinstance(m, dict):
        continue
    m["_trade_price"] = trade_price.get(ticker)
    m["_trade_count"] = trade_count.get(ticker, 0)
    markets_active.append(m)

markets_active.sort(key=lambda m: m.get("_trade_count", 0), reverse=True)

lines.append(f"\n" + "="*70)
lines.append(f"## MOST ACTIVE MARKETS RIGHT NOW ({len(markets_active)} shown)")
lines.append("="*70)
lines.append(f"  {'Ticker':<42} {'Price':>6}  Trades  Closes")
lines.append("  " + "-"*80)
for m in markets_active:
    ticker = m.get("ticker", "")[:42]
    close  = str(m.get("close_time", ""))[:10]
    trades_n = m.get("_trade_count", 0)
    y = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars")) or m.get("_trade_price")
    p_s = f"{y}¢" if y is not None else "  ?"
    title = m.get("title", "")[:50]
    lines.append(f"  {ticker:<42} {p_s:>5}  {trades_n:>6}  {close}  {title}")

# ================================================================
# SECTION 2: World Cup markets
# ================================================================
lines.append(f"\n" + "="*70)
lines.append("## FIFA WORLD CUP 2026 MARKETS")
lines.append("="*70)

WC_KEYWORDS = ["world cup", "fifa", "worldcup", "wc2026", "wc 2026"]

# Strategy A: scan events endpoint for WC events
wc_event_tickers = []
cursor = None
for _ in range(10):  # up to 1000 events
    params = {"status": "open", "limit": 100}
    if cursor:
        params["cursor"] = cursor
    resp = get("/events", params)
    if not resp:
        break
    events = resp.get("events", []) or []
    for e in events:
        title = (e.get("title") or "").lower()
        series = (e.get("series_ticker") or "").lower()
        if any(kw in title or kw in series for kw in WC_KEYWORDS):
            wc_event_tickers.append(e.get("event_ticker", ""))
    cursor = resp.get("cursor")
    if not cursor or not events:
        break

print(f"  Found {len(wc_event_tickers)} WC events from events endpoint", file=sys.stderr)

# Strategy B: also try known series tickers directly
WC_SERIES = ["KXFIFAWC26", "KXFIFAWC", "KXWC26", "KXWC2026", "KXWC",
             "FIFAWC", "FIFAWC26", "SOCCER26", "KXSOCCER"]

wc_markets = []
seen_tickers = set()

# Fetch markets from discovered WC events
for event_ticker in wc_event_tickers[:50]:
    resp = get(f"/events/{event_ticker}")
    if not resp:
        continue
    event = resp.get("event", resp)
    for m in (event.get("markets") or []):
        tk = m.get("ticker", "")
        if tk and tk not in seen_tickers:
            seen_tickers.add(tk)
            wc_markets.append(m)

# Fetch markets from known series tickers
for series in WC_SERIES:
    resp = get("/markets", {"status": "open", "series_ticker": series, "limit": 100})
    if not resp:
        continue
    for m in (resp.get("markets") or []):
        tk = m.get("ticker", "")
        if tk and tk not in seen_tickers:
            seen_tickers.add(tk)
            wc_markets.append(m)
    if wc_markets:
        print(f"  Series {series}: found {len(wc_markets)} WC markets", file=sys.stderr)
        break  # found them, stop trying

print(f"  Total WC markets: {len(wc_markets)}", file=sys.stderr)

if wc_markets:
    # Group by event_ticker
    by_event = {}
    for m in wc_markets:
        ev = m.get("event_ticker", "other")
        by_event.setdefault(ev, []).append(m)

    for event_ticker, ms in sorted(by_event.items()):
        lines.append(f"\n### {event_ticker}")
        lines.append(f"  {'Title':<55} {'YES':>5} {'NO':>5}  Closes")
        lines.append("  " + "-"*80)
        for m in sorted(ms, key=lambda x: x.get("title", "")):
            title = m.get("title", "")[:55]
            close = str(m.get("close_time", ""))[:10]
            y = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars"))
            n = cents(m.get("no_bid_dollars"))
            y_s = f"{y}¢" if y is not None else "  ?"
            n_s = f"{n}¢" if n is not None else "  ?"
            lines.append(f"  {title:<55} {y_s:>5} {n_s:>5}  {close}")
else:
    lines.append("\n  No World Cup markets found via events or known series tickers.")
    lines.append("  The WC series ticker may differ — check data below for clues.")
    # Dump a sample of event titles to help find the right series
    lines.append("\n  Sample open event titles (first 20):")
    resp = get("/events", {"status": "open", "limit": 20})
    if resp:
        for e in (resp.get("events") or [])[:20]:
            lines.append(f"    [{e.get('series_ticker','')}] {e.get('title','')}")

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
