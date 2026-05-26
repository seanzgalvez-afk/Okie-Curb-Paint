"""Kalshi snapshot — robust version, never crashes, World Cup aware."""
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
        print(f"GET {path} -> {r.status_code}", file=sys.stderr)
        if not r.is_success:
            print(f"  {r.text[:200]}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"GET {path} ERROR: {e}", file=sys.stderr)
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
    if bal and isinstance(bal, dict):
        lines.append(f"\n## Balance: ${bal.get('balance', 0)/100:.2f}")
    else:
        lines.append("\n## Balance: unavailable")
except Exception:
    lines.append("\n## Balance: error")

try:
    pos = get("/portfolio/positions", {"limit": 50})
    positions = (pos or {}).get("market_positions", []) or [] if pos else []
    lines.append("\n## Open positions:")
    if positions:
        for p in positions:
            qty = p.get("position", 0)
            lines.append(f"  {p.get('ticker','')}  {'YES' if qty>0 else 'NO'}  qty={abs(qty)}")
    else:
        lines.append("  none")
except Exception:
    lines.append("\n## Positions: error")

# ================================================================
# SECTION 1: Active markets via trades feed
# ================================================================
try:
    trades_resp = get("/markets/trades", {"limit": 200})
    trades = (trades_resp or {}).get("trades", []) or []

    trade_price, trade_count, ordered, seen = {}, {}, [], set()
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
    for ticker in ordered[:20]:
        try:
            resp = get(f"/markets/{ticker}")
            if not resp:
                continue
            m = resp.get("market", resp) if isinstance(resp, dict) else resp
            if not isinstance(m, dict):
                continue
            m["_trade_price"] = trade_price.get(ticker)
            m["_trade_count"] = trade_count.get(ticker, 0)
            markets_active.append(m)
        except Exception:
            continue

    markets_active.sort(key=lambda m: m.get("_trade_count", 0), reverse=True)

    lines.append(f"\n" + "="*70)
    lines.append(f"## MOST ACTIVE RIGHT NOW")
    lines.append("="*70)
    lines.append(f"  {'Ticker':<40} {'YES':>4} {'NO':>4}  Trades  Closes  Title")
    lines.append("  " + "-"*95)
    for m in markets_active:
        ticker   = m.get("ticker", "")[:40]
        close    = str(m.get("close_time", ""))[:10]
        tn       = m.get("_trade_count", 0)
        y  = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars")) or m.get("_trade_price")
        n  = cents(m.get("no_bid_dollars"))
        ys = f"{y}c" if y is not None else "  ?"
        ns = f"{n}c" if n is not None else "  ?"
        title = m.get("title", "")[:40]
        lines.append(f"  {ticker:<40} {ys:>4} {ns:>4}  {tn:>6}  {close}  {title}")
except Exception as e:
    lines.append(f"\n## Active markets: ERROR - {e}")
    traceback.print_exc(file=sys.stderr)

# ================================================================
# SECTION 2: World Cup markets
# ================================================================
lines.append(f"\n" + "="*70)
lines.append("## FIFA WORLD CUP 2026")
lines.append("="*70)

try:
    WC_KEYWORDS = ["world cup", "fifa", "worldcup", "wc2026", "wc 2026",
                   "soccer", "football", "copa"]
    WC_SERIES   = ["KXFIFAWC26", "KXFIFAWC", "KXWC26", "KXWC2026", "KXWC",
                   "KXSOCCER", "FIFAWC26", "KXCOPA"]

    wc_markets = []
    seen_tk    = set()

    # Try series tickers directly (fast)
    for series in WC_SERIES:
        resp = get("/markets", {"status": "open", "series_ticker": series, "limit": 100})
        batch = (resp or {}).get("markets", []) or []
        for m in batch:
            tk = m.get("ticker", "")
            if tk and tk not in seen_tk:
                seen_tk.add(tk)
                wc_markets.append(m)
        if wc_markets:
            print(f"  Found WC via series {series}: {len(wc_markets)} markets", file=sys.stderr)
            break

    # If nothing found via series, scan events (up to 5 pages = 500 events)
    if not wc_markets:
        wc_event_tickers = []
        cursor = None
        for _ in range(5):
            resp = get("/events", {"status": "open", "limit": 100,
                                   **(({"cursor": cursor}) if cursor else {})})
            if not resp:
                break
            events = (resp or {}).get("events", []) or []
            for e in events:
                t = (e.get("title") or "").lower()
                s = (e.get("series_ticker") or "").lower()
                if any(kw in t or kw in s for kw in WC_KEYWORDS):
                    et = e.get("event_ticker", "")
                    if et:
                        wc_event_tickers.append(et)
            cursor = resp.get("cursor")
            if not cursor or not events:
                break

        print(f"  WC events found: {len(wc_event_tickers)}", file=sys.stderr)
        for et in wc_event_tickers[:30]:
            try:
                resp = get(f"/events/{et}")
                event = (resp or {}).get("event", resp) if resp else {}
                for m in (event.get("markets") or [] if isinstance(event, dict) else []):
                    tk = m.get("ticker", "")
                    if tk and tk not in seen_tk:
                        seen_tk.add(tk)
                        wc_markets.append(m)
            except Exception:
                continue

    if wc_markets:
        by_event = {}
        for m in wc_markets:
            ev = m.get("event_ticker", "other")
            by_event.setdefault(ev, []).append(m)
        for ev, ms in sorted(by_event.items()):
            lines.append(f"\n### {ev} ({len(ms)} markets)")
            lines.append(f"  {'Title':<50} {'YES':>4} {'NO':>4}  Closes")
            lines.append("  " + "-"*72)
            for m in sorted(ms, key=lambda x: x.get("title", "")):
                title = m.get("title", "")[:50]
                close = str(m.get("close_time", ""))[:10]
                y = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars"))
                n = cents(m.get("no_bid_dollars"))
                ys = f"{y}c" if y is not None else "  ?"
                ns = f"{n}c" if n is not None else "  ?"
                lines.append(f"  {title:<50} {ys:>4} {ns:>4}  {close}")
    else:
        lines.append("\n  No WC markets found yet. Dumping sample event titles:")
        resp = get("/events", {"status": "open", "limit": 25})
        for e in ((resp or {}).get("events") or [])[:25]:
            lines.append(f"    [{e.get('series_ticker','?')}] {e.get('title','')}")
except Exception as e:
    lines.append(f"  World Cup section ERROR: {e}")
    traceback.print_exc(file=sys.stderr)

# Always write output, even if parts failed
snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
