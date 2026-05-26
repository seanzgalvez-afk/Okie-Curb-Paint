"""Kalshi snapshot — active markets + deep World Cup search."""
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
    lines.append(f"\n## Balance: ${(bal or {}).get('balance', 0)/100:.2f}" if bal else "\n## Balance: unavailable")
except Exception:
    lines.append("\n## Balance: error")

try:
    pos = get("/portfolio/positions", {"limit": 50})
    positions = (pos or {}).get("market_positions", []) or []
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
# SECTION 2: World Cup — exhaustive search
# ================================================================
lines.append("\n" + "="*70)
lines.append("## FIFA WORLD CUP 2026")
lines.append("="*70)

try:
    WC_KEYWORDS = [
        "world cup", "fifa", "worldcup", "wc 2026", "wc2026",
        "soccer", "football", "copa mundial", "copa",
        "brazil", "france", "argentina", "england", "spain",
        "germany", "portugal", "group a", "group b", "group c",
        "group d", "group e", "group f", "group g", "group h",
    ]
    # Broad list of possible series tickers
    WC_SERIES = [
        "KXFIFAWC26", "KXFIFAWC", "KXWC26", "KXWC2026", "KXWC",
        "KXSOCCER", "KXSOCCER26", "KXFIFA", "KXFIFA26",
        "FIFAWC26", "KXCOPA", "KXCOPA26", "KXWORLDCUP",
        "KXWORLDCUP26", "KXWC26CHAMP", "KXFOOTBALL",
        "KXWC26GROUP", "KXWC26MATCH", "KXWC26WIN",
    ]

    wc_markets = []
    found_series = None
    seen_tk = set()

    # 1. Try series tickers directly
    for series in WC_SERIES:
        resp = get("/markets", {"status": "open", "series_ticker": series, "limit": 100})
        batch = (resp or {}).get("markets", []) or []
        for m in batch:
            tk = m.get("ticker", "")
            if tk and tk not in seen_tk:
                seen_tk.add(tk); wc_markets.append(m)
        if batch:
            found_series = series
            print(f"  Found WC via series {series}: {len(batch)} markets", file=sys.stderr)
            break

    # 2. Scan ALL events pages (up to 3000 events) looking for WC
    if not wc_markets:
        print("  Series scan failed, scanning all events...", file=sys.stderr)
        wc_event_tickers = []
        all_series_seen = set()
        cursor = None
        pages = 0
        while pages < 30:  # up to 3000 events
            resp = get("/events", {"status": "open", "limit": 100,
                                   **(({"cursor": cursor}) if cursor else {})})
            if not resp: break
            events = (resp or {}).get("events", []) or []
            for e in events:
                t = (e.get("title") or "").lower()
                s = (e.get("series_ticker") or "").lower()
                all_series_seen.add(e.get("series_ticker", ""))
                if any(kw in t or kw in s for kw in WC_KEYWORDS):
                    et = e.get("event_ticker", "")
                    if et: wc_event_tickers.append((et, e.get("title",""), e.get("series_ticker","")))
            cursor = resp.get("cursor")
            pages += 1
            if not cursor or not events: break

        print(f"  Scanned {pages} pages. WC events: {len(wc_event_tickers)}", file=sys.stderr)

        # Fetch markets for each WC event found
        for et, etitle, eseries in wc_event_tickers[:50]:
            try:
                resp = get(f"/events/{et}")
                event = (resp or {}).get("event", {})
                if isinstance(event, dict):
                    for m in (event.get("markets") or []):
                        tk = m.get("ticker", "")
                        if tk and tk not in seen_tk:
                            seen_tk.add(tk); wc_markets.append(m)
            except Exception: continue

        if not wc_markets:
            # Show all unique series tickers found so we can identify the right one
            lines.append("\n  No WC markets found after exhaustive search.")
            lines.append(f"  Scanned {pages*100} events across {pages} pages.")
            lines.append("  All unique series tickers found (sample):")
            for s in sorted(all_series_seen)[:60]:
                if s: lines.append(f"    {s}")

    if wc_markets:
        by_event = {}
        for m in wc_markets:
            ev = m.get("event_ticker", "other")
            by_event.setdefault(ev, []).append(m)
        lines.append(f"\n  Found {len(wc_markets)} WC markets across {len(by_event)} events (series: {found_series})")
        for ev, ms in sorted(by_event.items()):
            lines.append(f"\n### {ev} ({len(ms)} markets)")
            lines.append(f"  {'Title':<52} {'YES':>4} {'NO':>4}  Closes")
            lines.append("  " + "-"*72)
            for m in sorted(ms, key=lambda x: x.get("title", "")):
                title = m.get("title", "")[:52]
                close = str(m.get("close_time", ""))[:10]
                y = cents(m.get("yes_bid_dollars")) or cents(m.get("last_price_dollars"))
                n = cents(m.get("no_bid_dollars"))
                ys = f"{y}c" if y is not None else "  ?"
                ns = f"{n}c" if n is not None else "  ?"
                lines.append(f"  {title:<52} {ys:>4} {ns:>4}  {close}")

except Exception as e:
    lines.append(f"  World Cup ERROR: {e}")
    traceback.print_exc(file=sys.stderr)

snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    print(f"Saved -> {out}", file=sys.stderr)
else:
    print(snapshot)
