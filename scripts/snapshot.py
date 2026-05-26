"""Kalshi snapshot — RSA-PSS auth, JSON dashboard output, Vegas edge finder."""
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

# ═══════════════════════════════════════════════════════════════════════════════
# ODDS API — Vegas vs Kalshi edge finder
# ═══════════════════════════════════════════════════════════════════════════════
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE    = "https://api.the-odds-api.com/v4"

ODDS_SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_fifa_world_cup",
]

# Kalshi ticker/title fragment → team name fragment (for matching)
TEAM_HINTS = {
    # NBA
    "OKC": "oklahoma city", "SA": "san antonio",  "BOS": "boston",
    "MIA": "miami",         "NYK": "new york kni", "IND": "indiana",
    "MIL": "milwaukee",     "MIN": "minnesota",    "DEN": "denver",
    "LAL": "lakers",        "LAC": "clippers",     "GSW": "golden state",
    "PHX": "phoenix",       "DAL": "dallas",       "MEM": "memphis",
    "NOP": "new orleans",   "HOU": "houston",      "SAC": "sacramento",
    "UTA": "utah",          "CHA": "charlotte",    "ATL": "atlanta",
    "CHI": "chicago",       "CLE": "cleveland",    "DET": "detroit",
    "ORL": "orlando",       "PHI": "76ers",        "TOR": "toronto",
    "WAS": "washington",    "BKN": "brooklyn",     "POR": "portland",
    # NFL
    "KC":  "kansas city",   "SF":  "san francisco","BUF": "buffalo",
    "NE":  "new england",   "GB":  "green bay",    "SEA": "seattle",
    "BAL": "baltimore",     "CIN": "cincinnati",   "PIT": "pittsburgh",
    # Soccer
    "MEX": "mexico",        "USA": "united states","BRA": "brazil",
    "ARG": "argentina",     "FRA": "france",       "ENG": "england",
    "GER": "germany",       "ESP": "spain",        "POR": "portugal",
}

def am_to_prob(odds):
    """American odds integer → implied probability 0-100."""
    try:
        o = int(odds)
        if o < 0: return abs(o) / (abs(o) + 100) * 100
        return 100 / (o + 100) * 100
    except: return None

def fetch_vegas_odds():
    """Fetch all upcoming games from The Odds API. Returns list of game dicts."""
    if not ODDS_API_KEY:
        log("ODDS_API_KEY not set — skipping Vegas comparison")
        return []
    games = []
    for sport in ODDS_SPORTS:
        try:
            r = httpx.get(f"{ODDS_BASE}/sports/{sport}/odds/",
                params={"apiKey": ODDS_API_KEY, "regions": "us",
                        "markets": "h2h", "oddsFormat": "american"},
                timeout=15)
            log(f"Odds API {sport} -> {r.status_code}")
            if r.status_code == 200:
                sport_games = r.json()
                for g in sport_games:
                    g["_sport"] = sport
                    games.append(g)
                remaining = r.headers.get("x-requests-remaining", "?")
                log(f"  {len(sport_games)} games | {remaining} credits remaining")
        except Exception as e:
            log(f"Odds API {sport} error: {e}")
    return games

def game_consensus_prob(game, team_fragment):
    """Average implied probability for a team across all bookmakers (0-100)."""
    probs = []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h": continue
            for oc in mkt.get("outcomes", []):
                if team_fragment.lower() in oc.get("name", "").lower():
                    p = am_to_prob(oc.get("price"))
                    if p is not None: probs.append(p)
    return round(sum(probs) / len(probs), 1) if probs else None

def find_divergences(kalshi_markets, vegas_games):
    """Compare Kalshi prices to Vegas consensus. Return edge alert dicts."""
    divs = []
    for km in kalshi_markets:
        ticker = km.get("ticker", "")
        title  = km.get("title", "")
        kp     = km.get("yes_bid") or km.get("last_price")
        if kp is None: continue

        for game in vegas_games:
            home = game.get("home_team", "").lower()
            away = game.get("away_team", "").lower()
            matched_team = None

            # Try TEAM_HINTS lookup first
            for abbr, hint in TEAM_HINTS.items():
                if abbr in ticker.upper() or abbr in title.upper():
                    if hint in home: matched_team = hint; break
                    if hint in away: matched_team = hint; break

            # Fallback: word overlap between game teams and Kalshi title
            if not matched_team:
                for team in [home, away]:
                    for word in team.split():
                        if len(word) > 4 and word in title.lower():
                            matched_team = team; break
                    if matched_team: break

            if not matched_team: continue

            vp = game_consensus_prob(game, matched_team)
            if vp is None: continue

            gap = round(vp - kp, 1)
            if abs(gap) < 5: continue  # < 5¢ gap = noise

            books = []
            for bm in game.get("bookmakers", [])[:4]:
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != "h2h": continue
                    for oc in mkt.get("outcomes", []):
                        if matched_team.lower() in oc.get("name", "").lower():
                            books.append(f"{bm.get('title','?')}: {oc.get('price',0):+d}")

            divs.append({
                "ticker":       ticker,
                "title":        title,
                "kalshi_price": kp,
                "vegas_prob":   vp,
                "gap":          gap,
                "direction":    "BUY YES" if gap > 0 else "BUY NO",
                "game":         f"{game.get('away_team','')} @ {game.get('home_team','')}",
                "game_time":    game.get("commence_time", ""),
                "books":        books[:3],
                "sport":        game.get("_sport", ""),
            })

    divs.sort(key=lambda x: abs(x["gap"]), reverse=True)
    return divs[:10]

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

# ═══════════════════════════════════════════════════════════════════════════════
# ESPN — NBA injury reports and today's games
# ═══════════════════════════════════════════════════════════════════════════════
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

def fetch_espn_injuries():
    """Fetch NBA injury report from ESPN. Returns list of injury dicts."""
    try:
        r = httpx.get(f"{ESPN_BASE}/basketball/nba/injuries", timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        injuries = []
        for team in data.get("injuries", []):
            team_name = team.get("team", {}).get("displayName", "")
            for inj in team.get("injuries", []):
                athlete = inj.get("athlete", {})
                injuries.append({
                    "player": athlete.get("displayName", ""),
                    "team": team_name,
                    "status": inj.get("status", ""),  # "Out", "Questionable", "Doubtful"
                    "detail": inj.get("details", {}).get("detail", ""),
                    "return_date": inj.get("details", {}).get("returnDate", ""),
                })
        return injuries
    except Exception as e:
        log(f"ESPN injuries error: {e}")
        return []

def fetch_espn_games():
    """Fetch today's NBA games from ESPN."""
    try:
        r = httpx.get(f"{ESPN_BASE}/basketball/nba/scoreboard", timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        games = []
        for event in data.get("events", []):
            comp = event.get("competitions", [{}])[0]
            teams = comp.get("competitors", [])
            game = {
                "game_id": event.get("id", ""),
                "name": event.get("name", ""),
                "date": event.get("date", ""),
                "status": event.get("status", {}).get("type", {}).get("description", ""),
                "home_team": "",
                "away_team": "",
                "home_score": None,
                "away_score": None,
            }
            for t in teams:
                if t.get("homeAway") == "home":
                    game["home_team"] = t.get("team", {}).get("displayName", "")
                    game["home_score"] = t.get("score")
                else:
                    game["away_team"] = t.get("team", {}).get("displayName", "")
                    game["away_score"] = t.get("score")
            games.append(game)
        return games
    except Exception as e:
        log(f"ESPN games error: {e}")
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# CoinGecko — BTC/ETH/SOL prices
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_crypto_prices():
    """Fetch BTC and ETH prices from CoinGecko (free, no key)."""
    try:
        r = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            timeout=10
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        return {
            "btc_usd": data.get("bitcoin", {}).get("usd"),
            "btc_24h_change": round(data.get("bitcoin", {}).get("usd_24h_change", 0), 2),
            "eth_usd": data.get("ethereum", {}).get("usd"),
            "eth_24h_change": round(data.get("ethereum", {}).get("usd_24h_change", 0), 2),
            "sol_usd": data.get("solana", {}).get("usd"),
            "sol_24h_change": round(data.get("solana", {}).get("usd_24h_change", 0), 2),
        }
    except Exception as e:
        log(f"CoinGecko error: {e}")
        return {}

# ═══════════════════════════════════════════════════════════════════════════════
# Metaculus — cross-market probability comparison
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_metaculus_questions(search_terms=None):
    """Fetch top active Metaculus questions for cross-market comparison."""
    try:
        params = {
            "status": "open",
            "order_by": "-activity",
            "limit": 20,
            "type": "forecast",
        }
        if search_terms:
            params["search"] = search_terms
        r = httpx.get("https://www.metaculus.com/api2/questions/", params=params, timeout=10)
        if r.status_code != 200:
            return []
        results = r.json().get("results", [])
        questions = []
        for q in results:
            # community_prediction is a float 0-1 or None
            cp = q.get("community_prediction", {})
            if isinstance(cp, dict):
                prob = cp.get("full", {}).get("q2")  # median
            elif isinstance(cp, (int, float)):
                prob = cp
            else:
                prob = None
            questions.append({
                "id": q.get("id"),
                "title": q.get("title", ""),
                "prob": round(prob * 100, 1) if prob is not None else None,
                "close_time": str(q.get("close_time", ""))[:10],
                "url": f"https://www.metaculus.com/questions/{q.get('id')}/",
            })
        return questions
    except Exception as e:
        log(f"Metaculus error: {e}")
        return []

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

# ── External data feeds ───────────────────────────────────────────────────────
espn_injuries = []
espn_games    = []
crypto_prices = {}
metaculus_qs  = []

try:
    espn_injuries = fetch_espn_injuries()
    log(f"ESPN injuries: {len(espn_injuries)}")
except Exception as e:
    log(f"ESPN injuries error: {e}")

try:
    espn_games = fetch_espn_games()
    log(f"ESPN games today: {len(espn_games)}")
except Exception as e:
    log(f"ESPN games error: {e}")

try:
    crypto_prices = fetch_crypto_prices()
    log(f"Crypto: BTC=${crypto_prices.get('btc_usd','?')}")
except Exception as e:
    log(f"Crypto error: {e}")

try:
    metaculus_qs = fetch_metaculus_questions()
    log(f"Metaculus questions: {len(metaculus_qs)}")
except Exception as e:
    log(f"Metaculus error: {e}")

# ── Vegas vs Kalshi divergences ───────────────────────────────────────────────
edges = []
try:
    vegas_games = fetch_vegas_odds()
    log(f"Total Vegas games: {len(vegas_games)}")
    edges = find_divergences(markets_list, vegas_games)
    log(f"Edge alerts found: {len(edges)}")
    for e in edges:
        log(f"  EDGE {e['gap']:+.1f}¢  {e['ticker']}  Kalshi={e['kalshi_price']}¢ Vegas={e['vegas_prob']}¢  {e['direction']}")
except Exception as e:
    log(f"Odds comparison error: {e}")
    traceback.print_exc(file=sys.stderr)

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
    "edges":         edges,
    "espn_injuries": espn_injuries,
    "espn_games":    espn_games,
    "crypto":        crypto_prices,
    "metaculus":     metaculus_qs,
}, indent=2))
log(f"Saved JSON -> {docs_dir / 'data.json'}")
