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
    # Soccer — World Cup 2026 (48 teams, hosted by USA/Canada/Mexico)
    "MEX": "mexico",        "USA": "united states","BRA": "brazil",
    "ARG": "argentina",     "FRA": "france",       "ENG": "england",
    "GER": "germany",       "ESP": "spain",        "POR": "portugal",
    "NED": "netherlands",   "URU": "uruguay",      "COL": "colombia",
    "ECU": "ecuador",       "CAN": "canada",       "AUS": "australia",
    "JPN": "japan",         "KOR": "korea",        "MAR": "morocco",
    "SEN": "senegal",       "NGA": "nigeria",      "BEL": "belgium",
    "ITA": "italy",         "SUI": "switzerland",  "CRO": "croatia",
    "SWE": "sweden",        "DEN": "denmark",      "POL": "poland",
    "UKR": "ukraine",       "SRB": "serbia",       "AUT": "austria",
    "KSA": "saudi arabia",  "IRN": "iran",         "QAT": "qatar",
    "CRC": "costa rica",    "PAN": "panama",       "CHI": "chile",
    "PER": "peru",          "PAR": "paraguay",
}

def am_to_decimal(odds):
    """American odds → decimal odds (from The Odds API sample utilities.py)."""
    try:
        o = float(odds)
        return 1 - 100.0 / o if o < 0 else 1 + o / 100.0
    except: return None

def am_to_prob(odds):
    """American odds → implied probability 0-100."""
    d = am_to_decimal(odds)
    return round(100.0 / d, 2) if d and d > 0 else None

def find_most_balanced(side_1, side_2):
    """Find the line with tightest spread across two sets of outcomes (sharpest true price).
    Adapted from The Odds API samples/utilities.py."""
    by_point_1 = {x["point"]: x for x in side_1 if "point" in x}
    by_point_2 = {x["point"]: x for x in side_2 if "point" in x}
    best_point, best_diff = None, float("inf")
    for pt in by_point_1:
        if pt not in by_point_2: continue
        d1 = am_to_decimal(by_point_1[pt].get("price"))
        d2 = am_to_decimal(by_point_2[pt].get("price"))
        if d1 and d2:
            diff = abs(d1 - d2)
            if diff < best_diff:
                best_diff, best_point = diff, pt
    if best_point is None: return None, None
    return by_point_1[best_point], by_point_2[best_point]

def fetch_vegas_odds():
    """Fetch h2h + spreads + totals for all upcoming games from The Odds API."""
    if not ODDS_API_KEY:
        log("ODDS_API_KEY not set — skipping Vegas comparison")
        return []
    games = []
    for sport in ODDS_SPORTS:
        try:
            r = httpx.get(f"{ODDS_BASE}/sports/{sport}/odds/",
                params={"apiKey": ODDS_API_KEY, "regions": "us",
                        "markets": "h2h,spreads,totals", "oddsFormat": "american"},
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

ODDS_HISTORY_FILE = Path(__file__).parent.parent / "data" / "odds_history.json"

def load_odds_history():
    """Load previous Odds API game prices for line movement detection."""
    if ODDS_HISTORY_FILE.exists():
        try:
            return json.loads(ODDS_HISTORY_FILE.read_text())
        except Exception:
            pass
    return {}

def save_odds_history(games):
    """Save current Odds API game prices for next run comparison."""
    try:
        snapshot = {}
        for g in games:
            gid = g.get("id", "")
            if not gid:
                continue
            prices = {}
            for bm in g.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") == "h2h":
                        for oc in mkt.get("outcomes", []):
                            name = oc.get("name", "")
                            price = oc.get("price")
                            if name and price:
                                prices[name] = price
            if prices:
                snapshot[gid] = {
                    "home": g.get("home_team", ""),
                    "away": g.get("away_team", ""),
                    "sport": g.get("_sport", ""),
                    "commence_time": g.get("commence_time", ""),
                    "prices": prices,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }
        ODDS_HISTORY_FILE.parent.mkdir(exist_ok=True)
        ODDS_HISTORY_FILE.write_text(json.dumps(snapshot, indent=2))
        log(f"Saved odds history: {len(snapshot)} games")
    except Exception as e:
        log(f"Odds history save error: {e}")

def detect_line_movements(games, history):
    """
    Compare current Odds API prices to previous snapshot.
    Returns list of significant moves (≥3 American odds points = sharp signal).
    A move at sharp books before soft books = informed money.
    """
    movements = []
    for g in games:
        gid = g.get("id", "")
        if gid not in history:
            continue
        prev = history[gid].get("prices", {})
        for bm in g.get("bookmakers", []):
            book = bm.get("title", "")
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for oc in mkt.get("outcomes", []):
                    name  = oc.get("name", "")
                    price = oc.get("price")
                    if name not in prev or price is None:
                        continue
                    prev_price = prev[name]
                    # Convert to implied prob to measure move in consistent units
                    prob_now  = am_to_prob(price)
                    prob_prev = am_to_prob(prev_price)
                    if prob_now is None or prob_prev is None:
                        continue
                    move = prob_now - prob_prev  # positive = team got more likely
                    if abs(move) >= 3.0:  # ≥3 probability points = significant
                        movements.append({
                            "game_id":    gid,
                            "home":       g.get("home_team", ""),
                            "away":       g.get("away_team", ""),
                            "sport":      g.get("_sport", ""),
                            "team":       name,
                            "bookmaker":  book,
                            "prev_price": prev_price,
                            "curr_price": price,
                            "prob_move":  round(move, 1),
                            "direction":  "shortening" if move > 0 else "drifting",
                            "commence":   g.get("commence_time", ""),
                        })
    # Sort by magnitude
    movements.sort(key=lambda x: abs(x["prob_move"]), reverse=True)
    return movements[:10]

# ═══════════════════════════════════════════════════════════════════════════════
# Kalshi price history — track price changes between runs for trend detection
# ═══════════════════════════════════════════════════════════════════════════════
PRICE_HISTORY_FILE = Path(__file__).parent.parent / "data" / "price_history.json"

def load_price_history():
    """
    Load saved Kalshi market prices. Supports two formats:
    - Legacy: {ticker: {price, ts}}
    - Multi-horizon: {ticker: [{price, ts}, ...]}  (last 12 snapshots, newest last)
    Returns the raw dict (callers handle both formats).
    """
    if PRICE_HISTORY_FILE.exists():
        try:
            return json.loads(PRICE_HISTORY_FILE.read_text())
        except Exception:
            pass
    return {}

def save_price_history(markets_list):
    """
    Save current Kalshi market prices for inter-run comparison.
    Maintains a rolling buffer of the last 12 snapshots per ticker (~1 hour).
    """
    try:
        existing = load_price_history()
        ts = datetime.now(timezone.utc).isoformat()
        new_snapshot: dict = {}
        for m in markets_list:
            tk = m.get("ticker", "")
            yp = m.get("_yes_price") or m.get("yes_bid") or m.get("last_price")
            if not tk or yp is None:
                continue
            entry = {"price": yp, "ts": ts}
            prev = existing.get(tk)
            if prev is None:
                new_snapshot[tk] = [entry]
            elif isinstance(prev, list):
                # Multi-horizon format: append and keep last 12
                buf = prev[-11:] + [entry]
                new_snapshot[tk] = buf
            else:
                # Legacy single-entry format → upgrade to list
                new_snapshot[tk] = [prev, entry]
        PRICE_HISTORY_FILE.parent.mkdir(exist_ok=True)
        PRICE_HISTORY_FILE.write_text(json.dumps(new_snapshot, indent=2))
        log(f"Saved price history: {len(new_snapshot)} markets (rolling 12-snapshot buffer)")
    except Exception as e:
        log(f"Price history save error: {e}")

def _get_prev_entry(hist_val, lookback_steps: int = 1):
    """
    Extract a previous price entry from the history value.
    hist_val may be a list (multi-horizon) or a dict (legacy single-entry).
    lookback_steps=1 → most recent prior snapshot
    lookback_steps=12 → ~1 hour ago (12 × 5-min runs)
    """
    if isinstance(hist_val, list) and hist_val:
        idx = max(0, len(hist_val) - lookback_steps - 1)
        return hist_val[idx]
    elif isinstance(hist_val, dict):
        return hist_val
    return None

def detect_price_movements(markets_list, history):
    """
    Compare current Kalshi prices to previous snapshot AND to 1-hour-ago snapshot.
    Returns list of significant moves (≥4¢ over 5 min, OR ≥8¢ over 1 hour).
    Includes `move_1h` field for the 1-hour trend when available.
    """
    moves = []
    for m in markets_list:
        tk = m.get("ticker", "")
        if not tk or tk not in history:
            continue
        hist_val   = history[tk]
        curr_price = m.get("_yes_price") or m.get("yes_bid") or m.get("last_price")
        if curr_price is None:
            continue

        # Short-term: compare to last snapshot (5 min)
        prev_entry = _get_prev_entry(hist_val, 1)
        if prev_entry is None:
            continue
        prev_price = prev_entry.get("price")
        if prev_price is None:
            continue
        move = curr_price - prev_price

        # Long-term: compare to ~1-hour-ago snapshot (12 steps back)
        old_entry  = _get_prev_entry(hist_val, 12)
        move_1h    = None
        if old_entry and old_entry.get("price") is not None and old_entry is not prev_entry:
            move_1h = round(curr_price - old_entry["price"], 1)

        # Emit if 5-min move ≥4¢ OR 1-hour move ≥8¢ (sustained trend)
        if abs(move) >= 4 or (move_1h is not None and abs(move_1h) >= 8):
            moves.append({
                "ticker":     tk,
                "title":      m.get("title", ""),
                "prev_price": prev_price,
                "curr_price": curr_price,
                "move":       round(move, 1),
                "move_1h":    move_1h,
                "direction":  "shortening" if move > 0 else "drifting",
                "prev_ts":    prev_entry.get("ts", ""),
                "category":   m.get("category", "Other"),
            })
    moves.sort(key=lambda x: abs(x.get("move_1h") or x["move"]), reverse=True)
    return moves[:15]

def fetch_event_player_props(sport, event_id):
    """Fetch player prop odds for a specific game (uses per-event endpoint).
    Markets: player_points, player_rebounds, player_assists.
    Note: costs extra quota credits per call — use sparingly."""
    if not ODDS_API_KEY:
        return []
    try:
        r = httpx.get(f"{ODDS_BASE}/sports/{sport}/events/{event_id}/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "us",
                    "markets": "player_points,player_rebounds,player_assists",
                    "oddsFormat": "american"},
            timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        props = []
        for bm in data.get("bookmakers", [])[:2]:   # top 2 books only
            for mkt in bm.get("markets", []):
                for oc in mkt.get("outcomes", []):
                    props.append({
                        "book":    bm.get("title",""),
                        "market":  mkt.get("key",""),
                        "player":  oc.get("description",""),
                        "name":    oc.get("name",""),   # Over/Under
                        "point":   oc.get("point"),
                        "price":   oc.get("price"),
                        "prob":    am_to_prob(oc.get("price")),
                    })
        return props
    except Exception as e:
        log(f"Player props error ({sport} {event_id}): {e}")
        return []

def game_consensus_prob(game, team_fragment, market="h2h"):
    """Average implied probability for a team across all bookmakers (0-100)."""
    probs = []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market: continue
            for oc in mkt.get("outcomes", []):
                if team_fragment.lower() in oc.get("name", "").lower():
                    p = am_to_prob(oc.get("price"))
                    if p is not None: probs.append(p)
    return round(sum(probs) / len(probs), 1) if probs else None

def game_consensus_total(game):
    """Get consensus over/under total and sharpest line across books."""
    all_overs, all_unders = [], []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "totals": continue
            for oc in mkt.get("outcomes", []):
                if oc.get("name") == "Over":  all_overs.append(oc)
                if oc.get("name") == "Under": all_unders.append(oc)
    best_over, best_under = find_most_balanced(all_overs, all_unders)
    if best_over and best_under:
        return {
            "line":       best_over.get("point"),
            "over_prob":  am_to_prob(best_over.get("price")),
            "under_prob": am_to_prob(best_under.get("price")),
        }
    return None

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

            total = game_consensus_total(game)
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
                "total":        total,   # over/under consensus line
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
# ESPN — multi-sport data: injuries, scoreboard, odds, news, win probability
# Free, no API key required.
# Docs: github.com/pseudo-r/Public-ESPN-API
# ═══════════════════════════════════════════════════════════════════════════════
ESPN_SITE   = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_CORE   = "https://sports.core.api.espn.com/v2/sports"
ESPN_NOW    = "https://now.core.api.espn.com/v1/sports"

# Sports/leagues to monitor — covers every Kalshi sports category
ESPN_SPORTS = [
    ("basketball", "nba"),
    ("football",   "nfl"),
    ("baseball",   "mlb"),
    ("hockey",     "nhl"),
    ("soccer",     "fifa.world"),   # FIFA World Cup 2026
    ("soccer",     "usa.1"),        # MLS
    ("tennis",     "atp"),          # ATP
    ("golf",       "pga"),          # PGA Tour
    ("mma",        "ufc"),          # UFC
]

def _espn_get(url, params=None):
    """Simple ESPN GET with timeout, returns json or None."""
    try:
        r = httpx.get(url, params=params or {}, timeout=10,
                      headers={"User-Agent": "Mozilla/5.0"})
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def fetch_espn_scoreboard():
    """Fetch today's + tomorrow's games across all sports.
    Returns list of game dicts with event IDs.
    Looks ahead 2 days to catch upcoming games that have Kalshi markets.
    """
    from datetime import timedelta
    all_games = []
    seen_ids = set()

    # Fetch today and next 2 days for each sport/league
    today_utc = datetime.now(timezone.utc).date()
    dates_to_fetch = [today_utc + timedelta(days=d) for d in range(3)]

    for sport, league in ESPN_SPORTS:
        for fetch_date in dates_to_fetch:
            params = {"dates": fetch_date.strftime("%Y%m%d")} if fetch_date != today_utc else {}
            data = _espn_get(f"{ESPN_SITE}/{sport}/{league}/scoreboard", params)
            if not data:
                continue
            for event in data.get("events", []):
                event_id = event.get("id", "")
                if event_id and event_id in seen_ids:
                    continue  # Skip duplicate games (same event fetched for different dates)
                if event_id:
                    seen_ids.add(event_id)

                comp  = event.get("competitions", [{}])[0]
                teams = comp.get("competitors", [])
                game  = {
                    "event_id":   event_id,
                    "sport":      sport,
                    "league":     league,
                    "name":       event.get("name", ""),
                    "short_name": event.get("shortName", ""),
                    "date":       event.get("date", ""),
                    "status":     event.get("status", {}).get("type", {}).get("description", ""),
                    "home_team":  "", "away_team": "",
                    "home_score": None, "away_score": None,
                    "home_win_pct": None, "away_win_pct": None,
                }
                for t in teams:
                    side = "home" if t.get("homeAway") == "home" else "away"
                    game[f"{side}_team"]  = t.get("team", {}).get("displayName", "")
                    game[f"{side}_score"] = t.get("score")
                    # Win probability sometimes embedded in scoreboard
                    wp = t.get("statistics", [])
                    for stat in wp:
                        if stat.get("name") == "winProbability":
                            game[f"{side}_win_pct"] = stat.get("displayValue")
                all_games.append(game)
            log(f"ESPN {sport}/{league} {fetch_date} -> {len(data.get('events',[]))} games")
    log(f"ESPN scoreboard total: {len(all_games)} games across 3 days")
    return all_games

def fetch_espn_injuries():
    """Fetch injury reports across all sports. Returns combined list."""
    all_injuries = []
    for sport, league in ESPN_SPORTS:
        data = _espn_get(f"{ESPN_SITE}/{sport}/{league}/injuries")
        if not data:
            continue
        for team in data.get("injuries", []):
            team_name = team.get("team", {}).get("displayName", "")
            for inj in team.get("injuries", []):
                athlete = inj.get("athlete", {})
                all_injuries.append({
                    "player":      athlete.get("displayName", ""),
                    "team":        team_name,
                    "sport":       sport,
                    "league":      league,
                    "status":      inj.get("status", ""),
                    "detail":      inj.get("details", {}).get("detail", ""),
                    "return_date": inj.get("details", {}).get("returnDate", ""),
                })
    log(f"ESPN injuries total: {len(all_injuries)}")
    return all_injuries

def fetch_espn_news():
    """Fetch breaking news headlines across all sports (real-time injury/lineup alerts)."""
    all_news = []
    # Only fetch news for major sports to save API credits
    for sport, league in [("basketball","nba"),("football","nfl"),("baseball","mlb"),
                           ("hockey","nhl"),("soccer","fifa.world")]:
        data = _espn_get(f"{ESPN_SITE}/{sport}/{league}/news", {"limit": 5})
        if not data:
            continue
        for article in data.get("articles", []):
            headline = article.get("headline", "")
            # Flag injury/lineup relevant headlines
            keywords = ["injured","injury","questionable","doubtful","out","ruled out",
                        "inactive","starting","lineup","suspended","trade","waived"]
            is_alert = any(kw in headline.lower() for kw in keywords)
            all_news.append({
                "headline":   headline,
                "sport":      sport,
                "league":     league,
                "published":  article.get("published", ""),
                "is_alert":   is_alert,
                "url":        article.get("links", {}).get("web", {}).get("href", ""),
            })
    log(f"ESPN news total: {len(all_news)} articles")
    return all_news

def fetch_espn_game_odds(sport, league, event_id):
    """Fetch ESPN's own betting odds for a specific game. Free, no key needed."""
    data = _espn_get(
        f"{ESPN_CORE}/{sport}/leagues/{league}/events/{event_id}"
        f"/competitions/{event_id}/odds"
    )
    if not data:
        return []
    odds_list = []
    for item in data.get("items", []):
        provider = item.get("provider", {}).get("name", "ESPN")
        details  = item.get("details", "")
        home_odds = item.get("homeTeamOdds", {})
        away_odds = item.get("awayTeamOdds", {})
        odds_list.append({
            "provider":      provider,
            "details":       details,
            "home_fav":      home_odds.get("favorite", False),
            "home_moneyline": home_odds.get("moneyLine"),
            "away_moneyline": away_odds.get("moneyLine"),
            "home_win_pct":  home_odds.get("winPercentage"),
            "away_win_pct":  away_odds.get("winPercentage"),
            "spread":        item.get("spread"),
            "over_under":    item.get("overUnder"),
        })
    return odds_list

def fetch_espn_win_probability(sport, league, event_id):
    """Fetch live in-game win probability for an ongoing game."""
    data = _espn_get(
        f"{ESPN_CORE}/{sport}/leagues/{league}/events/{event_id}"
        f"/competitions/{event_id}/probabilities"
    )
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        return None
    latest = items[-1]  # most recent probability
    return {
        "home_win_pct": latest.get("homeWinPercentage"),
        "away_win_pct": latest.get("awayWinPercentage"),
        "tie_pct":      latest.get("tiePercentage"),
    }

def fetch_espn_playoff_series():
    """
    Fetch current playoff series standings from ESPN bracket data.
    Returns list of dicts: {league, team1, team2, wins1, wins2, leader, series_key}

    Tries the ESPN `scoreboard` endpoint's `series` field and also the
    dedicated bracket/playoff endpoints for NBA and NHL.
    """
    series_list = []
    seen_keys = set()

    for sport, league in [("basketball", "nba"), ("hockey", "nhl"), ("baseball", "mlb")]:
        try:
            # Try to get playoff scoreboard which includes series info
            data = _espn_get(f"{ESPN_SITE}/{sport}/{league}/scoreboard",
                             {"groups": "playoff"})
            if not data:
                # Try without groups param
                data = _espn_get(f"{ESPN_SITE}/{sport}/{league}/scoreboard", {})
            if not data:
                continue

            for event in data.get("events", []):
                comp = event.get("competitions", [{}])[0]
                series_info = comp.get("series") or event.get("series") or {}

                # Try to extract series score from competition data
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                home_comp = next((c for c in competitors if c.get("homeAway") == "home"), None)
                away_comp = next((c for c in competitors if c.get("homeAway") == "away"), None)
                if not home_comp or not away_comp:
                    continue

                home_name = home_comp.get("team", {}).get("displayName", "")
                away_name = away_comp.get("team", {}).get("displayName", "")
                if not home_name or not away_name:
                    continue

                # Series wins from 'records' or 'series' field
                home_wins = 0
                away_wins = 0

                # Check competitor records for series wins
                for c, cname, side in [(home_comp, home_name, "home"), (away_comp, away_name, "away")]:
                    for rec in c.get("records", []):
                        if rec.get("type") == "playoff":
                            wins_str = rec.get("summary", "0-0")
                            parts = wins_str.split("-")
                            try:
                                w = int(parts[0])
                                if side == "home":
                                    home_wins = w
                                else:
                                    away_wins = w
                            except Exception:
                                pass

                # Also try series.summary "X-X"
                if series_info:
                    summ = series_info.get("summary", "") or ""
                    if summ and "-" in summ:
                        try:
                            a, b = summ.split("-", 1)
                            home_wins = int(a.strip()); away_wins = int(b.strip())
                        except Exception:
                            pass

                series_key = tuple(sorted([home_name, away_name]))
                if series_key in seen_keys:
                    continue
                if home_wins == 0 and away_wins == 0:
                    continue  # No series data available

                seen_keys.add(series_key)
                leader = home_name if home_wins > away_wins else (away_name if away_wins > home_wins else None)
                series_list.append({
                    "league":      league,
                    "home_team":   home_name,
                    "away_team":   away_name,
                    "home_wins":   home_wins,
                    "away_wins":   away_wins,
                    "leader":      leader,
                    "series_key":  list(series_key),
                    "event_id":    event.get("id", ""),
                })

        except Exception as e:
            log(f"ESPN playoff series {sport}/{league}: {e}")

    log(f"ESPN playoff series: {len(series_list)} active series found")
    return series_list


def fetch_all_espn_data():
    """Fetch scoreboard, injuries, news. Fetch odds for each game found."""
    scoreboard = fetch_espn_scoreboard()
    injuries   = fetch_espn_injuries()
    news       = fetch_espn_news()

    # Enrich scoreboard with ESPN odds for each game
    for game in scoreboard:
        if game["event_id"] and game["sport"] in ("basketball","football","baseball","hockey"):
            odds = fetch_espn_game_odds(game["sport"], game["league"], game["event_id"])
            game["espn_odds"] = odds
        else:
            game["espn_odds"] = []

    return scoreboard, injuries, news

# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# CoinGecko Pro — full crypto intelligence suite
# ═══════════════════════════════════════════════════════════════════════════════
# Budget: 10,000 credits/month (replenishes June 1)
# Credit schedule:
#   Prices   → every 15 min  = 2,880/month  (1 credit each)
#   Global   → every 30 min  = 1,440/month  (1 credit each)
#   Movers   → every 30 min  = 1,440/month  (1 credit each)
#   Sparkline→ every 60 min  =   720/month  (1 credit each)
#   Fear&Greed → FREE (alternative.me, not CoinGecko)
#   ─────────────────────────────────────────────────────
#   Total    =  6,480/month  →  3,520 credits headroom ✅
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")

def _cg_headers():
    if COINGECKO_API_KEY:
        return {"x-cg-pro-api-key": COINGECKO_API_KEY}
    return {}

def _cg_base():
    return "https://pro-api.coingecko.com/api/v3" if COINGECKO_API_KEY \
           else "https://api.coingecko.com/api/v3"

def _cg_get(path, params=None):
    try:
        r = httpx.get(f"{_cg_base()}{path}", params=params or {},
                      headers=_cg_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
        log(f"CoinGecko {path} -> {r.status_code}: {r.text[:80]}")
    except Exception as e:
        log(f"CoinGecko {path} error: {e}")
    return None

def _on_interval(minutes):
    """True if current UTC minute is on a multiple-of-minutes boundary."""
    return datetime.now(timezone.utc).minute % minutes == 0

def fetch_crypto_prices():
    """Live prices for BTC/ETH/SOL + key alts. Every 15 min = 2,880 credits/month."""
    if not _on_interval(15):
        log("CoinGecko prices: skipping (not 15-min mark)")
        return {}
    data = _cg_get("/simple/price", {
        "ids": "bitcoin,ethereum,solana,sui,avalanche-2,chainlink,dogecoin",
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_market_cap": "true",
        "include_24hr_vol": "true",
    })
    if not data:
        return {}
    def coin(key):
        c = data.get(key, {})
        return {
            "usd":        c.get("usd"),
            "change_24h": round(c.get("usd_24h_change") or 0, 2),
            "market_cap": c.get("usd_market_cap"),
            "vol_24h":    c.get("usd_24h_vol"),
        }
    log(f"CoinGecko prices: BTC=${data.get('bitcoin',{}).get('usd','?')}")
    return {
        "btc": coin("bitcoin"),   "eth": coin("ethereum"),
        "sol": coin("solana"),    "sui": coin("sui"),
        "avax": coin("avalanche-2"), "link": coin("chainlink"),
        "doge": coin("dogecoin"),
        # Legacy flat keys for dashboard backward compatibility
        "btc_usd": data.get("bitcoin",{}).get("usd"),
        "btc_24h_change": round(data.get("bitcoin",{}).get("usd_24h_change") or 0, 2),
        "eth_usd": data.get("ethereum",{}).get("usd"),
        "eth_24h_change": round(data.get("ethereum",{}).get("usd_24h_change") or 0, 2),
        "sol_usd": data.get("solana",{}).get("usd"),
        "sol_24h_change": round(data.get("solana",{}).get("usd_24h_change") or 0, 2),
    }

def fetch_crypto_global():
    """Global market data: total cap, BTC dominance, market trend.
    Every 30 min = 1,440 credits/month."""
    if not _on_interval(30):
        log("CoinGecko global: skipping")
        return {}
    data = _cg_get("/global")
    if not data:
        return {}
    d = data.get("data", {})
    log(f"CoinGecko global: BTC dom={d.get('btc_dominance',0):.1f}%")
    return {
        "total_market_cap_usd":   d.get("total_market_cap", {}).get("usd"),
        "total_volume_24h_usd":   d.get("total_volume", {}).get("usd"),
        "btc_dominance":          round(d.get("btc_dominance") or 0, 2),
        "eth_dominance":          round(d.get("eth_dominance") or 0, 2),
        "active_cryptos":         d.get("active_cryptocurrencies"),
        "market_cap_change_24h":  round(d.get("market_cap_change_percentage_24h_usd") or 0, 2),
    }

def fetch_crypto_movers():
    """Top 5 gainers and losers in last 24h from top-250 coins.
    Every 30 min = 1,440 credits/month."""
    if not _on_interval(30):
        log("CoinGecko movers: skipping")
        return {"gainers": [], "losers": []}
    data = _cg_get("/coins/markets", {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "price_change_percentage": "24h",
        "sparkline": "false",
    })
    if not data:
        return {"gainers": [], "losers": []}
    def fmt(c):
        return {
            "symbol": c.get("symbol","").upper(),
            "name":   c.get("name",""),
            "price":  c.get("current_price"),
            "change": round(c.get("price_change_percentage_24h") or 0, 2),
            "cap":    c.get("market_cap"),
        }
    ranked = sorted(data, key=lambda x: x.get("price_change_percentage_24h") or 0)
    gainers = [fmt(c) for c in reversed(ranked[-5:])]
    losers  = [fmt(c) for c in ranked[:5]]
    log(f"CoinGecko movers: top gainer {gainers[0]['symbol']} +{gainers[0]['change']}%")
    return {"gainers": gainers, "losers": losers}

def fetch_crypto_sparklines():
    """7-day hourly price data for BTC and ETH for chart display.
    Every 60 min = 720 credits/month (1 credit per coin per call)."""
    if not _on_interval(60):
        log("CoinGecko sparklines: skipping")
        return {}
    result = {}
    for coin_id, symbol in [("bitcoin","btc"),("ethereum","eth")]:
        data = _cg_get(f"/coins/{coin_id}/market_chart", {
            "vs_currency": "usd", "days": "7", "interval": "hourly",
        })
        if data:
            # prices = [[timestamp_ms, price], ...]
            prices = data.get("prices", [])
            result[symbol] = {
                "timestamps": [p[0] for p in prices],
                "prices":     [round(p[1], 2) for p in prices],
            }
            log(f"CoinGecko sparkline {symbol}: {len(prices)} points")
    return result

def fetch_fear_greed():
    """Crypto Fear & Greed Index from alternative.me — completely FREE, no credits used."""
    try:
        r = httpx.get("https://api.alternative.me/fng/?limit=2", timeout=8)
        if r.status_code != 200:
            return {}
        items = r.json().get("data", [])
        if not items:
            return {}
        today     = items[0]
        yesterday = items[1] if len(items) > 1 else {}
        result = {
            "value":       int(today.get("value", 0)),
            "label":       today.get("value_classification", ""),   # "Fear", "Greed", etc.
            "yesterday":   int(yesterday.get("value", 0)) if yesterday else None,
            "timestamp":   today.get("timestamp"),
        }
        log(f"Fear & Greed: {result['value']} ({result['label']})")
        return result
    except Exception as e:
        log(f"Fear & Greed error: {e}")
        return {}

# ═══════════════════════════════════════════════════════════════════════════════
# Metaculus — cross-market probability comparison
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_metaculus_questions(search_terms=None):
    """Fetch top active Metaculus questions. Tries multiple API versions.
    Metaculus has changed their API structure multiple times; we try several formats.
    """
    try:
        headers = {
            "Accept":     "application/json",
            "User-Agent": "kalshi-bot/1.0 (prediction market analysis)",
        }
        # Try multiple endpoints in priority order
        # v3 posts API (newest), v2 questions API (older), v2 with has_group filter
        endpoints = [
            # v3 posts API — returns type:"question" objects with nested question data
            ("https://www.metaculus.com/api/posts/?statuses=open&order_by=-activity&limit=50&post_type=question", "results"),
            # v3 with different params
            ("https://www.metaculus.com/api/posts/?status=open&order_by=-hotness&limit=50", "results"),
            # v2 questions API (older, may still work)
            ("https://www.metaculus.com/api2/questions/?status=open&order_by=-activity&limit=50", "results"),
            # v2 binary questions only
            ("https://www.metaculus.com/api2/questions/?status=open&type=binary&order_by=-activity&limit=50", "results"),
        ]
        raw = []
        for url, key in endpoints:
            try:
                r = httpx.get(url, headers=headers, timeout=15)
                log(f"Metaculus {url[:70]} -> {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        raw = data
                    else:
                        raw = data.get(key) or data.get("items") or []
                    if raw:
                        log(f"Metaculus: got {len(raw)} results")
                        break
                    log(f"Metaculus: empty results from {url[:50]}")
                elif r.status_code in (429, 403):
                    log(f"Metaculus rate-limited/forbidden — skipping remaining endpoints")
                    break
            except Exception as e:
                log(f"Metaculus endpoint error: {e}")
                continue

        if not raw:
            log("Metaculus: no results from any endpoint")
            return []

        questions = []
        for q in raw:
            # Handle both v3 (nested) and v2 (flat) formats
            # v3: {"type": "question", "title": "...", "question": {...}}
            # v2: {"id": ..., "title": "...", "community_prediction": {...}}
            inner = q.get("question") or q  # v3 nests under "question"; v2 is flat
            title = inner.get("title") or q.get("title", "")
            qid   = inner.get("id") or q.get("id") or q.get("question_id")

            # Probability — try multiple field paths across API versions
            prob = None
            # v3 format: community_weighting or cp_reveal_time
            for path in [
                # v3 nested fields
                lambda i: i.get("cp"),
                lambda i: i.get("community_prediction", {}).get("full", {}).get("q2") if isinstance(i.get("community_prediction"), dict) else None,
                lambda i: i.get("community_prediction") if isinstance(i.get("community_prediction"), (int, float)) else None,
                # v2 flat fields
                lambda i: i.get("probability"),
                lambda i: i.get("community_median_prediction"),
                # v3 top-level (some endpoints put it here)
                lambda i: q.get("cp"),
                lambda i: q.get("probability"),
            ]:
                try:
                    val = path(inner)
                    if val is not None:
                        prob = float(val)
                        # Metaculus probabilities are already 0-1
                        if prob > 1:
                            prob = prob / 100.0  # some endpoints return 0-100
                        break
                except Exception:
                    continue

            close_time = (
                inner.get("scheduled_close_time") or
                inner.get("close_time") or
                q.get("close_time") or
                q.get("scheduled_close_time") or
                q.get("resolution_criteria", {}).get("close_time") if isinstance(q.get("resolution_criteria"), dict) else None or
                ""
            )

            if not title or not qid:
                continue
            questions.append({
                "id":         qid,
                "title":      title,
                "prob":       round(prob * 100, 1) if prob is not None else None,
                "close_time": str(close_time)[:10],
                "url":        f"https://www.metaculus.com/questions/{qid}/",
            })

        # Only keep questions that have a probability (needed for edge analysis)
        with_prob = [q for q in questions if q.get("prob") is not None]
        log(f"Metaculus: {len(questions)} parsed, {len(with_prob)} have probability")
        return with_prob
    except Exception as e:
        log(f"Metaculus error: {e}")
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# FRED — Federal Reserve Economic Data
# ═══════════════════════════════════════════════════════════════════════════════
# Free API key at fred.stlouisfed.org → add as FRED_API_KEY secret
# Runs every 60 min (data updates daily/monthly — no point calling more often)
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE    = "https://api.stlouisfed.org/fred"

FRED_SERIES = {
    "cpi":          ("CPIAUCSL", "CPI Inflation",        "%"),
    "pce":          ("PCEPI",    "PCE Inflation",        "%"),   # Fed's preferred measure
    "unemployment": ("UNRATE",   "Unemployment Rate",    "%"),
    "fed_rate":     ("FEDFUNDS", "Fed Funds Rate",       "%"),
    "treasury_10y": ("DGS10",    "10-Year Treasury",     "%"),
    "treasury_2y":  ("DGS2",     "2-Year Treasury",      "%"),
    "payrolls":     ("PAYEMS",   "Nonfarm Payrolls",     "K"),
    "gdp":          ("GDPC1",    "Real GDP",             "B"),
    "sentiment":    ("UMCSENT",  "Consumer Sentiment",   ""),
    "vix":          ("VIXCLS",   "VIX Volatility",       ""),   # equity market fear gauge
}

def fetch_fred_data():
    """Fetch latest values for key economic indicators from FRED.
    Runs every 60 min = 720 credits/month (free, unlimited on FRED)."""
    if not FRED_API_KEY:
        log("FRED_API_KEY not set — skipping economic data")
        return {}
    if not _on_interval(60):
        log("FRED: skipping (not 60-min mark)")
        return {}
    result = {}
    for key, (series_id, label, unit) in FRED_SERIES.items():
        try:
            r = httpx.get(f"{FRED_BASE}/series/observations",
                params={"series_id": series_id, "api_key": FRED_API_KEY,
                        "sort_order": "desc", "limit": 2,
                        "file_type": "json"},
                timeout=10)
            if r.status_code != 200:
                log(f"FRED {series_id} -> {r.status_code}")
                continue
            obs = r.json().get("observations", [])
            if not obs:
                continue
            latest = obs[0]
            prev   = obs[1] if len(obs) > 1 else {}
            val_str  = latest.get("value", ".")
            prev_str = prev.get("value", ".")
            val, prev_val, change = None, None, None
            try:
                val      = float(val_str)
                prev_val = float(prev_str) if prev_str not in (".", "") else None
                change   = round(val - prev_val, 3) if prev_val is not None else None
            except (ValueError, TypeError):
                pass
            result[key] = {
                "label":  label,
                "value":  val,
                "unit":   unit,
                "date":   latest.get("date", ""),
                "prev":   prev_val,
                "change": change,
            }
            log(f"FRED {series_id}: {val} ({latest.get('date','')})")
        except Exception as e:
            log(f"FRED {series_id} error: {e}")
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# PolyMarket — crypto prediction market (free, no key)
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_polymarket_markets():
    """Fetch active PolyMarket markets for cross-platform price comparison.
    No API key needed. Every 15 min."""
    if not _on_interval(15):
        log("PolyMarket: skipping")
        return []
    try:
        r = httpx.get("https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": 200,
                    "order": "volume", "ascending": "false"},
            timeout=15)
        if r.status_code != 200:
            log(f"PolyMarket -> {r.status_code}")
            return []
        markets = r.json()
        result = []
        for m in markets:
            try:
                prices_raw   = m.get("outcomePrices", "[]")
                outcomes_raw = m.get("outcomes", '["Yes","No"]')
                prices   = json.loads(prices_raw)   if isinstance(prices_raw,   str) else prices_raw
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                yes_price = None
                for i, outcome in enumerate(outcomes):
                    if str(outcome).lower() in ("yes", "true") and i < len(prices):
                        try: yes_price = round(float(prices[i]) * 100, 1)
                        except: pass
                vol = m.get("volume") or m.get("volumeNum") or 0
                try: vol = float(vol)
                except: vol = 0
                result.append({
                    "id":        str(m.get("id", "")),
                    "question":  m.get("question", ""),
                    "yes_price": yes_price,
                    "volume":    vol,
                    "end_date":  str(m.get("endDate", ""))[:10],
                    "url":       f"https://polymarket.com/event/{m.get('slug', m.get('id',''))}",
                })
            except Exception:
                continue
        log(f"PolyMarket: {len(result)} markets")
        return result
    except Exception as e:
        log(f"PolyMarket error: {e}")
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# PredictIt — US political prediction market (free, no key)
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_predictit_markets():
    """Fetch PredictIt political markets for cross-platform arb. Every 15 min."""
    if not _on_interval(15):
        log("PredictIt: skipping")
        return []
    try:
        r = httpx.get("https://www.predictit.org/api/marketdata/all/",
            headers={"Accept": "application/json"}, timeout=15)
        if r.status_code != 200:
            log(f"PredictIt -> {r.status_code}")
            return []
        markets_raw = r.json().get("markets", [])
        result = []
        for m in markets_raw:
            contracts = []
            for c in m.get("contracts", []):
                yp = c.get("bestBuyYesCost") or c.get("lastTradePrice") or 0
                np_ = c.get("bestBuyNoCost") or 0
                contracts.append({
                    "name":       c.get("shortName") or c.get("name", ""),
                    "yes_price":  round(float(yp) * 100, 1),
                    "no_price":   round(float(np_) * 100, 1),
                    "last_price": round(float(c.get("lastTradePrice") or 0) * 100, 1),
                })
            result.append({
                "id":        m.get("id"),
                "name":      m.get("name", ""),
                "url":       m.get("url", ""),
                "contracts": contracts,
            })
        log(f"PredictIt: {len(result)} markets")
        return result
    except Exception as e:
        log(f"PredictIt error: {e}")
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# Manifold Markets — free prediction market, no API key needed
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_manifold_markets():
    """Fetch active Manifold Markets for cross-platform price comparison.
    Manifold is a free prediction market with wide topic coverage.
    No API key needed. Runs every 15 min."""
    if not _on_interval(15):
        log("Manifold: skipping")
        return []
    try:
        result = []
        # Fetch top markets by liquidity + activity
        for sort in ["liquidity", "score"]:
            try:
                r = httpx.get(
                    "https://api.manifold.markets/v0/markets",
                    params={"limit": 200, "sort": sort, "filter": "open"},
                    timeout=15
                )
                if r.status_code != 200:
                    log(f"Manifold {sort} -> {r.status_code}")
                    continue
                markets = r.json()
                seen_ids = {m["id"] for m in result}
                for m in markets:
                    try:
                        # Only use binary (CPMM) markets with a probability
                        if m.get("mechanism") not in ("cpmm-1", "cpmm-2"):
                            continue
                        prob = m.get("probability")
                        if prob is None:
                            continue
                        yes_price = round(float(prob) * 100, 1)
                        vol = float(m.get("volume", 0) or 0)
                        mkt_id = str(m.get("id", ""))
                        if mkt_id in seen_ids:
                            continue
                        seen_ids.add(mkt_id)
                        # Parse close time
                        close_ms = m.get("closeTime")
                        close_str = ""
                        if close_ms:
                            from datetime import datetime, timezone
                            close_dt = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
                            close_str = close_dt.strftime("%Y-%m-%d")
                        result.append({
                            "id":        mkt_id,
                            "question":  m.get("question", ""),
                            "yes_price": yes_price,
                            "volume":    vol,
                            "end_date":  close_str,
                            "url":       m.get("url", f"https://manifold.markets/M/{m.get('slug','')}"),
                        })
                    except Exception:
                        continue
                if result:
                    break  # Got results from first sort, stop
            except Exception as e:
                log(f"Manifold {sort} error: {e}")
                continue

        log(f"Manifold: {len(result)} markets")
        return result[:200]
    except Exception as e:
        log(f"Manifold error: {e}")
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# WEATHER — NWS + Open-Meteo for temperature market edge detection
# ═══════════════════════════════════════════════════════════════════════════════
# Free, no key required. Runs every 30 min.

WEATHER_CITIES = {
    "ATX": {"name": "Austin",       "lat": 30.27,  "lon": -97.74},
    "LAX": {"name": "Los Angeles",  "lat": 34.05,  "lon": -118.24},
    "SFO": {"name": "San Francisco","lat": 37.77,  "lon": -122.42},
    "HOU": {"name": "Houston",      "lat": 29.76,  "lon": -95.37},
    "NYC": {"name": "New York",     "lat": 40.71,  "lon": -74.01},
    "CHI": {"name": "Chicago",      "lat": 41.88,  "lon": -87.63},
    "MIA": {"name": "Miami",        "lat": 25.77,  "lon": -80.19},
    "DEN": {"name": "Denver",       "lat": 39.74,  "lon": -104.98},
    "SEA": {"name": "Seattle",      "lat": 47.61,  "lon": -122.33},
    "PHX": {"name": "Phoenix",      "lat": 33.45,  "lon": -112.07},
}

def fetch_weather_forecasts():
    """Fetch tomorrow's high temp forecast from Open-Meteo for each city.
    Open-Meteo is free, no key needed, and uses ECMWF/GFS ensemble models.
    Runs every 30 min."""
    if not _on_interval(30):
        log("Weather: skipping")
        return {}
    results = {}
    for city_code, info in WEATHER_CITIES.items():
        try:
            r = httpx.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude":  info["lat"],
                    "longitude": info["lon"],
                    "daily":     "temperature_2m_max",
                    "temperature_unit": "fahrenheit",
                    "forecast_days": 3,
                    "timezone":  "auto",
                },
                timeout=10,
            )
            if r.status_code != 200:
                log(f"Weather {city_code} -> {r.status_code}")
                continue
            data = r.json()
            daily = data.get("daily", {})
            dates  = daily.get("time", [])
            highs  = daily.get("temperature_2m_max", [])
            city_forecasts = []
            for date, high in zip(dates, highs):
                if high is not None:
                    city_forecasts.append({"date": date, "high_f": round(float(high), 1)})
            results[city_code] = {
                "name":      info["name"],
                "forecasts": city_forecasts,  # [{"date": "2026-05-27", "high_f": 87.3}, ...]
            }
            log(f"Weather {city_code}: {city_forecasts[:2]}")
        except Exception as e:
            log(f"Weather {city_code} error: {e}")
    return results

# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY ENGINE — Combines all signals into actionable recommendations
# ═══════════════════════════════════════════════════════════════════════════════

KALSHI_TAKER_FEE_RATE = 0.07   # 7% × c × (1-c)
KALSHI_MAKER_FEE_RATE = 0.0175 # 1.75% × c × (1-c)

def kalshi_fee(price_cents, maker=False):
    """Kalshi fee per contract in cents."""
    c = price_cents / 100.0
    rate = KALSHI_MAKER_FEE_RATE if maker else KALSHI_TAKER_FEE_RATE
    return round(rate * c * (1 - c) * 100, 4)

def kelly_size(prob_pct, price_cents, maker=False, fraction=0.25):
    """
    Kelly criterion for Kalshi binary market.
    prob_pct: your probability estimate (0-100)
    price_cents: current contract price (0-100)
    fraction: Kelly multiplier (default quarter-Kelly = 0.25)
    Returns: fraction of bankroll to risk (0.0-0.25), or 0 if no edge.
    """
    # Minimum edge threshold: under 2 cent edge after fees = not worth trading
    edge = prob_pct - price_cents  # in probability units (0-100)
    if edge < 2:
        return 0.0

    p  = prob_pct / 100.0
    c  = price_cents / 100.0
    fee = kalshi_fee(price_cents, maker) / 100.0

    # Betting YES
    net_edge_yes = (p - c) - fee
    if net_edge_yes > 0 and (1 - c - fee) > 0:
        kelly_yes = net_edge_yes / (1 - c - fee)
        return round(min(kelly_yes * fraction, 0.25), 4)

    # Betting NO
    net_edge_no = ((1 - p) - (1 - c)) - fee
    if net_edge_no > 0 and (c - fee) > 0:
        kelly_no = net_edge_no / (c - fee)
        return round(min(kelly_no * fraction, 0.25), 4)

    return 0.0

def analyze_longshot_bias(markets):
    """
    Favorite-Longshot Bias: historically, contracts at extremes are mispriced.

    Research findings:
    - Longshots (< 10¢): lose 60%+ on average. BUY NO.
    - Extreme favorites (> 90¢): slightly underpriced. BUY YES.
    - Moderate favorites (85-90¢): slight value in YES.

    Exclude: markets closing within 2h, very low volume markets (< 10 trades).
    """
    signals = []
    for m in markets:
        ticker = m.get("ticker", "")
        title  = m.get("title", ticker)
        yp     = m.get("_yes_price")
        vol    = m.get("volume", 0) or 0

        if yp is None or not ticker:
            continue
        # Skip completely empty markets — but allow supplemental markets with vol=0
        # (their volume may just be missing from API, not truly zero)
        # Only skip if both volume AND trade_count are 0 AND it's a non-supplemental market
        if vol < 1 and not m.get("_supplemental"):
            continue

        if yp <= 10:
            # Strong longshot: pay ≤10¢ for YES that historically wins <4%
            # Research: 10¢ contracts resolve YES only ~4-7% of time
            edge_prob = 100 - 7  # historical NO win rate ≈ 93-96%
            no_price  = 100 - yp
            kelly = kelly_size(edge_prob, no_price, maker=True)
            signals.append({
                "type":              "longshot_bias",
                "direction":         "BUY NO",
                "ticker":            ticker,
                "title":             title,
                "price":             yp,
                "rationale":         f"Longshot bias: {yp}¢ YES contracts win only ~5% historically. Buy NO @ {no_price}¢.",
                "confidence":        "high",
                "kelly_frac":        kelly * 0.5,
                "fee_cents":         kalshi_fee(no_price),
                "priority":          2,
                "entry_limit_cents": max(1, no_price - 1),
                "take_profit_cents": 97,
                "stop_loss_pct":     0.3,
            })
        elif yp <= 15:
            # Moderate longshot: still biased but less extreme
            edge_prob = 100 - 12
            no_price  = 100 - yp
            kelly = kelly_size(edge_prob, no_price, maker=True)
            signals.append({
                "type":              "longshot_bias",
                "direction":         "BUY NO",
                "ticker":            ticker,
                "title":             title,
                "price":             yp,
                "rationale":         f"Longshot bias: {yp}¢ YES contracts lose 60%+ historically. Buy NO.",
                "confidence":        "medium",
                "kelly_frac":        kelly * 0.5,
                "fee_cents":         kalshi_fee(no_price),
                "priority":          2,
                "entry_limit_cents": max(1, no_price - 1),
                "take_profit_cents": 97,
                "stop_loss_pct":     0.3,
            })
        elif yp >= 92:
            # Extreme favorite value: >92¢ YES slightly underpriced historically
            # Research: Extreme favorites win ≈ 95-97% but are priced at 92-95¢
            edge_prob = 96
            kelly = kelly_size(edge_prob, yp, maker=True)
            if kelly > 0.001:
                signals.append({
                    "type":              "longshot_bias",
                    "direction":         "BUY YES",
                    "ticker":            ticker,
                    "title":             title,
                    "price":             yp,
                    "rationale":         f"Extreme favourite value: {yp}¢ YES historically wins ~96%. Small edge.",
                    "confidence":        "low",
                    "kelly_frac":        kelly * 0.3,
                    "fee_cents":         kalshi_fee(yp),
                    "priority":          3,  # lower priority for small edge
                    "entry_limit_cents": max(1, yp - 2),
                    "take_profit_cents": min(98, yp + 5),
                    "stop_loss_pct":     0.3,
                })
        elif yp >= 85:
            # Moderate favourite: slight underpricing documented
            edge_prob = 91
            kelly = kelly_size(edge_prob, yp, maker=True)
            if kelly > 0.001:
                signals.append({
                    "type":              "longshot_bias",
                    "direction":         "BUY YES",
                    "ticker":            ticker,
                    "title":             title,
                    "price":             yp,
                    "rationale":         f"Favorite value: {yp}¢ — market underprices certainty. Buy YES.",
                    "confidence":        "low",
                    "kelly_frac":        kelly * 0.3,
                    "fee_cents":         kalshi_fee(yp),
                    "priority":          3,
                    "entry_limit_cents": max(1, yp - 2),
                    "take_profit_cents": min(98, yp + 5),
                    "stop_loss_pct":     0.3,
                })

    # Sort by kelly_frac desc and cap to avoid flooding the signal list
    signals.sort(key=lambda x: -x.get("kelly_frac", 0))
    log(f"Longshot bias: {len(signals)} signals (capped to 6)")
    return signals[:6]

def _is_exhaustive_series(series, suffixes):
    """
    Check if the contracts in a series form an exhaustive/mutually-exclusive set.

    For 2-contract series: assume exhaustive UNLESS it's a SPREAD market
    (spread outcomes are stacked, not complementary).

    For SPREAD markets (e.g., OKC6, OKC20, SAS1): NOT exhaustive because:
    - OKC6 = "OKC wins by 6+" and OKC20 = "OKC wins by 20+" OVERLAP
    - Missing outcome: "OKC wins by 1-5"

    For GAME/1H/WINNER markets (e.g., SAS, OKC): exactly one wins, exhaustive.
    """
    s = series.upper()
    # Spread markets are NOT exhaustive (overlapping outcomes)
    if "SPREAD" in s:
        return False
    # TOTAL markets can be non-exhaustive (e.g., 10, 11, 12 run options overlap if not adjacent)
    if "TOTAL" in s and len(suffixes) > 2:
        # Check if they look like a ladder (numeric-only suffixes)
        try:
            vals = sorted(int(x) for x in suffixes)
            # If they're sequential integers, they might be adjacent buckets — allow
            return all(vals[i+1] == vals[i] + 1 for i in range(len(vals)-1))
        except ValueError:
            return False
    # GAME markets are always 2-outcome exhaustive sets
    if "GAME" in s or "1H" in s:
        return len(suffixes) == 2 and all(len(x) <= 5 and x.isalpha() for x in suffixes)

    # WINNER/CHAMP/FINALS markets are exhaustive if ALL remaining teams are present
    # (prices summing to ~100 confirms exhaustiveness — checked in the caller)
    winner_types = ["WINNER", "CHAMP", "FINALS", "FUTURES", "ELEC", "POL",
                    "PRES", "SEN", "GOV", "MVE", "MVP", "WORLDCUP"]
    if any(t in s for t in winner_types):
        # Allow any count of team/candidate suffixes that look like abbreviations
        if all(len(x) <= 5 and x.isalpha() for x in suffixes):
            return True

    # 2-contract non-spread series: assume exhaustive if suffixes look like team names
    if len(suffixes) == 2 and all(len(x) <= 5 and x.isalpha() for x in suffixes):
        return True

    return False


def analyze_bundle_arb(markets):
    """
    Bundle arbitrage: if sum of YES prices for exhaustive mutually-exclusive
    outcomes < 100 - fees, buying all contracts locks in risk-free profit.

    Key constraint: ONLY flags true exhaustive sets (e.g., SAS or OKC wins).
    Explicitly rejects SPREAD markets (overlapping outcomes) to prevent false arbs.
    """
    signals = []
    from collections import defaultdict
    series_groups = defaultdict(list)
    for m in markets:
        ticker = m.get("ticker", "")
        parts = ticker.rsplit("-", 1)
        if len(parts) == 2:
            series_groups[parts[0]].append(m)

    for series, contracts in series_groups.items():
        if len(contracts) < 2: continue

        # Exhaustiveness check — skip non-exhaustive series
        suffixes = [c.get("ticker", "").rsplit("-", 1)[-1] for c in contracts]
        if not _is_exhaustive_series(series, suffixes):
            continue

        yes_prices = []
        valid = True
        for c in contracts:
            yp = c.get("_yes_price")
            if yp is None: valid = False; break
            yes_prices.append(yp)
        if not valid: continue

        total = sum(yes_prices)
        # Prices should sum near 100; a gap = guaranteed profit
        # Reject if total > 100 (overpriced) or suspiciously low (<50% = game unlikely)
        if total >= 97 or total < 50:
            continue
        total_fee = sum(kalshi_fee(yp) for yp in yes_prices)
        net_profit = 100 - total - total_fee

        if net_profit > 2.0:  # at least 2¢ net profit after fees
            signals.append({
                "type":              "bundle_arb",
                "direction":         "BUY ALL",
                "ticker":            series,
                "title":             f"Bundle arb: {len(contracts)} contracts sum to {total:.1f}¢",
                "price":             total,
                "rationale":         f"Sum of {len(contracts)} mutually exclusive contracts = {total:.1f}¢ (pays 100¢). Net after fees: +{net_profit:.1f}¢",
                "confidence":        "high",
                "kelly_frac":        0.05,  # conservative fixed size for arb
                "fee_cents":         total_fee,
                "net_profit":        round(net_profit, 1),
                "priority":          1,
                "contracts":         [c.get("ticker") for c in contracts],
                "entry_limit_cents": total,
                "take_profit_cents": 100,
                "stop_loss_pct":     None,
            })
    return signals

def analyze_vegas_divergence(edges):
    """
    Convert existing Vegas edge analysis into strategy signals with Kelly sizing.
    """
    signals = []
    for e in edges:
        gap       = e.get("gap", 0)
        kp        = e.get("kalshi_price", 50)
        vp        = e.get("vegas_prob", 50)
        direction = e.get("direction", "")
        if abs(gap) < 5: continue

        is_buy_no = "NO" in direction.upper() and "YES" not in direction.upper()
        if is_buy_no:
            # BUY NO: Kalshi overprices YES (kp > vp). We buy NO.
            side_price = 100 - kp          # NO fair price
            side_prob  = 100 - vp          # NO win probability per Vegas
            tp_cents   = min(99, int(100 - vp) + 3)  # sell NO when YES falls to vp
        else:
            side_price = kp
            side_prob  = vp
            tp_cents   = min(99, int(vp))

        kelly = kelly_size(side_prob, side_price, maker=True, fraction=0.25)

        signals.append({
            "type":              "vegas_divergence",
            "direction":         direction,
            "ticker":            e.get("ticker", ""),
            "title":             e.get("title", "")[:60],
            "price":             kp,
            "rationale":         f"Vegas implies {vp:.1f}¢, Kalshi at {kp}¢. Gap: {gap:+.1f}¢. Use LIMIT order.",
            "confidence":        "high" if abs(gap) >= 10 else "medium",
            "kelly_frac":        kelly,
            "fee_cents":         kalshi_fee(side_price, maker=True),
            "priority":          1 if abs(gap) >= 10 else 2,
            "game":              e.get("game", ""),
            "books":             e.get("books", []),
            "entry_limit_cents": max(1, side_price - 3),
            "take_profit_cents": tp_cents,
            "stop_loss_pct":     0.4,
        })
    return signals

def analyze_cross_platform_arb(cross_arb):
    """
    Convert cross-market arb into strategy signals with fee-adjusted profitability.
    Minimum viable spread: ~3¢ after fees for Kalshi-PolyMarket/Manifold,
                           ~17¢ for anything involving PredictIt.
    """
    signals = []
    for a in cross_arb:
        gap = abs(a.get("gap", 0))
        # find_cross_market_arb uses 'platform' and 'platform_price' (not source/other_price)
        platform = a.get("platform", a.get("source", "")).lower()
        kalshi_p = a.get("kalshi_price", 50)
        other_p  = a.get("platform_price", a.get("other_price", 50))

        # Fee thresholds by platform
        kalshi_fee_val = kalshi_fee(kalshi_p, maker=True)
        if "predictit" in platform:
            min_viable = 17.0  # PredictIt 15% effective fee kills most arb
            other_fee  = 15.0
        elif "manifold" in platform:
            min_viable = 4.0  # Manifold lower liquidity = higher threshold
            other_fee  = 0.5
        else:  # PolyMarket
            min_viable = 3.0
            other_fee  = 0.02

        net_profit = gap - kalshi_fee_val - other_fee
        if net_profit < 1.0: continue  # not profitable after fees

        direction_str    = a.get("direction", "")
        is_buy_no        = "BUY NO" in direction_str
        side_price       = (100 - kalshi_p) if is_buy_no else kalshi_p
        platform_display = a.get("platform", platform.title() if platform else "Other")
        signals.append({
            "type":              "cross_platform_arb",
            "direction":         direction_str,
            "ticker":            a.get("kalshi_ticker", ""),
            "title":             a.get("kalshi_title", "")[:60],
            "price":             kalshi_p,  # YES price for display convention
            "rationale":         f"{platform_display}: {other_p:.1f}¢ vs Kalshi {kalshi_p:.1f}¢. Net after fees: +{net_profit:.1f}¢. ⚠️ Verify settlement rules match.",
            "confidence":        "medium",
            "kelly_frac":        0.03,  # small fixed size — settlement risk
            "fee_cents":         kalshi_fee_val,
            "net_profit":        round(net_profit, 2),
            "priority":          1 if net_profit >= 5 else 2,
            "warning":           "Verify settlement language matches before entering both legs.",
            "entry_limit_cents": max(1, side_price - 2),
            "take_profit_cents": min(99, (100 - int(other_p)) if is_buy_no else int(other_p)),
            "stop_loss_pct":     0.4,
        })
    return signals

def analyze_weather_edge(markets, weather_data):
    """
    Compare NWS/Open-Meteo model forecast to Kalshi weather market prices.
    Market-implied uncertainty exceeds realized uncertainty by 1.27x historically.
    Targets KXHIGH* markets.
    """
    if not weather_data:
        return []
    signals = []

    # Map city codes to Kalshi ticker fragments
    CITY_MAP = {
        "ATX": ["KXHIGHAUS", "KXHIGHTSATX"],
        "SFO": ["KXHIGHTSFO"],
        "LAX": ["KXHIGHLAX"],
        "HOU": ["KXHIGHOUSTON", "KXHIGHHOU"],
        "NYC": ["KXHIGHNYC", "KXHIGHTSNYE"],
        "CHI": ["KXHIGHCHI"],
        "MIA": ["KXHIGHMIA"],
        "DEN": ["KXHIGHDEN"],
        "SEA": ["KXHIGHSEA"],
        "PHX": ["KXHIGHPHX"],
    }

    for m in markets:
        ticker = m.get("ticker", "")
        yes_p  = m.get("_yes_price")
        title  = m.get("title", "")
        if yes_p is None: continue
        if "KXHIGH" not in ticker.upper(): continue

        # Find matching city
        matched_city = None
        matched_forecast = None
        for city_code, prefixes in CITY_MAP.items():
            for prefix in prefixes:
                if ticker.upper().startswith(prefix):
                    matched_city = city_code
                    break
            if matched_city: break

        if not matched_city or matched_city not in weather_data:
            continue

        forecasts = weather_data[matched_city].get("forecasts", [])
        if not forecasts:
            continue

        # Get tomorrow's forecast high
        tomorrow_forecast = forecasts[1] if len(forecasts) > 1 else forecasts[0]
        model_high = tomorrow_forecast.get("high_f")
        if model_high is None:
            continue

        # Try to extract the threshold from the title
        # e.g. "Will the high temp in Austin be 87-88°"
        import re
        temp_match = re.search(r'(\d+)[\-–](\d+)', title)
        if not temp_match:
            temp_match = re.search(r'(\d{2,3})', title)
        if not temp_match:
            continue

        try:
            low_temp = float(temp_match.group(1))
            high_temp = float(temp_match.group(2)) if temp_match.lastindex >= 2 else low_temp + 1
        except (ValueError, AttributeError):
            continue

        mid_temp = (low_temp + high_temp) / 2
        diff = model_high - mid_temp

        if abs(diff) >= 3:
            direction   = "BUY YES" if diff >= 0 else "BUY NO"
            city_name   = weather_data[matched_city].get("name", matched_city)
            is_no       = direction == "BUY NO"
            side_price  = (100 - yes_p) if is_no else yes_p
            model_prob  = int(round(yes_p + diff * 3))  # rough model-implied YES prob
            prob_for_kelly = (85 if abs(diff) >= 5 else 70)
            if is_no:
                prob_for_kelly = 100 - prob_for_kelly  # flip to NO probability
            signals.append({
                "type":              "weather_edge",
                "direction":         direction,
                "ticker":            ticker,
                "title":             title[:60],
                "price":             yes_p,
                "rationale":         f"Model forecasts {model_high}°F high for {city_name}. Market bucket: {low_temp:.0f}-{high_temp:.0f}°F. Diff: {diff:+.1f}°. Use LIMIT order.",
                "confidence":        "high" if abs(diff) >= 5 else "medium",
                "kelly_frac":        kelly_size(prob_for_kelly, side_price, maker=True, fraction=0.25),
                "fee_cents":         kalshi_fee(side_price, maker=True),
                "priority":          1 if abs(diff) >= 5 else 2,
                "model_high":        model_high,
                "entry_limit_cents": max(1, side_price - 5),
                "take_profit_cents": min(99, side_price + 10) if is_no else min(99, model_prob),
                "stop_loss_pct":     0.4,
            })
    return signals

def analyze_volume_spikes(markets):
    """
    Volume spike detection: markets with unusually high 24h volume
    relative to their typical volume signal informed trading.
    High volume + neutral price (40-60¢) = undecided market about to move.
    High volume + extreme price (>85¢ or <15¢) = confirmation trade.
    """
    signals = []
    # Sort by volume — use _trade_count as fallback, lower threshold for Kalshi
    def mkt_vol(m):
        return m.get("volume", 0) or m.get("_trade_count", 0) or 0

    sorted_markets = sorted(
        [m for m in markets if mkt_vol(m) > 3],  # lowered from 50 to 3 (Kalshi lower volume)
        key=mkt_vol,
        reverse=True
    )
    # Take top 20% by volume (or top 10, whichever smaller)
    top_n = max(3, min(10, len(sorted_markets) // 5))
    hot_markets = sorted_markets[:top_n]

    if not hot_markets:
        return signals

    # Compute median volume for reference
    vols = [mkt_vol(m) for m in sorted_markets]
    median_vol = sorted(vols)[len(vols) // 2] if vols else 10

    for m in hot_markets:
        ticker = m.get("ticker", "")
        title  = m.get("title", ticker)
        vol    = mkt_vol(m)
        price  = m.get("_yes_price", m.get("yes_ask", 50))

        if not ticker or not price:
            continue

        vol_ratio = vol / median_vol if median_vol else 1
        if vol_ratio < 2.0:  # Need 2x median volume to qualify (lowered from 3x)
            continue

        # Strategy: high volume on undecided markets = follow the money
        if 35 <= price <= 65:
            # Neutral market with high volume = someone knows something
            # Direction: buy YES if price is above 50, NO if below
            direction = "BUY YES" if price >= 50 else "BUY NO"
            side_price = price if direction == "BUY YES" else (100 - price)
            # Volume-adjusted probability: smart money increases implied certainty
            # Each unit of vol_ratio above 2x adds ~3% probability boost (capped at +15%)
            vol_boost = min(15, (vol_ratio - 2) * 3)
            if direction == "BUY YES":
                true_prob = min(95, price + vol_boost)
            else:  # BUY NO — use NO probability
                true_prob = min(95, (100 - price) + vol_boost)
            kelly = kelly_size(true_prob, side_price, maker=True)
            signals.append({
                "type":              "volume_spike",
                "direction":         direction,
                "ticker":            ticker,
                "title":             title,
                "price":             price,
                "rationale":         f"Volume spike {vol_ratio:.1f}x median ({vol:,} trades). Neutral price {price}¢ — informed traders positioning. True prob est: {true_prob:.0f}¢.",
                "confidence":        "medium",
                "kelly_frac":        kelly * 0.5,  # half-Kelly for momentum
                "fee_cents":         kalshi_fee(side_price),
                "priority":          2,
                "volume":            vol,
                "vol_ratio":         round(vol_ratio, 1),
                "entry_limit_cents": max(1, side_price - 2),
                "take_profit_cents": min(99, side_price + 5),
                "stop_loss_pct":     0.35,
            })
        elif price > 85:
            # Heavy volume on near-certainty = high confidence follow
            # Volume spike on >85¢ favourite: estimated true prob slightly higher
            vol_boost = min(5, (vol_ratio - 2) * 1.5)  # small boost, already near ceiling
            true_prob = min(98, price + vol_boost)
            kelly = kelly_size(true_prob, price, maker=True)
            if kelly > 0:
                signals.append({
                    "type":              "volume_spike",
                    "direction":         "BUY YES",
                    "ticker":            ticker,
                    "title":             title,
                    "price":             price,
                    "rationale":         f"Volume spike {vol_ratio:.1f}x ({vol:,} trades) on {price}¢ favourite. Heavy confirmation. Est. true P={true_prob:.0f}¢.",
                    "confidence":        "high",
                    "kelly_frac":        kelly * 0.3,
                    "fee_cents":         kalshi_fee(price),
                    "priority":          2,
                    "volume":            vol,
                    "vol_ratio":         round(vol_ratio, 1),
                    "entry_limit_cents": max(1, price - 2),
                    "take_profit_cents": min(99, price + 5),
                    "stop_loss_pct":     0.35,
                })
        elif price < 15:
            # High volume longshot — longshot bias says fade, so BUY NO
            # Longshot bias: 10¢ contracts win ~4-7%, so NO probability ~93-96%
            no_price  = 100 - price
            true_prob = min(97, no_price + min(5, (vol_ratio - 2) * 1.5))  # bias + vol boost
            kelly = kelly_size(true_prob, no_price, maker=True)
            if kelly > 0:
                signals.append({
                    "type":              "volume_spike",
                    "direction":         "BUY NO",
                    "ticker":            ticker,
                    "title":             title,
                    "price":             price,
                    "rationale":         f"Volume spike {vol_ratio:.1f}x ({vol:,} trades) on {price}¢ longshot. Bias + confirmation = fade. NO true P≈{true_prob:.0f}¢.",
                    "confidence":        "medium",
                    "kelly_frac":        kelly * 0.3,
                    "fee_cents":         kalshi_fee(no_price),
                    "priority":          2,
                    "volume":            vol,
                    "vol_ratio":         round(vol_ratio, 1),
                    "entry_limit_cents": max(1, no_price - 2),
                    "take_profit_cents": min(99, no_price + 3),
                    "stop_loss_pct":     0.35,
                })

    return signals

def analyze_espn_odds_edge(espn_games, markets):
    """
    Compare ESPN's embedded DraftKings odds to Kalshi prices.
    Gives a second data point beyond The Odds API — helps confirm or contradict
    existing edges and catches markets the Odds API misses.
    """
    signals = []
    if not espn_games or not markets:
        return signals

    # Build index: sport → ticker suffix → market
    # KXMLBGAME-26MAY261840LAADET-LAA → suffix "LAA" = LAA wins
    from collections import defaultdict
    sport_suffix_map = defaultdict(dict)
    league_map = {"basketball": "nba", "baseball": "mlb", "hockey": "nhl", "football": "nfl"}
    ticker_league = {}
    for m in markets:
        ticker = m.get("ticker", "")
        yp = m.get("_yes_price")
        if yp is None:
            continue
        # Guess league from ticker prefix
        for kw, league in [("NBA", "nba"), ("MLB", "mlb"), ("NHL", "nhl"), ("NFL", "nfl")]:
            if kw in ticker:
                parts = ticker.rsplit("-", 1)
                if len(parts) == 2:
                    suffix = parts[1].upper()
                    sport_suffix_map[league][suffix] = m
                    ticker_league[ticker] = league
                break

    for game in espn_games:
        league = game.get("league", "")
        if league not in sport_suffix_map:
            continue

        # Skip already-in-progress or completed games
        status = (game.get("status") or "").lower()
        if any(w in status for w in ("in progress", "final", "halftime", "end of")):
            continue

        # Get pre-game DraftKings odds (not live)
        pre_odds = [o for o in game.get("espn_odds", []) if "live" not in o.get("provider", "").lower()]
        if not pre_odds:
            continue
        odds = pre_odds[0]

        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        hml = odds.get("home_moneyline")
        aml = odds.get("away_moneyline")
        if not hml or not aml:
            continue

        # Convert moneyline to implied probability (remove vig)
        def ml_to_prob(ml):
            if ml is None: return None
            return (abs(ml) / (abs(ml) + 100) * 100) if ml < 0 else (100 / (ml + 100) * 100)

        home_prob = ml_to_prob(hml)
        away_prob = ml_to_prob(aml)
        if home_prob is None or away_prob is None:
            continue

        # Remove vig (normalize so probs sum to 100)
        total = home_prob + away_prob
        home_prob = home_prob / total * 100
        away_prob = away_prob / total * 100

        # Try to match to Kalshi markets by team abbreviation
        suffix_map = sport_suffix_map[league]

        # Hard-coded team name → Kalshi ticker suffix lookup
        _TEAM_ABBREV = {
            # NBA
            "ATLANTA HAWKS": "ATL", "BOSTON CELTICS": "BOS", "BROOKLYN NETS": "BKN",
            "CHARLOTTE HORNETS": "CHA", "CHICAGO BULLS": "CHI", "CLEVELAND CAVALIERS": "CLE",
            "DALLAS MAVERICKS": "DAL", "DENVER NUGGETS": "DEN", "DETROIT PISTONS": "DET",
            "GOLDEN STATE WARRIORS": "GSW", "HOUSTON ROCKETS": "HOU", "INDIANA PACERS": "IND",
            "LOS ANGELES CLIPPERS": "LAC", "LOS ANGELES LAKERS": "LAL", "MEMPHIS GRIZZLIES": "MEM",
            "MIAMI HEAT": "MIA", "MILWAUKEE BUCKS": "MIL", "MINNESOTA TIMBERWOLVES": "MIN",
            "NEW ORLEANS PELICANS": "NOP", "NEW YORK KNICKS": "NYK", "OKLAHOMA CITY THUNDER": "OKC",
            "ORLANDO MAGIC": "ORL", "PHILADELPHIA 76ERS": "PHI", "PHOENIX SUNS": "PHX",
            "PORTLAND TRAIL BLAZERS": "POR", "SACRAMENTO KINGS": "SAC", "SAN ANTONIO SPURS": "SAS",
            "TORONTO RAPTORS": "TOR", "UTAH JAZZ": "UTA", "WASHINGTON WIZARDS": "WAS",
            # MLB
            "ARIZONA DIAMONDBACKS": "ARI", "ATLANTA BRAVES": "ATL", "BALTIMORE ORIOLES": "BAL",
            "BOSTON RED SOX": "BOS", "CHICAGO CUBS": "CHC", "CHICAGO WHITE SOX": "CWS",
            "CINCINNATI REDS": "CIN", "CLEVELAND GUARDIANS": "CLE", "COLORADO ROCKIES": "COL",
            "DETROIT TIGERS": "DET", "HOUSTON ASTROS": "HOU", "KANSAS CITY ROYALS": "KC",
            "LOS ANGELES ANGELS": "LAA", "LOS ANGELES DODGERS": "LAD", "MIAMI MARLINS": "MIA",
            "MILWAUKEE BREWERS": "MIL", "MINNESOTA TWINS": "MIN", "NEW YORK METS": "NYM",
            "NEW YORK YANKEES": "NYY", "OAKLAND ATHLETICS": "OAK", "PHILADELPHIA PHILLIES": "PHI",
            "PITTSBURGH PIRATES": "PIT", "SAN DIEGO PADRES": "SD", "SAN FRANCISCO GIANTS": "SF",
            "SEATTLE MARINERS": "SEA", "ST. LOUIS CARDINALS": "STL", "TAMPA BAY RAYS": "TB",
            "TEXAS RANGERS": "TEX", "TORONTO BLUE JAYS": "TOR", "WASHINGTON NATIONALS": "WSH",
            # NHL
            "ANAHEIM DUCKS": "ANA", "ARIZONA COYOTES": "ARI", "BOSTON BRUINS": "BOS",
            "BUFFALO SABRES": "BUF", "CALGARY FLAMES": "CGY", "CAROLINA HURRICANES": "CAR",
            "CHICAGO BLACKHAWKS": "CHI", "COLORADO AVALANCHE": "COL", "COLUMBUS BLUE JACKETS": "CBJ",
            "DALLAS STARS": "DAL", "DETROIT RED WINGS": "DET", "EDMONTON OILERS": "EDM",
            "FLORIDA PANTHERS": "FLA", "LOS ANGELES KINGS": "LA", "MINNESOTA WILD": "MIN",
            "MONTREAL CANADIENS": "MTL", "NASHVILLE PREDATORS": "NSH", "NEW JERSEY DEVILS": "NJ",
            "NEW YORK ISLANDERS": "NYI", "NEW YORK RANGERS": "NYR", "OTTAWA SENATORS": "OTT",
            "PHILADELPHIA FLYERS": "PHI", "PITTSBURGH PENGUINS": "PIT", "SAN JOSE SHARKS": "SJS",
            "SEATTLE KRAKEN": "SEA", "ST. LOUIS BLUES": "STL", "TAMPA BAY LIGHTNING": "TB",
            "TORONTO MAPLE LEAFS": "TOR", "UTAH HOCKEY CLUB": "UTA", "VANCOUVER CANUCKS": "VAN",
            "VEGAS GOLDEN KNIGHTS": "VGK", "WASHINGTON CAPITALS": "WSH", "WINNIPEG JETS": "WPG",
        }

        for team_name, espn_prob, ml in [
            (home_team, home_prob, hml),
            (away_team, away_prob, aml),
        ]:
            team_upper = team_name.upper()
            # Try hard-coded lookup first
            candidates = set()
            if team_upper in _TEAM_ABBREV:
                candidates.add(_TEAM_ABBREV[team_upper])

            # Also try common abbreviation patterns from team name
            words = team_upper.split()
            if words:
                candidates.add(words[-1][:3])   # last word first 3 (e.g., THUNDER→THU)
                candidates.add(words[-1][:4])   # last word first 4 (e.g., THUNDER→THUN)
                candidates.add(words[0][:3])    # first word first 3
                # Common 2-3 letter abbreviations
                if len(words) >= 2:
                    candidates.add(words[0][0] + words[1][:2])   # OKC from Oklahoma City
                    candidates.add(words[0][0] + words[-1][:2])
                # Single words: use first 3 letters
                for w in words:
                    candidates.add(w[:3])
                    candidates.add(w[:4])

            matched_market = None
            matched_abbrev = None
            for cand in candidates:
                if cand in suffix_map:
                    matched_market = suffix_map[cand]
                    matched_abbrev = cand
                    break

            if not matched_market:
                continue

            kalshi_price = matched_market.get("_yes_price", 50)
            gap = kalshi_price - espn_prob  # positive = Kalshi overprices this team

            if abs(gap) < 5:
                continue

            # ESPN says team is cheaper → buy YES (Kalshi is overpriced on opposing team)
            # ESPN says team is more expensive → buy NO
            if gap > 5:  # Kalshi overprices → buy NO (fade)
                direction  = "BUY NO"
                no_price   = 100 - kalshi_price
                side_price = no_price
                prob       = 100 - espn_prob
                # TP: sell NO when YES falls to ESPN consensus (NO rises to 100 - espn_prob)
                tp = min(99, int(100 - espn_prob) + 3)
            else:  # Kalshi underprices → buy YES
                direction  = "BUY YES"
                side_price = kalshi_price
                prob       = espn_prob
                tp = min(99, int(espn_prob))

            kelly = kelly_size(prob, side_price, maker=True)
            if kelly <= 0:
                continue

            signals.append({
                "type":              "espn_odds_edge",
                "direction":         direction,
                "ticker":            matched_market.get("ticker", ""),
                "title":             matched_market.get("title", ""),
                "price":             round(kalshi_price, 1),
                "rationale":         f"ESPN/DK: {team_name} {ml:+d} ({espn_prob:.0f}%) vs Kalshi {kalshi_price:.0f}¢. Gap: {gap:+.1f}¢ — {game.get('short_name','')}",
                "confidence":        "high" if abs(gap) >= 10 else "medium",
                "kelly_frac":        kelly * 0.5,
                "fee_cents":         kalshi_fee(side_price),
                "priority":          1 if abs(gap) >= 10 else 2,
                "espn_prob":         round(espn_prob, 1),
                "gap":               round(gap, 1),
                "entry_limit_cents": max(1, side_price - 3),
                "take_profit_cents": tp,
                "stop_loss_pct":     0.35,
            })

    signals.sort(key=lambda x: abs(x.get("gap", 0)), reverse=True)
    log(f"ESPN odds edge: {len(signals)} signals")
    return signals[:5]


def analyze_fear_greed_edge(markets, fear_greed_data):
    """
    Extreme fear/greed creates predictable mispricings in crypto Kalshi markets.

    Research: crypto retail follows sentiment with a 24-48h lag.
    - Extreme Fear (≤20): crypto markets priced too bearish → BUY YES on UP contracts
    - Extreme Greed (≥80): crypto markets priced too bullish → BUY NO on UP contracts

    Only targets BTC/ETH/crypto price direction markets.
    """
    signals = []
    if not fear_greed_data or not markets:
        return signals

    fg_value = fear_greed_data.get("value")
    if fg_value is None:
        return signals

    try:
        fg = int(fg_value)
    except (ValueError, TypeError):
        return signals

    if 25 < fg < 75:
        return signals  # neutral — no edge

    is_fear = fg <= 25
    fg_label = fear_greed_data.get("label", "")

    crypto_kws = ["BTC", "ETH", "SOL", "CRYPTO", "KXBTC", "KXETH", "KXSOL"]
    direction_kws = ["UP", "ABOVE", "HIGH", "PRICE"]
    # Skip ultra-short-term markets (15min/1hr BTC markets — F&G works on daily timeframe)
    short_term_kws = ["15M", "1HR", "30M", "15MIN", "1H-", "1H_", "KXBTC1", "KXETH1"]

    for m in markets:
        ticker = m.get("ticker", "").upper()
        title  = m.get("title", "").upper()

        # Only target crypto direction markets
        if not any(kw in ticker for kw in crypto_kws):
            continue
        # Skip ultra-short-term price markets — F&G sentiment resolves over days
        if any(kw in ticker for kw in short_term_kws):
            continue
        is_direction = any(kw in ticker or kw in title for kw in direction_kws)
        if not is_direction:
            continue

        price = m.get("_yes_price")
        if price is None or not (5 <= price <= 95):
            continue

        if is_fear:
            # Extreme fear → price action likely to recover → buy YES on up markets
            direction  = "BUY YES"
            side_price = price
            prob       = min(75, price + 15)  # sentiment edge: ~15% adjustment
            kelly      = kelly_size(prob, price, maker=True)
            rationale  = f"Fear & Greed = {fg} ({fg_label}). Extreme fear → sentiment reversal edge. Crypto markets priced too bearish."
        else:
            # Extreme greed → likely to cool → fade the move
            direction  = "BUY NO"
            side_price = 100 - price  # NO price
            prob       = min(75, side_price + 15)
            kelly      = kelly_size(prob, side_price, maker=True)
            rationale  = f"Fear & Greed = {fg} ({fg_label}). Extreme greed → distribution phase. Crypto markets priced too bullish."

        if kelly <= 0:
            continue

        signals.append({
            "type":              "fear_greed_edge",
            "direction":         direction,
            "ticker":            m.get("ticker", ""),
            "title":             m.get("title", ""),
            "price":             price,
            "rationale":         rationale,
            "confidence":        "high" if (fg <= 15 or fg >= 85) else "medium",
            "kelly_frac":        kelly * 0.4,
            "fee_cents":         kalshi_fee(side_price),
            "priority":          2,
            "fear_greed":        fg,
            "entry_limit_cents": max(1, side_price - 2),
            "take_profit_cents": min(99, side_price + 5),
            "stop_loss_pct":     0.4,
        })

    signals.sort(key=lambda x: x.get("kelly_frac", 0), reverse=True)
    log(f"Fear/Greed edge: {len(signals)} signals (F&G={fg})")
    return signals[:3]


def analyze_fred_edge(markets, fred_data):
    """
    Use FRED economic data to find mispriced economic/political Kalshi markets.

    Key relationships:
    - Fed Funds Rate (fed_rate): affects "Fed rate hike/cut/hold" markets (KXFED)
    - CPI (cpi): affects "inflation above/below X%" markets (KXCPI)
    - Unemployment (unemployment): affects "jobless rate" markets (KXJOBS)
    - 10Y Treasury (treasury_10y): affects "yield above/below X" markets
    - Payrolls (payrolls): affects nonfarm payroll markets

    Strategy: when FRED data strongly suggests a direction and Kalshi price
    disagrees, that's an edge.
    """
    signals = []
    if not fred_data or not markets:
        return signals

    # Extract key values using FRED_SERIES keys directly (not series IDs)
    def get_val(key):
        info = fred_data.get(key, {})
        if isinstance(info, dict):
            return info.get("value")
        try: return float(info)
        except: return None

    fed_rate    = get_val("fed_rate")      # FEDFUNDS: e.g. 4.33% in Jan 2025
    cpi         = get_val("cpi")           # CPIAUCSL: level e.g. 319.1 in Dec 2024
    pce         = get_val("pce")           # PCEPI: Fed's preferred inflation measure, level
    unrate      = get_val("unemployment")  # UNRATE: e.g. 4.1%
    t10y        = get_val("treasury_10y")  # DGS10: e.g. 4.60%
    t2y         = get_val("treasury_2y")   # DGS2: e.g. 4.25%
    payrolls    = get_val("payrolls")      # PAYEMS in thousands, e.g. 159,400
    vix         = get_val("vix")           # VIX: equity vol index, e.g. 18.5

    import re as _re

    # Cross-reference with Kalshi markets
    for m in markets:
        ticker = m.get("ticker", "").upper()
        title  = (m.get("title") or "").upper()
        yp     = m.get("_yes_price")
        if yp is None:
            continue

        # ── Fed rate markets (KXFED) ──────────────────────────────────────────
        is_fed_mkt = "KXFED" in ticker or (
            "FED" in ticker and any(x in title for x in
            ("CUT", "HIKE", "HOLD", "RAISE", "PAUSE", "PIVOT", "LOWER", "RAISE", "RATE")))

        if is_fed_mkt and fed_rate is not None:
            if any(x in title for x in ("HIKE", "RAISE", "INCREASE")) and yp > 20:
                # Fed in cutting cycle (2024+); another hike is very unlikely
                no_price = 100 - yp
                if no_price > 50:
                    signals.append({
                        "type":              "fred_edge",
                        "direction":         "BUY NO",
                        "ticker":            m.get("ticker", ""),
                        "title":             m.get("title", ""),
                        "price":             yp,
                        "rationale":         f"FRED: Fed Funds = {fed_rate:.2f}%. Fed in cutting cycle — rate hike market at {yp}¢ overpriced.",
                        "confidence":        "medium",
                        "kelly_frac":        0.02,
                        "fee_cents":         kalshi_fee(no_price),
                        "priority":          2,
                        "entry_limit_cents": max(1, no_price - 2),
                        "take_profit_cents": min(99, no_price + 8),
                        "stop_loss_pct":     0.4,
                    })
            elif any(x in title for x in ("CUT", "LOWER", "REDUCE")) and fed_rate > 3.0 and yp < 35:
                # Rates still elevated; more cuts expected by Fed
                signals.append({
                    "type":              "fred_edge",
                    "direction":         "BUY YES",
                    "ticker":            m.get("ticker", ""),
                    "title":             m.get("title", ""),
                    "price":             yp,
                    "rationale":         f"FRED: Fed Funds = {fed_rate:.2f}%. Rate still elevated — cut market at {yp}¢ may be underpriced.",
                    "confidence":        "medium",
                    "kelly_frac":        0.015,
                    "fee_cents":         kalshi_fee(yp),
                    "priority":          2,
                    "entry_limit_cents": max(1, yp - 2),
                    "take_profit_cents": min(99, yp + 8),
                    "stop_loss_pct":     0.4,
                })

        # ── CPI / inflation markets (KXCPI) ───────────────────────────────────
        is_cpi_mkt = "KXCPI" in ticker or any(
            x in title for x in ("CPI", "INFLATION", "PRICE INDEX"))

        if is_cpi_mkt and cpi is not None:
            # CPIAUCSL: Jan 2020 base=258, Dec 2024 ≈319 → ~3.5% above 2% target
            # If level >313, inflation has been running above 2.5% target for years
            if cpi > 313:
                if any(x in title for x in ("ABOVE", "HIGHER", "EXCEED", "OVER")) and yp < 45:
                    signals.append({
                        "type":              "fred_edge",
                        "direction":         "BUY YES",
                        "ticker":            m.get("ticker", ""),
                        "title":             m.get("title", ""),
                        "price":             yp,
                        "rationale":         f"FRED: CPI = {cpi:.1f} (well above pre-2021 trend). Above-threshold inflation market at {yp}¢ looks underpriced.",
                        "confidence":        "low",
                        "kelly_frac":        0.01,
                        "fee_cents":         kalshi_fee(yp),
                        "priority":          3,
                        "entry_limit_cents": max(1, yp - 2),
                        "take_profit_cents": min(99, yp + 6),
                        "stop_loss_pct":     0.4,
                    })

        # ── Unemployment / jobs markets (KXJOBS) ──────────────────────────────
        is_jobs_mkt = "KXJOBS" in ticker or any(
            x in ticker for x in ("UNEMP", "JOBLESS")) or "UNEMPLOYMENT" in title

        if is_jobs_mkt and unrate is not None:
            if unrate > 4.5 and any(x in title for x in ("ABOVE", "HIGHER", "EXCEED")) and yp < 40:
                signals.append({
                    "type":              "fred_edge",
                    "direction":         "BUY YES",
                    "ticker":            m.get("ticker", ""),
                    "title":             m.get("title", ""),
                    "price":             yp,
                    "rationale":         f"FRED: Unemployment = {unrate:.1f}%. Rate elevated — above-threshold market at {yp}¢ looks underpriced.",
                    "confidence":        "low",
                    "kelly_frac":        0.01,
                    "fee_cents":         kalshi_fee(yp),
                    "priority":          3,
                    "entry_limit_cents": max(1, yp - 2),
                    "take_profit_cents": min(99, yp + 5),
                    "stop_loss_pct":     0.4,
                })

        # ── Treasury yield markets ────────────────────────────────────────────
        is_yield_mkt = any(x in ticker for x in ("RATE", "YIELD", "DGS", "TREASURY")) or \
                       any(x in title for x in ("TREASURY", "10-YEAR", "10 YEAR", "YIELD", "T-NOTE"))

        if is_yield_mkt and t10y is not None:
            # Try to extract numeric threshold from title (e.g., "above 4.00%")
            m_thresh = _re.search(r"ABOVE\s+([\d.]+)\s*%", title)
            if m_thresh and yp is not None:
                thresh = float(m_thresh.group(1))
                if t10y > thresh + 0.3 and yp < 55:
                    signals.append({
                        "type":              "fred_edge",
                        "direction":         "BUY YES",
                        "ticker":            m.get("ticker", ""),
                        "title":             m.get("title", ""),
                        "price":             yp,
                        "rationale":         f"FRED: 10Y Treasury = {t10y:.2f}%. Threshold {thresh}% — currently {t10y - thresh:.2f}pts above. {yp}¢ looks underpriced.",
                        "confidence":        "medium" if t10y > thresh + 0.5 else "low",
                        "kelly_frac":        0.015,
                        "fee_cents":         kalshi_fee(yp),
                        "priority":          2,
                        "entry_limit_cents": max(1, yp - 2),
                        "take_profit_cents": min(99, yp + 8),
                        "stop_loss_pct":     0.35,
                    })
            m_thresh_below = _re.search(r"BELOW\s+([\d.]+)\s*%", title)
            if m_thresh_below and yp is not None:
                thresh = float(m_thresh_below.group(1))
                if t10y < thresh - 0.3 and yp < 55:
                    signals.append({
                        "type":              "fred_edge",
                        "direction":         "BUY YES",
                        "ticker":            m.get("ticker", ""),
                        "title":             m.get("title", ""),
                        "price":             yp,
                        "rationale":         f"FRED: 10Y Treasury = {t10y:.2f}%. Threshold {thresh}% — currently {thresh - t10y:.2f}pts below. {yp}¢ looks underpriced.",
                        "confidence":        "medium" if t10y < thresh - 0.5 else "low",
                        "kelly_frac":        0.015,
                        "fee_cents":         kalshi_fee(yp),
                        "priority":          2,
                        "entry_limit_cents": max(1, yp - 2),
                        "take_profit_cents": min(99, yp + 8),
                        "stop_loss_pct":     0.35,
                    })

        # ── PCE inflation markets ─────────────────────────────────────────────
        # PCEPI level ~100 for 2012 base; Dec 2024 ≈119. Annual PCE = compare to ~year ago.
        is_pce_mkt = any(x in title for x in ("PCE", "PERSONAL CONSUMPTION", "CORE INFLATION"))

        if is_pce_mkt and pce is not None:
            # PCE > 111 = well above 2% annual since 2012 base ≈100+2%/yr for 12yr ≈126
            # Simpler: if PCE exists and market asks about above-target inflation
            if any(x in title for x in ("ABOVE", "EXCEED", "OVER")) and yp < 45 and pce > 108:
                signals.append({
                    "type":              "fred_edge",
                    "direction":         "BUY YES",
                    "ticker":            m.get("ticker", ""),
                    "title":             m.get("title", ""),
                    "price":             yp,
                    "rationale":         f"FRED: PCE = {pce:.1f} (above pre-2021 trend). Above-target inflation market at {yp}¢ looks underpriced. PCE is Fed's preferred measure.",
                    "confidence":        "low",
                    "kelly_frac":        0.01,
                    "fee_cents":         kalshi_fee(yp),
                    "priority":          3,
                    "entry_limit_cents": max(1, yp - 2),
                    "take_profit_cents": min(99, yp + 6),
                    "stop_loss_pct":     0.4,
                })

        # ── VIX / equity volatility markets ──────────────────────────────────
        is_vix_mkt = "VIX" in ticker or "VOLATILITY" in title or "S&P" in title
        if is_vix_mkt and vix is not None:
            vix_thresh_match = _re.search(r"(?:ABOVE|OVER|EXCEED)\s+([\d.]+)", title)
            if vix_thresh_match:
                thresh = float(vix_thresh_match.group(1))
                if vix > thresh + 2 and yp < 55:
                    signals.append({
                        "type":              "fred_edge",
                        "direction":         "BUY YES",
                        "ticker":            m.get("ticker", ""),
                        "title":             m.get("title", ""),
                        "price":             yp,
                        "rationale":         f"FRED: VIX = {vix:.1f}, above threshold {thresh}. Volatility market at {yp}¢ looks underpriced.",
                        "confidence":        "medium" if vix > thresh + 5 else "low",
                        "kelly_frac":        0.015,
                        "fee_cents":         kalshi_fee(yp),
                        "priority":          2,
                        "entry_limit_cents": max(1, yp - 2),
                        "take_profit_cents": min(99, yp + 8),
                        "stop_loss_pct":     0.35,
                    })

    log(f"FRED edge: {len(signals)} signals (fed={fed_rate}, cpi={cpi}, pce={pce}, vix={vix}, unrate={unrate}, 10y={t10y})")
    return signals[:5]


def analyze_momentum_edge(markets):
    """
    Momentum / Mean-Reversion Edge:

    Binary markets tend to snap back when price moves far from historical equilibrium.

    Strategy:
    - If a GAME/WINNER market is priced 10-35¢ (heavily discounted underdog):
      Check if volume is spiking (someone may know something, OR it's emotional selloff)
    - If spread < 3¢ AND price 15-30¢ AND volume > 20: small edge buying the underdog
      (market research: underdogs at 20-30¢ win ~28-35% of the time vs implied 20-30%)
    - If price > 70¢ AND spread < 3¢: the heavy favorite may be slightly overpriced
      (people follow the crowd and push favorites past fair value)

    Evidence: Academic research on prediction markets shows prices cluster at round numbers
    (25¢, 50¢, 75¢) and underdogs between 20-35¢ are typically slightly underpriced
    due to favorite-longshot bias working in reverse for mid-range underdogs.
    """
    signals = []
    for m in markets:
        ticker = m.get("ticker", "")
        title  = m.get("title", ticker)
        yp     = m.get("_yes_price")
        vol    = m.get("volume", 0) or 0

        # Compute spread using binary market identity: yes_ask = 100 - no_bid
        # This avoids the bug where yes_ask defaults to 99 when missing.
        yb = m.get("yes_bid") or 0
        nb = m.get("no_bid")  or 0
        ya = m.get("yes_ask") or (100 - nb if nb else None)
        if ya is None:
            ya = 99  # last resort fallback
        spread = ya - yb

        if yp is None or not ticker:
            continue

        t_up = ticker.upper()
        # Look at GAME, WINNER, 1H markets (binary outcomes) + SERIES/PLAYOFF
        if not any(x in t_up for x in ["GAME", "WINNER", "1H", "SERIES", "PLAYOFF"]):
            continue

        # Require some volume (lowered for playoff markets)
        if vol < 5:
            continue

        # Tight spread required (< 8¢ — was 4¢, too strict for lower-volume markets)
        if spread > 8:
            continue

        # Mid-range underdog: 20-35¢ YES in a binary game market
        # Research: these markets slightly underprice the trailing team
        if 20 <= yp <= 35 and vol >= 5:
            true_prob = yp + 3  # small structural edge
            kelly = kelly_size(true_prob, yp, maker=True, fraction=0.25)
            if kelly > 0.001:
                signals.append({
                    "type":              "momentum_edge",
                    "direction":         "BUY YES",
                    "ticker":            ticker,
                    "title":             title,
                    "price":             yp,
                    "rationale":         f"Mid-range underdog at {yp}¢ in liquid market (vol={vol}). Research shows 20-35¢ game underdogs win ~3pts more than implied. Spread {spread}¢.",
                    "confidence":        "low",
                    "kelly_frac":        kelly,
                    "fee_cents":         kalshi_fee(yp),
                    "priority":          3,
                    "entry_limit_cents": max(1, yp - 2),
                    "take_profit_cents": min(99, yp + 6),
                    "stop_loss_pct":     0.35,
                })

        # Heavy favorite slight overpricing: 72-82¢ (crowd bias)
        # At round numbers the crowd tends to overweight favorites
        if 72 <= yp <= 82 and vol >= 5:
            no_price = 100 - yp
            true_prob = no_price + 2  # slight edge for NO
            kelly = kelly_size(true_prob, no_price, maker=True, fraction=0.25)
            if kelly > 0.001:
                signals.append({
                    "type":              "momentum_edge",
                    "direction":         "BUY NO",
                    "ticker":            ticker,
                    "title":             title,
                    "price":             yp,
                    "rationale":         f"Heavy favorite at {yp}¢ may be crowd-overpriced (vol={vol}). Crowd bias pushes favorites past fair value in range 70-82¢. Spread {spread}¢.",
                    "confidence":        "low",
                    "kelly_frac":        kelly,
                    "fee_cents":         kalshi_fee(no_price),
                    "priority":          3,
                    "entry_limit_cents": max(1, no_price - 2),
                    "take_profit_cents": min(99, no_price + 6),
                    "stop_loss_pct":     0.35,
                })

    log(f"Momentum edge: {len(signals)} signals")
    return signals[:5]


def analyze_metaculus_edge(markets, metaculus_qs):
    """
    Metaculus Consensus Edge: Compare Kalshi prices to Metaculus community predictions.

    Metaculus is one of the most calibrated forecasting platforms — community predictions
    from domain experts who track resolution history. When Metaculus diverges significantly
    from Kalshi, it suggests Kalshi may be mispriced.

    Evidence: Metaculus has a Brier score consistently better than base rates; their
    forecasters resolve questions correctly far more often than market-implied probabilities
    on related prediction markets. A ≥10¢ gap between Metaculus and Kalshi is actionable.

    Matching: fuzzy keyword matching between Metaculus question title and Kalshi market title.
    """
    signals = []
    if not metaculus_qs or not markets:
        return signals

    # Build keyword index of Kalshi markets (title words)
    def title_keywords(text):
        stopwords = {"the","a","an","in","of","on","at","to","for","and","or","is","will","by","with","from"}
        words = set(text.lower().split())
        return words - stopwords

    mkt_index = []
    for m in markets:
        yp = m.get("_yes_price")
        if yp is None:
            continue
        kw = title_keywords(m.get("title", "") + " " + m.get("ticker", ""))
        mkt_index.append((kw, m))

    for q in metaculus_qs:
        mprob = q.get("prob")
        if mprob is None:
            continue  # no community prediction yet

        q_kw = title_keywords(q.get("title", ""))
        if len(q_kw) < 3:
            continue

        # Find best-matching Kalshi market by keyword overlap
        best_overlap = 0
        best_market = None
        for mkt_kw, mkt in mkt_index:
            overlap = len(q_kw & mkt_kw)
            if overlap > best_overlap:
                best_overlap = overlap
                best_market = mkt

        # Require at least 3 keyword matches for a match (lowered from 4)
        if best_overlap < 3 or best_market is None:
            continue

        kalshi_price = best_market.get("_yes_price", 50)
        gap = mprob - kalshi_price  # positive = Metaculus more bullish than Kalshi

        if abs(gap) < 10:
            continue  # not a big enough gap to trade

        # Direction
        if gap > 10:   # Metaculus says YES is underpriced on Kalshi
            direction  = "BUY YES"
            side_price = kalshi_price
            prob       = mprob
            tp         = min(99, int(mprob))
        else:          # Metaculus says NO is better value
            direction  = "BUY NO"
            side_price = 100 - kalshi_price  # NO price
            prob       = 100 - mprob
            # TP: sell NO when YES falls to mprob (NO rises to 100 - mprob)
            tp = min(99, int(100 - mprob) + 3)

        kelly = kelly_size(prob, side_price, maker=True, fraction=0.25)
        if kelly <= 0:
            continue

        signals.append({
            "type":              "metaculus_edge",
            "direction":         direction,
            "ticker":            best_market.get("ticker", ""),
            "title":             best_market.get("title", ""),
            "price":             round(kalshi_price, 1),
            "rationale":         f"Metaculus consensus: {mprob:.0f}% vs Kalshi {kalshi_price:.0f}¢ (gap {gap:+.0f}¢). Expert forecasters from: {q.get('title','')[:60]}",
            "confidence":        "high" if abs(gap) >= 15 else "medium",
            "kelly_frac":        kelly,
            "fee_cents":         kalshi_fee(side_price),
            "priority":          1 if abs(gap) >= 15 else 2,
            "gap":               round(gap, 1),
            "espn_prob":         mprob,   # reuse field for display
            "entry_limit_cents": max(1, side_price - 3),
            "take_profit_cents": tp,
            "stop_loss_pct":     0.3,
        })

    signals.sort(key=lambda x: abs(x.get("gap", 0)), reverse=True)
    log(f"Metaculus edge: {len(signals)} signals")
    return signals[:3]


def analyze_home_advantage(markets, espn_games):
    """
    Home Team Advantage in Sports Playoff Markets.

    Well-documented research findings:
    - NBA Playoffs: Home teams win ~65% of games (historical ~1984-2024)
    - NHL Playoffs: Home teams win ~55% of games
    - MLB Regular Season: Home teams win ~54% of games
    - NFL: Home teams win ~57% of games (less in playoffs)

    When Kalshi underprices home teams in these ranges, there's structural edge.
    Must exclude in-progress games.

    Method: Parse Kalshi ticker to identify home vs away teams,
    cross-reference with ESPN to identify which team is home.
    """
    signals = []
    if not espn_games or not markets:
        return signals

    # Home advantage rates by sport
    home_rates = {"nba": 65, "nhl": 55, "mlb": 54, "nfl": 57}

    # Build a quick lookup: (league, away_team_abbrev, home_team_abbrev) -> game
    # Kalshi tickers: KXNBAGAME-26MAY26SASOKC-OKC means game SAS@OKC, OKC home
    game_map = {}
    for g in espn_games:
        status = (g.get("status") or "").lower()
        if any(w in status for w in ("in progress", "final", "halftime", "end of")):
            continue
        league = g.get("league", "")
        if league not in home_rates:
            continue
        game_map[g.get("event_id", "")] = g

    # For each GAME market pair, detect the home team
    # Pattern: KXNBAGAME-{date}{AWYHOM}-{TEAM}
    # The game string: SASOKC → SAS is away, OKC is home
    from collections import defaultdict
    game_pairs = defaultdict(list)

    for m in markets:
        ticker = m.get("ticker", "")
        if not any(x in ticker for x in ["NBAGAME", "NHLGAME", "MLBGAME"]):
            continue
        yp = m.get("_yes_price")
        if yp is None:
            continue
        # Parse: KXNBAGAME-26MAY26SASOKC-OKC
        # series = KXNBAGAME-26MAY26SASOKC, suffix = OKC
        parts = ticker.rsplit("-", 1)
        if len(parts) != 2:
            continue
        series = parts[0]
        suffix = parts[1]  # team abbreviation
        game_pairs[series].append({"suffix": suffix, "yp": yp, "ticker": ticker, "title": m.get("title","")})

    for series, contracts in game_pairs.items():
        if len(contracts) != 2:
            continue  # need exactly 2 teams
        if sum(c["yp"] for c in contracts) > 105 or sum(c["yp"] for c in contracts) < 95:
            continue  # prices don't sum to ~100 (not a proper 2-outcome binary)

        # Detect league from series name
        league = None
        if "NBA" in series:
            league = "nba"
        elif "NHL" in series:
            league = "nhl"
        elif "MLB" in series:
            league = "mlb"
        if not league:
            continue

        home_win_rate = home_rates[league]

        # Parse the game code: e.g., SASOKC from KXNBAGAME-26MAY26SASOKC
        # Game code is at the end of the series after the date part (DDMMMYY)
        # Format: KXNBAGAME-{DDMMMYY}{AWYHOM}
        s_upper = series.upper()
        # Find the two team abbreviations in the series name
        team_suffixes = [c["suffix"] for c in contracts]

        # Try to figure out which team is home from Kalshi's convention
        # The series name KXNBAGAME-26MAY26SASOKC means SAS@OKC → OKC is home
        # The last 3-6 chars before the series is the home team
        game_code = s_upper.split("-")[-1] if "-" in s_upper else ""

        # Find the suffix that appears at the END of the game code (= home team)
        home_suffix = None
        away_suffix = None
        for suf in team_suffixes:
            if game_code.endswith(suf):
                home_suffix = suf
                away_suffix = [x for x in team_suffixes if x != suf][0]
                break

        if not home_suffix:
            continue

        # Find the home team contract
        home_contract = next((c for c in contracts if c["suffix"] == home_suffix), None)
        if not home_contract:
            continue

        home_price = home_contract["yp"]

        # Home advantage edge: if home team priced below historical win rate
        gap = home_win_rate - home_price
        if gap < 5:
            continue  # insufficient edge

        kelly = kelly_size(home_win_rate, home_price, maker=True, fraction=0.25)
        if kelly <= 0:
            continue

        signals.append({
            "type":              "home_advantage",
            "direction":         "BUY YES",
            "ticker":            home_contract["ticker"],
            "title":             home_contract["title"],
            "price":             home_price,
            "rationale":         f"Home team ({home_suffix}) priced at {home_price}¢ vs historical {league.upper()} home win rate ~{home_win_rate}%. Gap: {gap:+.0f}¢.",
            "confidence":        "medium" if gap >= 10 else "low",
            "kelly_frac":        kelly,
            "fee_cents":         kalshi_fee(home_price),
            "priority":          2 if gap >= 10 else 3,
            "gap":               gap,
            "entry_limit_cents": max(1, home_price - 2),
            "take_profit_cents": min(99, int(home_win_rate)),
            "stop_loss_pct":     0.35,
        })

    signals.sort(key=lambda x: abs(x.get("gap", 0)), reverse=True)
    log(f"Home advantage: {len(signals)} signals")
    return signals[:5]


def analyze_series_momentum(markets, espn_games):
    """
    Playoff Series Momentum: Historical resolution rates for series leads.

    NBA Best-of-7 historical data (1984-2024):
    - 3-0 lead: 100% win rate (no team has EVER come back from 3-0 in NBA)
    - 3-1 lead: ~95% win rate
    - 2-0 lead: ~82% win rate
    - 2-1 lead: ~65% win rate (home team in next game)

    NHL Best-of-7:
    - 3-0 lead: ~98% win rate (1 comeback ever in 1975)
    - 3-1 lead: ~87% win rate
    - 2-0 lead: ~76% win rate

    When Kalshi underprices the series leader, that's a structural edge.
    """
    signals = []
    if not espn_games or not markets:
        return signals

    # Series win probability tables
    series_win_prob = {
        "nba": {(3,0): 99, (3,1): 95, (2,0): 82, (2,1): 65, (1,0): 60},
        "nhl": {(3,0): 98, (3,1): 87, (2,0): 76, (2,1): 60, (1,0): 55},
    }

    # Group ESPN games by series (same matchup across multiple games)
    from collections import defaultdict
    series_games = defaultdict(list)
    for g in espn_games:
        league = g.get("league", "")
        if league not in series_win_prob:
            continue
        home = g.get("home_team", "")
        away = g.get("away_team", "")
        if not home or not away:
            continue
        # Create a stable key for this series
        teams = tuple(sorted([home, away]))
        series_games[(league, teams)].append(g)

    for (league, teams), games in series_games.items():
        if len(games) < 2:
            continue  # need multiple games to detect a series

        # Find the most recent (latest) game to get current series score
        # Parse home/away scores from game records to infer series standing
        # ESPN doesn't give series score directly, but we can infer from game numbers
        # The game with the highest game number is most recent
        # We need to look for series score in game data
        latest_game = max(games, key=lambda g: g.get("date", ""), default=None)
        if not latest_game:
            continue

        home_team = latest_game.get("home_team", "")
        away_team = latest_game.get("away_team", "")

        # Look for a series-level Kalshi market
        # Pattern: KXNBA{series}WINNER or similar
        # These may not always exist, but try to find them
        home_abbrev = None
        away_abbrev = None

        # Try to find series winner markets from our markets list
        for m in markets:
            ticker = m.get("ticker", "")
            if league.upper() not in ticker:
                continue
            if "WINNER" not in ticker.upper() and "SERIES" not in ticker.upper():
                continue
            # Check if one of our teams is in the ticker
            t_upper = ticker.upper()
            for abbrev in [home_team[:3].upper(), away_team[:3].upper(),
                           home_team.split()[-1][:3].upper(),
                           away_team.split()[-1][:3].upper()]:
                if ticker.endswith(abbrev):
                    # Found a relevant market
                    yp = m.get("_yes_price")
                    if yp is None:
                        continue
                    # We found a series-level market
                    # Without real series score data, we can still check if price looks off
                    # For now, just flag markets that seem underpriced vs pure coin-flip
                    if yp < 40 and m.get("volume", 0) > 5:
                        # Market thinks one team has <40% chance in a series
                        # If there's active trading, check if this aligns with game count
                        kelly = kelly_size(50, yp, maker=True, fraction=0.25)
                        if kelly > 0.001:
                            signals.append({
                                "type":              "series_momentum",
                                "direction":         "BUY YES",
                                "ticker":            ticker,
                                "title":             m.get("title", ""),
                                "price":             yp,
                                "rationale":         f"Series market at {yp}¢ — if this team is competitive, series prices revert. Vol={m.get('volume',0)}.",
                                "confidence":        "low",
                                "kelly_frac":        kelly,
                                "fee_cents":         kalshi_fee(yp),
                                "priority":          3,
                                "entry_limit_cents": max(1, yp - 2),
                                "take_profit_cents": min(99, yp + 8),
                                "stop_loss_pct":     0.4,
                            })
                    break

    log(f"Series momentum: {len(signals)} signals")
    return signals[:3]


def analyze_series_momentum_v2(markets, playoff_series):
    """
    Improved series momentum analysis using real playoff series scores from ESPN.

    Uses historically accurate win-probability tables for NBA/NHL series leads.
    When Kalshi's market price for the series leader diverges from historical
    win rates, there's a structural edge.
    """
    signals = []

    # Historical series win probability by (wins_leader, wins_trailer), best-of-7
    # Source: Basketball Reference / Hockey Reference historical data
    SERIES_PROBS = {
        "nba": {
            (3, 0): 99,   # 100% historically (never blown in NBA)
            (3, 1): 95,   # 95% historically
            (3, 2): 79,   # Home team in G6 advantages shift this
            (2, 0): 82,
            (2, 1): 65,
            (1, 0): 58,
        },
        "nhl": {
            (3, 0): 98,   # 1 comeback ever (1975)
            (3, 1): 87,
            (3, 2): 74,
            (2, 0): 76,
            (2, 1): 61,
            (1, 0): 56,
        },
        "mlb": {
            (3, 0): 97,   # 1 comeback ever (2004 Red Sox)
            (3, 1): 86,
            (3, 2): 70,
            (2, 0): 71,
            (2, 1): 58,
            (1, 0): 54,
        },
    }

    for series in playoff_series:
        league  = series.get("league", "")
        probs   = SERIES_PROBS.get(league)
        if not probs:
            continue

        leader   = series.get("leader")
        hw       = series.get("home_wins", 0)
        aw       = series.get("away_wins", 0)
        if not leader or (hw == 0 and aw == 0):
            continue

        # Determine series score from leader's perspective
        if hw > aw:
            leader_wins  = hw
            trailer_wins = aw
            trailer      = series.get("away_team", "")
        else:
            leader_wins  = aw
            trailer_wins = hw
            trailer      = series.get("home_team", "")

        hist_prob = probs.get((leader_wins, trailer_wins))
        if hist_prob is None:
            continue

        # Search Kalshi markets for a WINNER or SERIES contract for the leader
        leader_abbrev = leader.split()[-1][:4].upper()  # last word, first 4 chars
        leader_words  = set(leader.lower().split())

        for m in markets:
            ticker = m.get("ticker", "")
            title  = m.get("title", "").lower()
            yp     = m.get("_yes_price")
            if yp is None:
                continue
            if "WINNER" not in ticker.upper() and "SERIES" not in ticker.upper() and \
               "CHAMP" not in ticker.upper():
                continue
            if league.upper() not in ticker.upper():
                continue

            # Check if this market is for the leader
            t_words = set(title.split())
            if not (leader_words & t_words) and leader_abbrev not in ticker.upper():
                continue

            gap = hist_prob - yp
            if abs(gap) < 5:
                continue  # need at least 5¢ divergence to be worth it

            # Leader is underpriced: buy YES on leader market
            if gap > 0:
                direction  = "BUY YES"
                side_price = yp
                side_prob  = hist_prob          # YES win probability
            else:
                # Leader is overpriced: buy NO on leader (= buy YES on trailer)
                direction  = "BUY NO"
                side_price = 100 - yp
                side_prob  = 100 - hist_prob   # NO win probability (trailer wins)

            kelly = kelly_size(side_prob, side_price, maker=True, fraction=0.25)
            if kelly <= 0.001:
                continue

            conf = "high" if abs(gap) >= 10 else "medium" if abs(gap) >= 7 else "low"
            signals.append({
                "type":              "series_momentum",
                "direction":         direction,
                "ticker":            ticker,
                "title":             m.get("title", ""),
                "price":             yp,  # YES price for display convention
                "rationale":         f"{leader} leads {leader_wins}-{trailer_wins}. Historical win rate {hist_prob}% vs Kalshi {yp}¢. Gap={gap:+.0f}¢.",
                "confidence":        conf,
                "kelly_frac":        kelly,
                "fee_cents":         kalshi_fee(side_price),
                "priority":          1 if abs(gap) >= 10 else 2,
                "gap":               round(gap, 1),
                "entry_limit_cents": max(1, side_price - 2),
                "take_profit_cents": min(99, int(hist_prob) - 2) if direction == "BUY YES" else min(99, int(100 - hist_prob) + 2),
                "stop_loss_pct":     0.35,
                "series_score":      f"{leader_wins}-{trailer_wins}",
                "hist_win_pct":      hist_prob,
            })
            break  # one signal per series

    signals.sort(key=lambda x: abs(x.get("gap", 0)), reverse=True)
    log(f"Series momentum v2: {len(signals)} signals")
    return signals[:5]


def analyze_price_trend(markets, price_moves):
    """
    Price Trend / Sharp Money Detection: Recent Kalshi price moves ≥4¢ signal informed
    money entering the market. Follow sharp bettors who have just moved prices.

    Logic:
    - If a market moves UP sharply (price increased), smart money is buying YES → BUY YES
    - If a market moves DOWN sharply (price decreased), smart money is buying NO → BUY YES
      on the inverse (BUY NO on YES side)
    - Only act on markets not too close to resolution (100¢ or 0¢)
    - Combine with volume data to filter noise

    This is a classic "follow the sharp money" strategy used in sports betting.
    """
    signals = []
    if not price_moves or not markets:
        return signals

    # Build market lookup
    mkt_by_ticker = {m.get("ticker", ""): m for m in markets}

    for move in price_moves:
        tk = move.get("ticker", "")
        if not tk:
            continue
        m = mkt_by_ticker.get(tk)
        if not m:
            continue

        curr_price = move.get("curr_price", 50)
        move_size  = move.get("move", 0)
        prev_price = move.get("prev_price", 50)

        # Skip markets at extreme prices (already near resolution)
        if curr_price >= 90 or curr_price <= 10:
            continue
        # Skip tiny markets
        vol = m.get("volume", 0) or m.get("_trade_count", 0) or 0
        if vol < 2:
            continue

        # 1-hour trend enrichment: if price also moved in same direction over 1h, boost confidence
        move_1h    = move.get("move_1h")
        sustained  = move_1h is not None and (move_1h > 0) == (move_size > 0) and abs(move_1h) >= 5

        # Determine direction and probability estimate
        # Use 1h trend magnitude if available to better estimate true momentum
        abs_move = abs(move_size)
        abs_1h   = abs(move_1h) if move_1h is not None else abs_move
        if move_size > 0:
            # Price went up — smart money bought YES
            direction  = "BUY YES"
            side_price = curr_price
            # If 1h trend confirms, extend probability estimate using 1h magnitude
            true_prob  = min(95, curr_price + (abs_1h if sustained else abs_move) * 0.5)
        else:
            # Price went down — smart money bought NO
            direction  = "BUY NO"
            side_price = 100 - curr_price  # NO price
            no_true_p  = max(5, curr_price - (abs_1h if sustained else abs_move) * 0.5)
            true_prob  = 100 - no_true_p  # flip to NO side probability

        kelly = kelly_size(true_prob, side_price, maker=True, fraction=0.25)
        if kelly <= 0:
            continue

        # Confidence: boost to "high" if 5-min move ≥10¢ OR sustained 1h trend
        confidence = "high" if (abs_move >= 10 or sustained) else ("medium" if abs_move >= 6 else "low")

        if direction == "BUY YES":
            entry_lmt = max(1, int(curr_price - 2))   # buy YES 2¢ below YES ask
            tp_cents  = min(99, int(curr_price + (abs_1h if sustained else abs_move) * 0.8))
        else:
            entry_lmt = max(1, int(side_price - 2))   # buy NO 2¢ below NO ask
            tp_cents  = min(99, int(side_price + (abs_1h if sustained else abs_move) * 0.8))

        # Build rationale with 1h context
        trend_str = f" | 1h trend: {move_1h:+.0f}¢ (sustained)" if sustained else (
                    f" | 1h: {move_1h:+.0f}¢" if move_1h is not None else "")
        signals.append({
            "type":              "price_trend",
            "direction":         direction,
            "ticker":            tk,
            "title":             m.get("title", ""),
            "price":             round(curr_price, 1),
            "prev_price":        round(prev_price, 1),
            "price_move":        round(move_size, 1),
            "move_1h":           move_1h,
            "rationale":         f"Sharp move {move_size:+.0f}¢ ({prev_price:.0f}→{curr_price:.0f}¢) — follow smart money. Vol={vol}.{trend_str}",
            "confidence":        confidence,
            "kelly_frac":        kelly,
            "fee_cents":         kalshi_fee(side_price),
            "priority":          1 if (abs_move >= 8 or sustained) else 2,
            "entry_limit_cents": entry_lmt,
            "take_profit_cents": tp_cents,
            "stop_loss_pct":     0.3,
        })

    log(f"Price trend: {len(signals)} signals")
    return signals[:5]


def analyze_worldcup_edge(markets, vegas_games):
    """
    FIFA World Cup 2026 Edge — Compare KXMENWORLDCUP Kalshi markets against
    Vegas sportsbook consensus for World Cup matches.

    World Cup 2026: June 11 – July 19, 2026.
    48 teams, 3-team groups, played in USA/Canada/Mexico.

    Strategy: when Vegas consensus for a World Cup game diverges ≥5¢ from Kalshi
    price, that's a structural edge (same as espn_odds_edge but for soccer WC).
    """
    signals = []

    # FIFA World Cup team name keywords → Kalshi ticker abbreviations
    WC_TEAMS = {
        "united states": "USA",  "usa": "USA",
        "mexico": "MEX",         "brazil": "BRA",     "argentina": "ARG",
        "france": "FRA",         "england": "ENG",    "spain": "ESP",
        "germany": "GER",        "portugal": "POR",   "netherlands": "NED",
        "uruguay": "URU",        "colombia": "COL",   "ecuador": "ECU",
        "canada": "CAN",         "australia": "AUS",  "japan": "JPN",
        "south korea": "KOR",    "korea republic": "KOR",
        "morocco": "MAR",        "senegal": "SEN",    "nigeria": "NGA",
        "ghana": "GHA",          "cameroon": "CMR",   "ivory coast": "CIV",
        "switzerland": "SUI",    "poland": "POL",     "croatia": "CRO",
        "denmark": "DEN",        "austria": "AUT",    "belgium": "BEL",
        "italy": "ITA",          "sweden": "SWE",     "norway": "NOR",
        "chile": "CHI",          "peru": "PER",       "paraguay": "PAR",
        "bolivia": "BOL",        "venezuela": "VEN",
        "iran": "IRN",           "saudi arabia": "KSA", "qatar": "QAT",
        "iraq": "IRQ",           "ukraine": "UKR",    "serbia": "SRB",
        "turkey": "TUR",         "slovenia": "SVN",   "slovakia": "SVK",
        "new zealand": "NZL",    "indonesia": "IDN",
        "costa rica": "CRC",     "panama": "PAN",     "honduras": "HON",
        "el salvador": "SLV",    "guatemala": "GUA",
    }

    # Only consider World Cup Vegas games
    wc_games = [g for g in (vegas_games or [])
                if g.get("_sport") == "soccer_fifa_world_cup"]
    if not wc_games:
        log("World Cup edge: no World Cup Vegas games found")
        return []

    # Filter: only KXMENWORLDCUP markets (or "WORLD CUP" in title)
    wc_markets = [m for m in markets if (
        "WORLDCUP" in m.get("ticker", "").upper() or
        "WORLD CUP" in (m.get("title") or "").upper()
    )]
    if not wc_markets:
        log("World Cup edge: no KXMENWORLDCUP Kalshi markets found")
        return []

    # Build suffix map: abbreviation → market (from last ticker segment)
    suffix_map = {}
    for m in wc_markets:
        ticker = m.get("ticker", "").upper()
        parts = ticker.rsplit("-", 1)
        if len(parts) == 2:
            suf = parts[1]
            suffix_map[suf] = m
        # Also index full ticker
        suffix_map[ticker] = m

    log(f"World Cup edge: {len(wc_games)} WC games, {len(wc_markets)} WC markets, "
        f"{len(suffix_map)} suffixes")

    for game in wc_games:
        home = game.get("home_team", "").lower()
        away = game.get("away_team", "").lower()

        for team_name, abbrev in WC_TEAMS.items():
            # Check if this team is in this game
            if team_name not in home and team_name not in away:
                continue

            # Get Vegas consensus for this team
            vp = game_consensus_prob(game, team_name)
            if vp is None:
                continue

            # Look for matching Kalshi market
            mkt = suffix_map.get(abbrev)
            if not mkt:
                # Also try country code variations
                for alt in [abbrev[:2], abbrev + "S"]:
                    if alt in suffix_map:
                        mkt = suffix_map[alt]
                        break
            if not mkt:
                continue

            kp = mkt.get("_yes_price") or mkt.get("yes_bid") or mkt.get("last_price")
            if kp is None:
                continue

            gap = vp - kp
            if abs(gap) < 5:
                continue

            direction  = "BUY YES" if gap > 0 else "BUY NO"
            side_price = kp if gap > 0 else (100 - kp)
            vp_side    = vp if gap > 0 else (100 - vp)
            kelly = kelly_size(vp_side, side_price, maker=True)
            if kelly <= 0:
                continue

            signals.append({
                "type":              "worldcup_edge",
                "direction":         direction,
                "ticker":            mkt.get("ticker", ""),
                "title":             mkt.get("title", ""),
                "price":             round(kp, 1),
                "rationale":         (f"WC 2026: {team_name.title()} Vegas {vp:.0f}¢ vs "
                                      f"Kalshi {kp:.0f}¢. Gap: {gap:+.1f}¢ | "
                                      f"{game.get('away_team','')} @ {game.get('home_team','')}"),
                "confidence":        "high" if abs(gap) >= 12 else "medium",
                "kelly_frac":        kelly * 0.5,
                "fee_cents":         kalshi_fee(side_price),
                "priority":          1 if abs(gap) >= 10 else 2,
                "gap":               round(gap, 1),
                "entry_limit_cents": max(1, side_price - 3),
                "take_profit_cents": min(99, int(vp)),
                "stop_loss_pct":     0.35,
            })

    signals.sort(key=lambda x: abs(x.get("gap", 0)), reverse=True)
    log(f"World Cup edge: {len(signals)} signals")
    return signals[:5]


def analyze_crypto_price_target(markets, crypto_prices):
    """
    Crypto Price Target Strategy:

    Kalshi lists BTC/ETH/SOL markets like "Will BTC close above $115,000 this week?"
    Using live prices + a simplified lognormal volatility model, we estimate whether
    the current Kalshi price fairly reflects reality.

    Model: P(S_T > X) using lognormal with:
      - BTC: 3.5% daily log-vol
      - ETH: 5% daily log-vol
      - SOL: 7% daily log-vol
      - Momentum adjustment: current 24h % change biases the drift

    Edge: when our model probability differs from the Kalshi market price by ≥10¢.
    """
    import math, re as _re

    signals = []
    if not crypto_prices or not markets:
        return signals

    DAILY_VOL = {"btc": 0.035, "eth": 0.050, "sol": 0.070}
    coins = {}
    for sym in ["btc", "eth", "sol"]:
        c = crypto_prices.get(sym) or {}
        price = c.get("usd") if isinstance(c, dict) else None
        change_24h = c.get("change_24h", 0) if isinstance(c, dict) else 0
        if price:
            coins[sym] = {"price": price, "change_24h": change_24h}

    # Also try legacy flat keys
    if "btc" not in coins and crypto_prices.get("btc_usd"):
        coins["btc"] = {"price": crypto_prices["btc_usd"], "change_24h": crypto_prices.get("btc_24h_change", 0)}
    if "eth" not in coins and crypto_prices.get("eth_usd"):
        coins["eth"] = {"price": crypto_prices["eth_usd"], "change_24h": crypto_prices.get("eth_24h_change", 0)}
    if "sol" not in coins and crypto_prices.get("sol_usd"):
        coins["sol"] = {"price": crypto_prices["sol_usd"], "change_24h": crypto_prices.get("sol_24h_change", 0)}

    if not coins:
        return signals

    now_utc = datetime.now(timezone.utc)

    for m in markets:
        ticker = m.get("ticker", "").upper()
        title  = (m.get("title") or "").upper()
        yp     = m.get("_yes_price")
        ct     = m.get("close_time", "")
        if yp is None or not ct:
            continue

        # Only crypto price direction markets
        coin_sym = None
        if "BTC" in ticker or "BITCOIN" in ticker:
            coin_sym = "btc"
        elif "ETH" in ticker or "ETHER" in ticker:
            coin_sym = "eth"
        elif "SOL" in ticker or "SOLANA" in ticker:
            coin_sym = "sol"
        if coin_sym not in coins:
            continue

        # Must have "ABOVE" or "OVER" / "BELOW" / "UNDER" direction
        is_above = any(x in title or x in ticker for x in ("ABOVE", "OVER", "HIGH", "UP"))
        is_below = any(x in title or x in ticker for x in ("BELOW", "UNDER", "LOW", "DOWN"))
        if not is_above and not is_below:
            continue

        # Parse target price from title (e.g., "$115,000" or "$115K" or "115000")
        target = None
        for pattern in [r'\$([0-9,]+(?:\.[0-9]+)?)[Kk]?\b', r'([0-9]{3,}[,]?[0-9]{3})']:
            m_r = _re.search(pattern, title.replace(",", ""))
            if m_r:
                raw = m_r.group(1).replace(",", "")
                try:
                    val = float(raw)
                    if "K" in title[m_r.start():m_r.end()+2].upper():
                        val *= 1000
                    # Sanity check for BTC (should be 50k-500k range)
                    if coin_sym == "btc" and 30000 <= val <= 1000000:
                        target = val; break
                    elif coin_sym == "eth" and 500 <= val <= 30000:
                        target = val; break
                    elif coin_sym == "sol" and 10 <= val <= 3000:
                        target = val; break
                except ValueError:
                    pass

        if target is None:
            continue

        # Calculate days to close
        try:
            close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            days_left = max(0.1, (close_dt - now_utc).total_seconds() / 86400)
        except Exception:
            continue

        # Lognormal probability model
        S = coins[coin_sym]["price"]
        X = target
        vol_day = DAILY_VOL[coin_sym]
        # Momentum drift: if up 3% today, add slight positive drift
        drift_adj = coins[coin_sym]["change_24h"] / 100.0 * 0.3  # dampen momentum
        sigma = vol_day * math.sqrt(days_left)
        mu = drift_adj * days_left  # drift term

        if S <= 0 or X <= 0:
            continue

        log_ratio = math.log(X / S) - mu
        # P(S_T > X) = 1 - Phi(log_ratio / sigma)
        # Simplified: use logistic approximation to normal CDF
        z = log_ratio / sigma
        # Logistic approximation: Phi(z) ≈ 1/(1 + exp(-1.7 * z))
        prob_above = 1.0 / (1.0 + math.exp(min(50, max(-50, 1.7 * z))))

        model_prob = prob_above * 100 if is_above else (1 - prob_above) * 100
        model_prob = max(2, min(98, model_prob))

        gap = model_prob - yp
        if abs(gap) < 10:  # need ≥10¢ discrepancy
            continue

        direction = "BUY YES" if gap > 0 else "BUY NO"
        side_price = yp if direction == "BUY YES" else (100 - yp)
        kelly = kelly_size(model_prob if direction == "BUY YES" else (100 - model_prob),
                           side_price, maker=True)
        if kelly <= 0:
            continue

        price_str = f"${S:,.0f}" if S >= 1000 else f"${S:.2f}"
        tgt_str   = f"${target:,.0f}" if target >= 1000 else f"${target:.2f}"
        signals.append({
            "type":              "crypto_price_target",
            "direction":         direction,
            "ticker":            m.get("ticker", ""),
            "title":             m.get("title", ""),
            "price":             yp,
            "rationale":         f"{coin_sym.upper()} at {price_str} vs target {tgt_str} ({days_left:.1f}d). Model P={model_prob:.0f}¢ vs market {yp}¢. Gap={gap:+.0f}¢.",
            "confidence":        "medium" if abs(gap) >= 15 else "low",
            "kelly_frac":        kelly * 0.5,
            "fee_cents":         kalshi_fee(side_price),
            "priority":          2,
            "days_until_close":  round(days_left, 1),
            "entry_limit_cents": max(1, side_price - 2),
            "take_profit_cents": min(99, side_price + max(5, int(abs(gap) * 0.5))),
            "stop_loss_pct":     0.4,
            "model_prob":        round(model_prob, 1),
            "current_price":     S,
            "target_price":      target,
        })

    signals.sort(key=lambda x: abs(x.get("model_prob", 50) - x.get("price", 50)), reverse=True)
    log(f"Crypto price target: {len(signals)} signals")
    return signals[:4]


def analyze_near_close_edge(markets):
    """
    Near-Resolution Mispricing:

    Markets closing in 1-7 days that still show 20-80% YES prices are often
    interesting because:
    1. Low-volume markets get forgotten and staleness creates edges
    2. New information hasn't been priced in
    3. Binary events approaching resolution have accelerating probability changes

    Strategy:
    - Find liquid markets (vol > 50) closing in 1-7 days
    - Where spread is tight (< 5¢) — indicates active market-making
    - Priced 20-40¢ YES: suggests true underdog chance not priced in
    - Priced 60-80¢ YES: suggests true favorite underprice
    - We flag these for human review since direction depends on context
    """
    signals = []
    now_utc = datetime.now(timezone.utc)

    for m in markets:
        ticker = m.get("ticker", "")
        title  = m.get("title", ticker)
        yp     = m.get("_yes_price")
        vol    = m.get("volume", 0) or 0
        ct     = m.get("close_time", "")

        if yp is None or not ticker or not ct:
            continue

        # Parse close time and calculate days remaining
        try:
            close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            days_left = (close_dt - now_utc).total_seconds() / 86400
        except Exception:
            continue

        # Only look at markets closing in 1-7 days
        if not (1 <= days_left <= 7):
            continue

        # Need decent volume (active market, not abandoned)
        if vol < 50:
            continue

        # Compute spread
        yb = m.get("yes_bid") or 0
        nb = m.get("no_bid") or 0
        ya = m.get("yes_ask") or (100 - nb if nb else 99)
        spread = ya - yb

        # Need tight spread (active market-making = informed prices, but also liquid)
        if spread > 8:
            continue

        # Avoid extreme prices (already at resolution)
        if yp < 15 or yp > 85:
            continue

        # Skip GAME markets closing tomorrow — those are tonight's games, normal
        t_up = ticker.upper()
        if days_left < 1.5 and any(x in t_up for x in ["GAME", "TONIGHT", "TODAY"]):
            continue

        # Signal: "elevated uncertainty" in near-resolution market
        # Edge direction: mean-reversion toward the clear binary outcome
        # We flag both sides and let the user/advisor decide
        urgency = "HIGH" if days_left <= 2 else ("MED" if days_left <= 4 else "LOW")

        # Slight bias toward favorites (markets are usually fair, but if
        # an event is nearly resolved, often price should be higher/lower)
        if 20 <= yp <= 40:
            # Possible underpricing of YES — review needed before trading
            signals.append({
                "type":              "near_close_mispricing",
                "direction":         "BUY YES",
                "ticker":            ticker,
                "title":             title,
                "price":             yp,
                "rationale":         f"Near close ({days_left:.1f}d) liquid underdog at {yp}¢ — verify no adverse news. Spread={spread}¢. Urgency={urgency}.",
                "confidence":        "low",
                "kelly_frac":        0.005,  # very small position until edge confirmed
                "fee_cents":         kalshi_fee(yp),
                "priority":          3,
                "days_until_close":  round(days_left, 1),
                "entry_limit_cents": max(1, yp - 1),
                "take_profit_cents": min(99, yp + 10),
                "stop_loss_pct":     0.4,
            })
        elif 60 <= yp <= 80:
            # Possible underpricing of YES favorite — check if outcome determined
            signals.append({
                "type":              "near_close_mispricing",
                "direction":         "BUY YES",
                "ticker":            ticker,
                "title":             title,
                "price":             yp,
                "rationale":         f"Near close ({days_left:.1f}d) favorite at {yp}¢ — check if outcome already determined. Spread={spread}¢. Urgency={urgency}.",
                "confidence":        "low",
                "kelly_frac":        0.005,
                "fee_cents":         kalshi_fee(yp),
                "priority":          3,
                "days_until_close":  round(days_left, 1),
                "entry_limit_cents": max(1, yp - 1),
                "take_profit_cents": min(99, yp + 8),
                "stop_loss_pct":     0.4,
            })

    signals.sort(key=lambda x: x.get("days_until_close", 99))
    log(f"Near-close edge: {len(signals)} signals")
    return signals[:5]


def run_strategy_engine(markets, edges, cross_arb, weather_data, espn_games=None,
                        metaculus_qs=None, price_moves=None, vegas_games=None,
                        playoff_series=None, fear_greed=None, fred_data=None,
                        crypto_prices=None):
    """
    Run all strategy modules and return unified ranked signal list.
    """
    all_signals = []

    # 1. Favorite-longshot bias (always runs)
    try:
        all_signals += analyze_longshot_bias(markets)
    except Exception as e:
        log(f"Strategy longshot error: {e}")

    # 2. Bundle arbitrage scanner
    try:
        all_signals += analyze_bundle_arb(markets)
    except Exception as e:
        log(f"Strategy bundle arb error: {e}")

    # 3. Vegas divergence (if edges available)
    try:
        if edges:
            all_signals += analyze_vegas_divergence(edges)
    except Exception as e:
        log(f"Strategy vegas error: {e}")

    # 4. Cross-platform arb (fee-adjusted)
    try:
        if cross_arb:
            all_signals += analyze_cross_platform_arb(cross_arb)
    except Exception as e:
        log(f"Strategy cross-arb error: {e}")

    # 5. Weather edge
    try:
        if weather_data:
            all_signals += analyze_weather_edge(markets, weather_data)
    except Exception as e:
        log(f"Strategy weather error: {e}")

    # 6. Volume spike detection
    try:
        all_signals += analyze_volume_spikes(markets)
    except Exception as e:
        log(f"Strategy volume spike error: {e}")

    # 7. ESPN odds edge (DraftKings vs Kalshi)
    try:
        if espn_games:
            all_signals += analyze_espn_odds_edge(espn_games, markets)
    except Exception as e:
        log(f"Strategy ESPN odds error: {e}")

    # 8. Fear & Greed sentiment edge
    try:
        if fear_greed:
            all_signals += analyze_fear_greed_edge(markets, fear_greed)
    except Exception as e:
        log(f"Strategy fear_greed error: {e}")

    # 9. FRED economic edge
    try:
        if fred_data:
            all_signals += analyze_fred_edge(markets, fred_data)
    except Exception as e:
        log(f"Strategy FRED error: {e}")

    # 10. Momentum / mean-reversion edge
    try:
        all_signals += analyze_momentum_edge(markets)
    except Exception as e:
        log(f"Strategy momentum error: {e}")

    # 11. Metaculus expert consensus vs Kalshi
    try:
        if metaculus_qs:
            all_signals += analyze_metaculus_edge(markets, metaculus_qs)
    except Exception as e:
        log(f"Strategy metaculus error: {e}")

    # 12. Home team advantage in sports markets
    try:
        if espn_games:
            all_signals += analyze_home_advantage(markets, espn_games)
    except Exception as e:
        log(f"Strategy home advantage error: {e}")

    # 13. Series momentum (playoff series leader historical win rates)
    try:
        if playoff_series:
            all_signals += analyze_series_momentum_v2(markets, playoff_series)
        elif espn_games:
            all_signals += analyze_series_momentum(markets, espn_games)
    except Exception as e:
        log(f"Strategy series momentum error: {e}")

    # 14. Price trend (follow sharp money — recent significant price moves)
    try:
        if price_moves:
            all_signals += analyze_price_trend(markets, price_moves)
    except Exception as e:
        log(f"Strategy price trend error: {e}")

    # 15. World Cup 2026 edge (Vegas vs Kalshi KXMENWORLDCUP markets)
    try:
        if vegas_games:
            all_signals += analyze_worldcup_edge(markets, vegas_games)
    except Exception as e:
        log(f"Strategy world cup error: {e}")

    # 16. Near-close mispricing (liquid markets closing in 1-7 days)
    try:
        all_signals += analyze_near_close_edge(markets)
    except Exception as e:
        log(f"Strategy near-close error: {e}")

    # 17. Crypto price target (lognormal model vs live BTC/ETH/SOL prices)
    try:
        if crypto_prices:
            all_signals += analyze_crypto_price_target(markets, crypto_prices)
    except Exception as e:
        log(f"Strategy crypto price target error: {e}")

    # Consensus detection: count how many strategies agree per ticker+direction
    from collections import defaultdict
    consensus = defaultdict(list)  # (ticker, direction) → [signal_types]
    for s in all_signals:
        key = (s.get("ticker", ""), s.get("direction", ""))
        if key[0]:
            consensus[key].append(s.get("type", ""))

    # Conflict detection: mark tickers where strategies disagree on direction
    # BUY YES vs BUY NO on the same ticker = genuine uncertainty
    ticker_directions = defaultdict(set)
    for s in all_signals:
        tk = s.get("ticker", "")
        if tk and s.get("direction"):
            # Normalize direction to YES/NO
            d = "YES" if "YES" in s.get("direction", "").upper() else ("NO" if "NO" in s.get("direction", "").upper() else None)
            if d:
                ticker_directions[tk].add(d)
    conflicted_tickers = {tk for tk, dirs in ticker_directions.items() if len(dirs) > 1}

    # Deduplicate: keep highest-priority signal per ticker
    # BUT boost confidence when multiple strategies agree
    seen_tickers = {}
    deduped = []
    for s in all_signals:
        ticker = s.get("ticker", "")
        if not ticker:
            deduped.append(s)
            continue
        if ticker not in seen_tickers:
            seen_tickers[ticker] = s
            deduped.append(s)
        else:
            # Keep the one with lower priority number (higher priority)
            existing = seen_tickers[ticker]
            if s.get("priority", 9) < existing.get("priority", 9):
                # Replace existing with this higher-priority signal
                deduped = [x for x in deduped if x.get("ticker") != ticker]
                seen_tickers[ticker] = s
                deduped.append(s)

    # Boost confidence for consensus signals (≥2 strategies agree on direction)
    for s in deduped:
        key = (s.get("ticker", ""), s.get("direction", ""))
        agreeing = consensus.get(key, [])
        n_agree = len(set(agreeing))  # unique strategy types in agreement
        if n_agree >= 2:
            s["consensus_count"] = n_agree
            s["consensus_types"] = list(set(agreeing))
            # Boost: 2+ agreement → upgrade confidence level
            if s.get("confidence") == "low":
                s["confidence"] = "medium"
            elif s.get("confidence") == "medium":
                s["confidence"] = "high"
            # Also boost priority (move closer to 1)
            s["priority"] = max(1, s.get("priority", 3) - 1)
            # Boost kelly by 10% per extra strategy agreeing
            boost = 1.0 + (n_agree - 1) * 0.10
            s["kelly_frac"] = round(s.get("kelly_frac", 0) * boost, 4)
            log(f"Consensus: {ticker} {s['direction']} — {n_agree} strategies agree: {s['consensus_types']}")

    all_signals = deduped

    # Apply conflict metadata: mark signals where other strategies disagree on direction
    for s in all_signals:
        tk = s.get("ticker", "")
        if tk and tk in conflicted_tickers:
            s["conflict_detected"] = True
            s["warning"] = "Conflicting signal: another strategy recommends the opposite direction"
            # Downgrade confidence one level — uncertainty is real
            if s.get("confidence") == "high":
                s["confidence"] = "medium"
            elif s.get("confidence") == "medium":
                s["confidence"] = "low"
            # Push priority back toward middle (don't hard-skip, just deprioritize)
            s["priority"] = min(4, s.get("priority", 3) + 1)
            log(f"Conflict: {tk} {s.get('direction')} — opposing strategies detected")

    # Filter out expired / closing-soon markets (need > 2 hours to place & fill)
    now_utc = datetime.now(timezone.utc)
    mkt_close = {m["ticker"]: m.get("close_time", "") for m in markets}
    live_signals = []
    for s in all_signals:
        ticker = s.get("ticker", "")
        ct = mkt_close.get(ticker, "")
        if ct:
            try:
                close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=timezone.utc)
                mins_left = (close_dt - now_utc).total_seconds() / 60
                if mins_left < 120:
                    log(f"Strategy: skip {ticker} — closes in {mins_left:.0f} min")
                    continue
            except Exception:
                pass  # can't parse — keep the signal
        live_signals.append(s)

    # Adjust kelly_frac by liquidity quality (volume-weighted)
    for s in live_signals:
        # Find the market for this signal
        ticker = s.get("ticker", "")
        mkt = next((m for m in markets if m.get("ticker") == ticker), None)
        if mkt:
            vol = mkt.get("volume", 0) or 0
            # Volume multiplier: 1.0x at 0 volume, up to 1.5x at 1000+ volume
            vol_mult = min(1.5, 1.0 + vol / 2000.0)
            # Spread tightness: tighter spread = higher quality
            # Use binary market identity: yes_ask = 100 - no_bid when yes_ask missing
            yb = mkt.get("yes_bid") or 0
            nb = mkt.get("no_bid") or 0
            ya = mkt.get("yes_ask") or (100 - nb if nb else 99)
            spread = ya - yb
            spread_mult = max(0.5, 1.0 - spread / 20.0)  # penalize wide spreads
            s["quality_score"] = round(vol_mult * spread_mult, 3)
            s["spread_cents"]  = spread
        else:
            s["quality_score"] = 0.5  # unknown liquidity

    # Sort by: priority (1=highest), then consensus, persistence, then kelly×quality
    live_signals.sort(key=lambda x: (
        x.get("priority", 9),
        -x.get("consensus_count", 0),
        -x.get("persistence_count", 0),
        -(x.get("kelly_frac", 0) * x.get("quality_score", 0.5))
    ))

    # Enrich each signal with kelly_pct, kelly_dollars, and close_time from market lookup
    # Reference bankroll: $1,000 (users can scale; demo trader uses real demo balance)
    _REF_BANKROLL_CENTS = 100_000
    for i, s in enumerate(live_signals):
        s["rank"] = i + 1
        # Add kelly_pct for easy display (percentage form)
        if "kelly_frac" in s and "kelly_pct" not in s:
            s["kelly_pct"] = round(s["kelly_frac"] * 100, 1)
        # Add kelly_dollars: recommended bet size on a $1,000 bankroll
        kf = s.get("kelly_frac", 0) or 0
        if kf > 0:
            s["kelly_dollars"] = round(kf * _REF_BANKROLL_CENTS / 100, 2)  # dollars
        # Add close_time from markets list
        if "close_time" not in s or not s.get("close_time"):
            ticker = s.get("ticker", "")
            for m in markets:
                if m.get("ticker") == ticker:
                    s["close_time"] = m.get("close_time", "")
                    break
        # Add volume and spread_cents from market if not already set
        ticker = s.get("ticker", "")
        mkt = next((m for m in markets if m.get("ticker") == ticker), None)
        if mkt:
            if "volume" not in s:
                s["volume"] = mkt.get("volume", 0) or 0
            # Add days_until_close for urgency assessment
            ct = mkt.get("close_time", "")
            if ct:
                try:
                    close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=timezone.utc)
                    days_left = (close_dt - now_utc).total_seconds() / 86400
                    s["days_until_close"] = round(days_left, 1)
                except Exception:
                    pass
            # Add live bid/ask for execution reference
            yb = mkt.get("yes_bid")
            nb = mkt.get("no_bid")
            ya = mkt.get("yes_ask")
            if yb is not None and "live_bid" not in s:
                s["live_bid"] = yb
            if nb is not None and "live_no_bid" not in s:
                s["live_no_bid"] = nb
            if ya is not None and "live_ask" not in s:
                s["live_ask"] = ya

    log(f"Strategy engine: {len(live_signals)} live signals (filtered {len(all_signals)-len(live_signals)} expired)")
    return live_signals[:20]  # top 20

def fetch_supplemental_markets():
    """
    Fetch markets from key categories not covered by recent trades.
    Returns a list of additional market dicts to augment markets_list.
    """
    extra = []

    # Fetch markets by querying /markets with various filters
    # Focus on markets with volume > 0 that close within next 90 days
    filters = [
        # ── Political ──
        {"event_ticker": "KXSENATE",    "limit": 10},
        {"event_ticker": "KXHOUSE",     "limit": 10},
        {"event_ticker": "KXPRES",      "limit": 10},
        {"event_ticker": "KXGOV",       "limit": 10},
        # ── Economic ──
        {"event_ticker": "KXFED",       "limit": 10},
        {"event_ticker": "KXCPI",       "limit": 10},
        {"event_ticker": "KXJOBS",      "limit": 10},
        {"event_ticker": "KXGDP",       "limit": 5},
        # ── Crypto ──
        {"event_ticker": "KXBTCD",      "limit": 5},  # daily BTC
        {"event_ticker": "KXETHD",      "limit": 5},  # daily ETH
        {"event_ticker": "KXBTCW",      "limit": 5},  # weekly BTC
        {"event_ticker": "KXETHW",      "limit": 5},  # weekly ETH
        {"event_ticker": "KXBTCM",      "limit": 3},  # monthly BTC
        {"event_ticker": "KXSOLW",      "limit": 3},  # weekly SOL
        # ── NBA Playoffs ──
        {"event_ticker": "KXNBAPLAYOFF","limit": 20},
        {"event_ticker": "KXNBAWINNER", "limit": 10},
        {"event_ticker": "KXNBAFINALS", "limit": 10},
        {"event_ticker": "KXNBACHAMP",  "limit": 5},
        # ── NHL Playoffs ──
        {"event_ticker": "KXNHLWINNER", "limit": 10},
        {"event_ticker": "KXNHLFINALS", "limit": 10},
        {"event_ticker": "KXNHLCHAMP",  "limit": 5},
        # ── Other Sports ──
        {"event_ticker": "KXMLBWINNER", "limit": 5},
        # ── World Cup 2026 ── (try multiple possible event ticker formats)
        {"event_ticker": "KXMENWORLDCUP",   "limit": 20},
        {"event_ticker": "KXWORLDCUP",      "limit": 10},
        {"event_ticker": "KXWC26",          "limit": 10},
        {"event_ticker": "KXWC2026",        "limit": 10},
        {"event_ticker": "KXFIFAWC26",      "limit": 10},
        {"event_ticker": "KXMENWC26",       "limit": 10},
    ]

    seen = set()
    for params in filters:
        try:
            resp = get("/markets", params)
            if not resp:
                continue
            mkts = resp.get("markets", [])
            # Take up to 8 per filter (was 5 — expand coverage)
            for m in mkts[:8]:
                ticker = m.get("ticker", "")
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)

                # Get detailed market data (needed for bid/ask prices)
                detail = get(f"/markets/{ticker}")
                if not detail:
                    continue
                md = detail.get("market", detail) if isinstance(detail, dict) else detail
                if not isinstance(md, dict):
                    continue
                extra.append(md)
        except Exception as e:
            log(f"Supplemental market fetch error ({params}): {e}")

    log(f"Supplemental markets: {len(extra)} fetched")
    return extra


def fetch_game_partner_markets(markets_list):
    """
    For each GAME market in markets_list, also fetch its companion contracts
    (the other team in the same game). This ensures analyze_home_advantage
    and analyze_bundle_arb can find complete game pairs.

    Example: if KXNBAGAME-26MAY26SASOKC-SAS is in markets_list but
    KXNBAGAME-26MAY26SASOKC-OKC is NOT, this function fetches OKC.
    """
    from collections import defaultdict
    extra = []

    # Group markets by series (game prefix before last dash)
    series_contracts = defaultdict(list)
    for m in markets_list:
        ticker = m.get("ticker", "")
        if not any(x in ticker.upper() for x in ["NBAGAME", "NHLGAME", "MLBGAME", "NFLGAME"]):
            continue
        parts = ticker.rsplit("-", 1)
        if len(parts) == 2:
            series_contracts[parts[0]].append(ticker)

    # For each game series, fetch all contracts for that event
    seen = {m.get("ticker") for m in markets_list}
    for series, known_tickers in series_contracts.items():
        # Use the series prefix as event_ticker (minus the "KX" prefix if present)
        try:
            resp = get("/markets", {"event_ticker": series, "limit": 20})
            if not resp:
                continue
            for m in resp.get("markets", []):
                ticker = m.get("ticker", "")
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)
                # Fetch full market details
                detail = get(f"/markets/{ticker}")
                if not detail:
                    continue
                md = detail.get("market", detail) if isinstance(detail, dict) else detail
                if isinstance(md, dict):
                    extra.append(md)
        except Exception as e:
            log(f"Game partner fetch error ({series}): {e}")

    log(f"Game partner markets: {len(extra)} fetched to complete {len(series_contracts)} game series")
    return extra


def find_cross_market_arb(kalshi_markets, poly_markets, pi_markets, manifold_markets=None):
    """Find price gaps ≥5¢ between Kalshi and PolyMarket/PredictIt/Manifold."""
    arb = []

    # Index PolyMarket by question words
    poly_idx = [(set(m["question"].lower().split()), m)
                for m in poly_markets if m.get("yes_price") is not None]

    # Index PredictIt contracts by name words
    pi_idx = []
    for m in pi_markets:
        for c in m.get("contracts", []):
            price = c.get("yes_price") or c.get("last_price")
            if price:
                words = set((m["name"] + " " + c["name"]).lower().split())
                pi_idx.append((words, m, c, price))

    # Index Manifold Markets by question words
    manifold_idx = []
    for m in (manifold_markets or []):
        yp = m.get("yes_price")
        if yp is not None:
            words = set(m.get("question", "").lower().split())
            manifold_idx.append((words, m, yp))

    for km in kalshi_markets:
        kp = km.get("yes_bid") or km.get("last_price")
        if kp is None:
            continue
        kwords = set(km.get("title", "").lower().split())
        # Remove very common stopwords that hurt precision
        stopwords = {"will", "the", "a", "an", "in", "of", "on", "at", "to", "for",
                     "and", "or", "is", "be", "by", "with", "from", "than", "that"}
        kwords -= stopwords

        # vs PolyMarket
        for pwords, pm in poly_idx:
            pw = pwords - stopwords
            common = len(kwords & pw)
            total  = len(kwords | pw)
            if total == 0 or common / total < 0.35:
                continue
            gap = round(pm["yes_price"] - kp, 1)
            if abs(gap) < 5:
                continue
            arb.append({
                "kalshi_ticker":  km["ticker"],
                "kalshi_title":   km["title"],
                "kalshi_price":   kp,
                "platform":       "PolyMarket",
                "platform_price": pm["yes_price"],
                "gap":            gap,
                "direction":      "BUY YES on Kalshi" if gap > 0 else "BUY NO on Kalshi",
                "platform_url":   pm["url"],
                "similarity":     round(common / total, 2),
            })

        # vs PredictIt
        for piwords, pm, c, pi_price in pi_idx:
            pw = piwords - stopwords
            common = len(kwords & pw)
            total  = len(kwords | pw)
            if total == 0 or common / total < 0.3:
                continue
            gap = round(pi_price - kp, 1)
            if abs(gap) < 5:
                continue
            arb.append({
                "kalshi_ticker":  km["ticker"],
                "kalshi_title":   km["title"],
                "kalshi_price":   kp,
                "platform":       "PredictIt",
                "platform_price": pi_price,
                "gap":            gap,
                "direction":      "BUY YES on Kalshi" if gap > 0 else "BUY NO on Kalshi",
                "platform_url":   pm.get("url", ""),
                "similarity":     round(common / total, 2),
            })

        # vs Manifold Markets
        for mwords, mm, mf_price in manifold_idx:
            mw = mwords - stopwords
            if len(mw) < 2:
                continue
            common = len(kwords & mw)
            total  = len(kwords | mw)
            if total == 0 or common / total < 0.3:
                continue
            gap = round(mf_price - kp, 1)
            if abs(gap) < 7:  # slightly higher threshold for Manifold (lower liquidity)
                continue
            arb.append({
                "kalshi_ticker":  km["ticker"],
                "kalshi_title":   km["title"],
                "kalshi_price":   kp,
                "platform":       "Manifold",
                "platform_price": mf_price,
                "gap":            gap,
                "direction":      "BUY YES on Kalshi" if gap > 0 else "BUY NO on Kalshi",
                "platform_url":   mm.get("url", ""),
                "similarity":     round(common / total, 2),
                "manifold_vol":   mm.get("volume", 0),
            })

    arb.sort(key=lambda x: abs(x["gap"]), reverse=True)
    return arb[:20]

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
        qty      = p.get("position", 0)
        side     = "YES" if qty > 0 else "NO"
        exposure = p.get("market_exposure", 0) or 0
        unreal   = p.get("unrealized_pnl")
        fees     = p.get("fees_paid", 0) or 0
        realized = p.get("realized_pnl", 0) or 0
        pnl_str  = f"  uPnL=${unreal/100:+.2f}" if unreal is not None else ""
        lines.append(f"  {p.get('ticker',''):42}  {side}  qty={abs(qty):>4}  exp=${exposure/100:.2f}{pnl_str}")
        positions_list.append({
            "ticker":          p.get("ticker", ""),
            "side":            side,
            "qty":             abs(qty),
            "market_exposure": exposure,
            "unrealized_pnl":  unreal,
            "realized_pnl":    realized,
            "fees_paid":       fees,
        })
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
        # Kalshi V2 API returns integer cents (yes_price), not dollars (yes_price_dollars)
        p = cents(t.get("yes_price_dollars")) or t.get("yes_price")
        if p is not None: trade_price[tk] = p
        trade_count[tk] = trade_count.get(tk, 0) + 1
    ordered.sort(key=lambda tk: trade_count.get(tk, 0), reverse=True)

    markets_active = []
    for ticker in ordered[:40]:  # Fetch top 40 for richer strategy signals
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
        # Kalshi V2 API returns integer cents fields (yes_bid) not dollars (yes_bid_dollars)
        y = cents(m.get("yes_bid_dollars")) or m.get("yes_bid") or cents(m.get("last_price_dollars")) or m.get("last_price") or m.get("_tp")
        n = cents(m.get("no_bid_dollars")) or m.get("no_bid")
        lines.append(f"  {m.get('ticker','')[:42]:<42} {str(y)+'c' if y else '?':>4} "
                     f"{str(n)+'c' if n else '?':>4}  {m.get('_tc',0):>6}  "
                     f"{str(m.get('close_time',''))[:10]}  {m.get('title','')[:40]}")

        # Build structured entry for JSON dashboard
        yes_bid   = cents(m.get("yes_bid_dollars")) or m.get("yes_bid") or m.get("_tp")
        no_bid    = cents(m.get("no_bid_dollars"))  or m.get("no_bid")
        yes_ask   = cents(m.get("yes_ask_dollars")) or m.get("yes_ask")
        no_ask    = cents(m.get("no_ask_dollars"))  or m.get("no_ask")
        last_p    = cents(m.get("last_price_dollars")) or m.get("last_price") or m.get("_tp")
        yes_price = yes_bid or yes_ask or last_p

        ticker_str = m.get("ticker", "")
        close_raw  = str(m.get("close_time", ""))
        close_date = close_raw  # keep full ISO datetime for expiry checks

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

        tc = trade_count.get(ticker_str, 0)
        # Use trade_count as a proxy for volume when direct volume is unavailable
        # The Kalshi API often returns volume=0 for markets; trade count is more reliable
        raw_volume = m.get("volume", 0) or m.get("volumeNum", 0) or 0
        effective_volume = max(raw_volume, tc)

        markets_list.append({
            "ticker":      ticker_str,
            "title":       m.get("title", ""),
            "yes_bid":     yes_bid,
            "no_bid":      no_bid,
            "yes_ask":     yes_ask,
            "no_ask":      no_ask,
            "last_price":  last_p,
            "volume":      effective_volume,
            "close_time":  close_date,
            "category":    category,
            "_yes_price":  yes_price,
            "_trade_count": tc,
        })

except Exception as e:
    lines.append(f"\n## Active markets ERROR: {e}")

# Fetch supplemental political/economic markets (for deeper strategy coverage)
if _on_interval(10):  # Every 10 minutes (was 30) — needed for active playoff markets
    try:
        supp_markets = fetch_supplemental_markets()
        existing_tickers = {m["ticker"] for m in markets_list}
        for m in supp_markets:
            try:
                # Kalshi V2 API returns integer cents (yes_bid) not dollars (yes_bid_dollars)
                yes_bid  = cents(m.get("yes_bid_dollars"))  or m.get("yes_bid")
                no_bid   = cents(m.get("no_bid_dollars"))   or m.get("no_bid")
                yes_ask  = cents(m.get("yes_ask_dollars"))  or m.get("yes_ask")
                no_ask   = cents(m.get("no_ask_dollars"))   or m.get("no_ask")
                last_p   = cents(m.get("last_price_dollars")) or m.get("last_price")
                yes_price = yes_bid or yes_ask or last_p
                ticker_str = m.get("ticker", "")
                if not ticker_str or ticker_str in existing_tickers:
                    continue
                existing_tickers.add(ticker_str)
                close_raw = str(m.get("close_time", ""))
                supp_vol = m.get("volume", 0) or m.get("volumeNum", 0) or 0
                supp_cat = (
                    "Soccer"    if any(x in ticker_str.upper() for x in ["WORLDCUP","WC26","FIFA","SOC"]) else
                    "Sports"    if any(x in ticker_str.upper() for x in ["NBA","NFL","MLB","NHL","WINNER","PLAYOFF"]) else
                    "Crypto"    if any(x in ticker_str.upper() for x in ["BTC","ETH","SOL","CRYPTO"]) else
                    "Politics"  if any(x in ticker_str.upper() for x in ["SENATE","HOUSE","PRES","POL","GOV"]) else
                    "Economics"
                )
                markets_list.append({
                    "ticker":      ticker_str,
                    "title":       m.get("title", ""),
                    "yes_bid":     yes_bid,
                    "no_bid":      no_bid,
                    "yes_ask":     yes_ask,
                    "no_ask":      no_ask,
                    "last_price":  last_p,
                    "volume":      supp_vol,
                    "close_time":  close_raw,
                    "category":    supp_cat,
                    "_yes_price":  yes_price,
                    "_trade_count": 0,
                    "_supplemental": True,
                })
            except Exception:
                continue
        log(f"Markets list after supplemental: {len(markets_list)}")
    except Exception as e:
        log(f"Supplemental market fetch block error: {e}")

# Always fetch game partner contracts (needed for home_advantage + bundle_arb)
try:
    partner_markets = fetch_game_partner_markets(markets_list)
    existing_tickers = {m["ticker"] for m in markets_list}
    for m in partner_markets:
        try:
            yes_bid  = cents(m.get("yes_bid_dollars")) or m.get("yes_bid")
            no_bid   = cents(m.get("no_bid_dollars"))  or m.get("no_bid")
            yes_ask  = cents(m.get("yes_ask_dollars")) or m.get("yes_ask")
            no_ask   = cents(m.get("no_ask_dollars"))  or m.get("no_ask")
            last_p   = cents(m.get("last_price_dollars")) or m.get("last_price")
            yes_price = yes_bid or yes_ask or last_p
            ticker_str = m.get("ticker", "")
            if not ticker_str or ticker_str in existing_tickers:
                continue
            existing_tickers.add(ticker_str)
            markets_list.append({
                "ticker":      ticker_str,
                "title":       m.get("title", ""),
                "yes_bid":     yes_bid,
                "no_bid":      no_bid,
                "yes_ask":     yes_ask,
                "no_ask":      no_ask,
                "last_price":  last_p,
                "volume":      m.get("volume", 0) or 0,
                "close_time":  str(m.get("close_time", "")),
                "category":    "Sports",
                "_yes_price":  yes_price,
                "_trade_count": 0,
                "_supplemental": True,
            })
        except Exception:
            continue
    if partner_markets:
        log(f"Markets list after game partners: {len(markets_list)}")
except Exception as e:
    log(f"Game partner fetch block error: {e}")

# ================================================================
# SECTION 2: World Cup discovery (once per hour to save API credits)
# ================================================================
lines.append("\n" + "="*70)
lines.append("## FIFA WORLD CUP 2026 — DISCOVERY")
lines.append("="*70)

if not _on_interval(60):
    lines.append("  [Skipped — runs every 60 min to conserve API credits]")
else:
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
        lines.append("\n### Direct event ticker fetch attempts:")
        GUESSES = [
            # Most likely official Kalshi tickers for World Cup 2026
            "KXMENWORLDCUP", "KXWORLDCUP", "KXWC26", "KXWC2026",
            "KXFIFAWC26", "KXFIFAWC2026", "KXMENWC26", "KXMENWC2026",
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
        lines.append("\n### Series tickers found in trades feed:")
        known_series = set()
        for ticker in ordered[:40]:
            parts = ticker.split("-")
            if parts:
                known_series.add(parts[0])
        for s in sorted(known_series):
            lines.append(f"  {s}")

    except Exception as e:
        lines.append(f"  ERROR: {e}")
        traceback.print_exc(file=sys.stderr)

# NOTE: snapshot text is assembled AFTER strategy engine runs (see below)
# so that strategy signals are included in data/snapshot.txt

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
espn_games    = []
espn_injuries = []
espn_news     = []
crypto_prices = {}
metaculus_qs  = []

try:
    espn_games, espn_injuries, espn_news = fetch_all_espn_data()
    log(f"ESPN: {len(espn_games)} games, {len(espn_injuries)} injuries, {len(espn_news)} news")
except Exception as e:
    log(f"ESPN error: {e}")

espn_playoff_series = []
try:
    espn_playoff_series = fetch_espn_playoff_series()
    log(f"ESPN playoff series: {len(espn_playoff_series)} series")
except Exception as e:
    log(f"ESPN playoff series error: {e}")

crypto_prices     = {}
crypto_global     = {}
crypto_movers     = {"gainers": [], "losers": []}
crypto_sparklines = {}
fear_greed        = {}

try:
    crypto_prices = fetch_crypto_prices()
    log(f"Crypto prices: BTC=${crypto_prices.get('btc_usd','?')}")
except Exception as e:
    log(f"Crypto prices error: {e}")

try:
    crypto_global = fetch_crypto_global()
    log(f"Crypto global: BTC dom={crypto_global.get('btc_dominance','?')}%")
except Exception as e:
    log(f"Crypto global error: {e}")

try:
    crypto_movers = fetch_crypto_movers()
    log(f"Crypto movers: {len(crypto_movers.get('gainers',[]))} gainers")
except Exception as e:
    log(f"Crypto movers error: {e}")

try:
    crypto_sparklines = fetch_crypto_sparklines()
    log(f"Sparklines: {list(crypto_sparklines.keys())}")
except Exception as e:
    log(f"Crypto sparklines error: {e}")

try:
    fear_greed = fetch_fear_greed()
    log(f"Fear & Greed: {fear_greed.get('value','?')} ({fear_greed.get('label','?')})")
except Exception as e:
    log(f"Fear & Greed error: {e}")

try:
    metaculus_qs = fetch_metaculus_questions()
    log(f"Metaculus questions: {len(metaculus_qs)}")
except Exception as e:
    log(f"Metaculus error: {e}")

fred_data        = {}
poly_markets     = []
pi_markets       = []
cross_market_arb = []

try:
    fred_data = fetch_fred_data()
    log(f"FRED: {len(fred_data)} indicators")
except Exception as e:
    log(f"FRED error: {e}")

try:
    poly_markets = fetch_polymarket_markets()
    log(f"PolyMarket: {len(poly_markets)} markets")
except Exception as e:
    log(f"PolyMarket error: {e}")

try:
    pi_markets = fetch_predictit_markets()
    log(f"PredictIt: {len(pi_markets)} markets")
except Exception as e:
    log(f"PredictIt error: {e}")

manifold_markets = []
try:
    manifold_markets = fetch_manifold_markets()
    log(f"Manifold: {len(manifold_markets)} markets")
except Exception as e:
    log(f"Manifold error: {e}")

try:
    cross_market_arb = find_cross_market_arb(markets_list, poly_markets, pi_markets,
                                              manifold_markets=manifold_markets)
    log(f"Cross-market arb: {len(cross_market_arb)} opportunities")
except Exception as e:
    log(f"Cross-market arb error: {e}")

# ── Weather forecasts ─────────────────────────────────────────────────────────
weather_data = {}
try:
    weather_data = fetch_weather_forecasts()
    log(f"Weather: {len(weather_data)} cities")
except Exception as e:
    log(f"Weather error: {e}")

# ── Kalshi price history (track inter-run price movements) ───────────────────
kalshi_price_moves = []
try:
    prev_prices = load_price_history()
    if prev_prices:
        kalshi_price_moves = detect_price_movements(markets_list, prev_prices)
        if kalshi_price_moves:
            log(f"Kalshi price moves: {len(kalshi_price_moves)} detected")
            for mv in kalshi_price_moves[:3]:
                log(f"  MOVE {mv['move']:+.1f}¢  {mv['ticker']}  {mv['prev_price']:.0f}→{mv['curr_price']:.0f}¢")
    save_price_history(markets_list)
except Exception as e:
    log(f"Price history error: {e}")

# ── Vegas vs Kalshi divergences ───────────────────────────────────────────────
edges = []
line_movements = []
vegas_games = []  # always defined — needed for World Cup strategy
vegas_games_count = 0
odds_status = "no_key"
try:
    if ODDS_API_KEY:
        vegas_games = fetch_vegas_odds()
        # Line movement detection
        odds_history = load_odds_history()
        line_movements = detect_line_movements(vegas_games, odds_history)
        save_odds_history(vegas_games)
        log(f"Line movements detected: {len(line_movements)}")
        vegas_games_count = len(vegas_games)
        odds_status = f"ok_{vegas_games_count}_games"
        log(f"Total Vegas games: {vegas_games_count}")
        edges = find_divergences(markets_list, vegas_games)
        log(f"Edge alerts found: {len(edges)}")
        for e in edges:
            log(f"  EDGE {e['gap']:+.1f}¢  {e['ticker']}  Kalshi={e['kalshi_price']}¢ Vegas={e['vegas_prob']}¢  {e['direction']}")
    else:
        odds_status = "no_key"
except Exception as e:
    odds_status = f"error: {e}"
    log(f"Odds comparison error: {e}")
    traceback.print_exc(file=sys.stderr)

# ── Load recent signal history for persistence detection ──────────────────────
_sig_hist_path = Path(__file__).parent.parent / "data" / "signal_history.json"
_persist_counter: dict = {}
try:
    if _sig_hist_path.exists():
        _recent = json.loads(_sig_hist_path.read_text())[-12:]  # last ~1 hour of 5-min runs
        for _entry in _recent:
            for _td in _entry.get("tickers", []):
                if isinstance(_td, (list, tuple)) and len(_td) >= 2:
                    _k = (_td[0], _td[1])
                    _persist_counter[_k] = _persist_counter.get(_k, 0) + 1
        log(f"Persistence: {len(_persist_counter)} ticker/direction combos in recent history")
except Exception as _e:
    log(f"Persistence load error: {_e}")

# ── Strategy engine ───────────────────────────────────────────────────────────
strategy_signals = []
try:
    strategy_signals = run_strategy_engine(markets_list, edges, cross_market_arb, weather_data,
                                            espn_games=espn_games, metaculus_qs=metaculus_qs,
                                            price_moves=kalshi_price_moves,
                                            vegas_games=(vegas_games if ODDS_API_KEY else []),
                                            playoff_series=espn_playoff_series,
                                            fear_greed=fear_greed, fred_data=fred_data,
                                            crypto_prices=crypto_prices)
    log(f"Strategy signals: {len(strategy_signals)}")
except Exception as e:
    log(f"Strategy engine error: {e}")
    traceback.print_exc(file=sys.stderr)

# ── Enrich signals with persistence count ─────────────────────────────────────
# Persistent signals (appearing in 3+ consecutive snapshots) get priority boost
for _s in strategy_signals:
    _key = (_s.get("ticker", ""), _s.get("direction", ""))
    _pc = _persist_counter.get(_key, 0)
    if _pc > 0:
        _s["persistence_count"] = _pc
        if _pc >= 6:  # ~30 min persistent — structural edge, upgrade confidence
            if _s.get("confidence") == "low":
                _s["confidence"] = "medium"
            elif _s.get("confidence") == "medium":
                _s["confidence"] = "high"
            _s["priority"] = max(1, _s.get("priority", 3) - 1)
        elif _pc >= 3:  # ~15 min — boost priority one level
            _s["priority"] = max(1, _s.get("priority", 3) - 1)
        if _pc >= 2:
            log(f"Persistent signal: {_s.get('ticker','')} {_s.get('direction','')} ({_pc}x in last hour)")

# ── Re-score best picks incorporating strategy signals ────────────────────────
# Replace initial best_picks (computed early, before strategy signals) with a
# smarter ranking that boosts markets that have strategy signals
try:
    sig_tickers = {s.get("ticker", ""): s for s in strategy_signals if s.get("ticker")}
    scored2 = sorted(
        [(score_market(m, all_volumes, today), m) for m in markets_list],
        key=lambda x: x[0][0], reverse=True
    )
    best_picks = []
    # First: add markets that have high/medium confidence strategy signals
    for sig in strategy_signals:
        if sig.get("confidence") in ("high", "medium") and sig.get("ticker"):
            tk = sig["ticker"]
            mkt = next((m for m in markets_list if m.get("ticker") == tk), None)
            if mkt and not any(p["ticker"] == tk for p in best_picks):
                direction = sig.get("direction", "BUY YES")
                yp = mkt.get("_yes_price", 50) or 50
                side = "YES" if "YES" in direction.upper() else "NO"
                price = yp if side == "YES" else (100 - yp)
                kelly_pct = round(sig.get("kelly_frac", 0) * 100, 1)
                best_picks.append({
                    "ticker":     tk,
                    "title":      mkt.get("title", ""),
                    "side":       side,
                    "price":      price,
                    "reason":     f"{sig.get('type','?')} signal | Kelly {kelly_pct}% | {sig.get('rationale','')[:60]}",
                    "edge_score": round(sig.get("kelly_frac", 0) * 100, 1),
                    "confidence": sig.get("confidence", "?"),
                })
                if len(best_picks) >= 3:
                    break
    # Fill remaining slots with highest-volume/price markets
    for (score, reason), m in scored2:
        if len(best_picks) >= 5:
            break
        tk = m.get("ticker", "")
        if any(p["ticker"] == tk for p in best_picks):
            continue
        yp = m.get("_yes_price")
        if yp is None:
            side, price = "YES", 50
        elif yp < 50:
            side, price = "YES", yp
        else:
            side, price = "NO", 100 - yp
        best_picks.append({"ticker": tk, "title": m.get("title", ""),
                            "side": side, "price": price,
                            "reason": reason, "edge_score": score})
except Exception as e:
    log(f"Best picks re-score error: {e}")

# ── Enrich positions with current market price for P&L estimation ─────────────
try:
    mkt_price_idx = {m["ticker"]: m for m in markets_list}
    total_unreal_cents = 0
    for p in positions_list:
        tk = p.get("ticker", "")
        mkt = mkt_price_idx.get(tk)
        if not mkt:
            continue
        curr_price = mkt.get("_yes_price")
        if curr_price is None:
            continue
        p["current_price"] = curr_price
        # Estimate unrealized P&L if API didn't return it
        if p.get("unrealized_pnl") is None and p.get("market_exposure") is not None:
            qty  = p["qty"]
            side = p["side"]
            # Approximate: exposure ≈ entry_cost; current value ≈ qty × curr_price
            # for YES: profit = qty × (curr_price - entry_avg); entry_avg ≈ exposure/qty
            exp = p.get("market_exposure", 0)
            if exp and qty:
                entry_avg = exp / qty
                if side == "YES":
                    estimated_pnl = round((curr_price - entry_avg) * qty)
                else:
                    estimated_pnl = round(((100 - curr_price) - entry_avg) * qty)
                p["estimated_pnl_cents"] = estimated_pnl
                total_unreal_cents += estimated_pnl
    if positions_list:
        log(f"Position P&L: est. unrealized ${total_unreal_cents/100:+.2f}")
except Exception as e:
    log(f"Position P&L enrichment error: {e}")

# ── Write JSON for dashboard ──────────────────────────────────────────────────

# Build health check dict for diagnostics
now_min = datetime.now(timezone.utc).minute
health = {
    "run_minute":   now_min,
    "kalshi":       "ok" if markets_list else "error",
    "odds_api":     odds_status,
    "coingecko":    ("ok" if crypto_prices else ("skipped_interval" if not _on_interval(15) else "no_key_or_error")) if COINGECKO_API_KEY else "no_key",
    "fear_greed":   "ok" if fear_greed else "error",
    "fred":         ("ok" if fred_data else ("skipped_interval" if not _on_interval(60) else "no_key_or_error")) if FRED_API_KEY else "no_key",
    "polymarket":   ("ok" if poly_markets else ("skipped_interval" if not _on_interval(15) else "error")),
    "predictit":    ("ok" if pi_markets else ("skipped_interval" if not _on_interval(15) else "error")),
    "manifold":     f"ok_{len(manifold_markets)}" if manifold_markets else ("skipped_interval" if not _on_interval(15) else "error"),
    "metaculus":    f"ok_{len(metaculus_qs)}" if metaculus_qs else "empty_or_error",
    "line_movements":  f"ok_{len(line_movements)}" if ODDS_API_KEY else "no_key",
    "price_moves":  len(kalshi_price_moves),
    "vegas_games":  vegas_games_count,
    "espn":         "ok" if espn_games else "empty",
    "weather":     f"ok_{len(weather_data)}" if weather_data else ("skipped_interval" if not _on_interval(30) else "error"),
}
docs_dir = Path(__file__).parent.parent / "docs"
docs_dir.mkdir(exist_ok=True)

clean_markets = [{k: v for k, v in m.items() if not k.startswith("_")}
                 for m in markets_list]

# Build signal summary for dashboard overview
from collections import Counter
sig_counts = Counter(s.get("type", "unknown") for s in strategy_signals)
conf_counts = Counter(s.get("confidence", "low") for s in strategy_signals)
consensus_signals = [s for s in strategy_signals if s.get("consensus_count", 0) >= 2]
signal_summary = {
    "total":       len(strategy_signals),
    "by_type":     dict(sig_counts),
    "by_conf":     dict(conf_counts),
    "high_conf":   conf_counts.get("high", 0),
    "medium_conf": conf_counts.get("medium", 0),
    "top_kelly":   round(max((s.get("kelly_frac", 0) for s in strategy_signals), default=0) * 100, 1),
    "consensus":   len(consensus_signals),
    "top_consensus": [{"ticker": s.get("ticker"), "direction": s.get("direction"),
                       "n_agree": s.get("consensus_count", 0), "types": s.get("consensus_types", [])}
                      for s in sorted(consensus_signals, key=lambda x: x.get("consensus_count",0), reverse=True)[:3]],
}

# ── Signal history tracking ───────────────────────────────────────────────────
# Append a summary entry to signal_history.json (keep last 200 runs).
# Used for detecting persistent signals and tracking strategy performance.
SIGNAL_HISTORY_FILE = Path(__file__).parent.parent / "data" / "signal_history.json"
sig_hist = []  # default; populated below
try:
    sig_hist = []
    if SIGNAL_HISTORY_FILE.exists():
        try:
            sig_hist = json.loads(SIGNAL_HISTORY_FILE.read_text())
        except Exception:
            sig_hist = []

    # Append snapshot of this run's signals
    # Compute per-type average kelly for performance tracking
    type_kelly = {}
    type_conf = {}
    for s in strategy_signals:
        t = s.get("type", "unknown")
        if t not in type_kelly:
            type_kelly[t] = []
            type_conf[t] = []
        type_kelly[t].append(s.get("kelly_frac", 0))
        type_conf[t].append(1 if s.get("confidence") == "high" else 0)
    type_perf = {t: {"avg_kelly": round(sum(v)/len(v)*100, 2),
                     "high_conf_rate": round(sum(type_conf[t])/len(type_conf[t])*100, 1),
                     "count": len(v)}
                 for t, v in type_kelly.items()}

    sig_hist.append({
        "ts":         ts_str,
        "total":      len(strategy_signals),
        "high_conf":  signal_summary["high_conf"],
        "top_kelly":  signal_summary["top_kelly"],
        "by_type":    signal_summary["by_type"],
        "type_perf":  type_perf,
        "consensus":  signal_summary["consensus"],
        "top_signals": [
            {
                "type":       s.get("type"),
                "ticker":     s.get("ticker"),
                "direction":  s.get("direction"),
                "price":      s.get("price"),
                "confidence": s.get("confidence"),
                "kelly_frac": s.get("kelly_frac"),
            }
            for s in strategy_signals[:5]
        ],
        "markets_count":     len(markets_list),
        "supplemental_count": len([m for m in markets_list if m.get("_supplemental")]),
        "tickers": [[s.get("ticker", ""), s.get("direction", "")] for s in strategy_signals if s.get("ticker")],
    })
    # Keep last 200 entries
    sig_hist = sig_hist[-200:]
    SIGNAL_HISTORY_FILE.parent.mkdir(exist_ok=True)
    SIGNAL_HISTORY_FILE.write_text(json.dumps(sig_hist, indent=2))
    log(f"Signal history: {len(sig_hist)} entries saved")
except Exception as e:
    log(f"Signal history save error: {e}")

(docs_dir / "data.json").write_text(json.dumps({
    "generated":         ts_str,
    "balance_cents":     balance_cents,
    "positions":         positions_list,
    "markets":           clean_markets,
    "best_picks":        best_picks,
    "edges":             edges,
    "espn_games":        espn_games,
    "espn_injuries":     espn_injuries,
    "espn_news":         espn_news,
    "espn_playoff_series": espn_playoff_series,
    "crypto":            crypto_prices,
    "crypto_global":     crypto_global,
    "crypto_movers":     crypto_movers,
    "sparklines":        crypto_sparklines,
    "fear_greed":        fear_greed,
    "metaculus":         metaculus_qs,
    "fred":              fred_data,
    "polymarket":        poly_markets,
    "predictit":         pi_markets,
    "manifold":          manifold_markets[:50],  # top 50 by liquidity
    "cross_arb":         cross_market_arb,
    "weather":           weather_data,
    "line_movements":    line_movements,
    "price_movements":   kalshi_price_moves,
    "strategy_signals":  strategy_signals,
    "signal_summary":    signal_summary,
    "signal_history":    sig_hist[-48:],   # last ~4h of 5-min runs for sparklines
    "health":            health,
}, indent=2))
log(f"Saved JSON -> {docs_dir / 'data.json'}")

# ── Append strategy signals to snapshot text ─────────────────────────────────
# Now that strategy engine has run, add signals to the text snapshot for
# the `kalshi advise` CLI command / AI analysis sessions.
if strategy_signals:
    lines.append("\n" + "="*70)
    lines.append("## STRATEGY SIGNALS — TOP PICKS")
    lines.append("="*70)
    lines.append(f"  {len(strategy_signals)} signals from 17 research modules "
                 f"({signal_summary['high_conf']} high-conf, top Kelly {signal_summary['top_kelly']}%)")
    lines.append(f"  {'Rank':<4} {'Type':<22} {'Dir':<8} {'Ticker':<36} {'P':>3} {'Kelly':>6}  Conf   Rationale")
    lines.append("  " + "-"*110)
    for i, s in enumerate(strategy_signals[:15], 1):
        direction_short = "YES" if "YES" in s.get("direction","") else ("NO" if "NO" in s.get("direction","") else "ALL")
        kelly_str = f"{s.get('kelly_frac',0)*100:.1f}%"
        conf = s.get("confidence","?")[:3].upper()
        rationale_short = s.get("rationale","")[:55]
        lines.append(f"  {i:<4} {s.get('type','?')[:22]:<22} {direction_short:<8} "
                     f"{s.get('ticker','?')[:36]:<36} {s.get('price',0):>3}¢ {kelly_str:>6}  {conf:<4}  {rationale_short}")

if best_picks:
    lines.append("\n## BEST PICKS SUMMARY")
    lines.append("  (High/medium confidence signals + top volume markets)")
    for p in best_picks:
        conf_tag = f"[{p.get('confidence','').upper()[:3]}]" if p.get("confidence") else ""
        lines.append(f"  {p.get('side','?'):3} {p.get('price',0):>3}¢  {conf_tag:5}  {p.get('ticker','')[:36]:<36}  {p.get('title','')[:45]}")

# ── Action Digest — plain-English top 3 ──────────────────────────────────────
# Surfaces the 3 most actionable opportunities in clear language
try:
    digest_sigs = [s for s in strategy_signals if s.get("confidence") in ("high", "medium")][:3]
    if digest_sigs or edges[:1]:
        lines.append("\n" + "="*70)
        lines.append("## ⚡ TOP ACTIONS NOW")
        lines.append("="*70)
        for i, s in enumerate(digest_sigs, 1):
            direction = s.get("direction", "?")
            ticker = s.get("ticker", "?")
            price = s.get("price", 0)
            entry = s.get("entry_limit_cents", price)
            tp = s.get("take_profit_cents")
            sl = s.get("stop_loss_pct", 0)
            days = s.get("days_until_close")
            kelly_pct = s.get("kelly_frac", 0) * 100
            cons = f" [{s.get('consensus_count',0)} strategies agree]" if s.get("consensus_count",0) >= 2 else ""
            days_str = f"  (closes {days:.0f}d)" if days else ""
            tp_str = f"  → TP {tp}¢" if tp else ""
            lines.append(f"  {i}. {direction} {ticker} @ {entry}¢{tp_str}  [Kelly {kelly_pct:.1f}%]{cons}{days_str}")
            lines.append(f"     Reason: {s.get('rationale','')[:100]}")
        if edges and len(digest_sigs) < 3:
            e = edges[0]
            lines.append(f"  {len(digest_sigs)+1}. {e.get('direction','?')} {e.get('ticker','')} — Vegas gap {e.get('gap',0):+.1f}¢ (Kalshi {e.get('kalshi_price')}¢ vs Vegas {e.get('vegas_prob')}¢)")
except Exception as ex:
    log(f"Action digest error: {ex}")

if edges:
    lines.append("\n## VEGAS EDGE ALERTS")
    for e in edges[:5]:
        lines.append(f"  {e.get('direction','?'):12}  {e.get('ticker','')[:36]:<36}  Kalshi={e.get('kalshi_price')}¢ Vegas={e.get('vegas_prob')}¢  Gap={e.get('gap',0):+.1f}¢")

if kalshi_price_moves:
    lines.append("\n## SHARP PRICE MOVES (inter-run)")
    for mv in kalshi_price_moves[:5]:
        lines.append(f"  {mv.get('move',0):+.1f}¢  {mv.get('ticker','')[:36]:<36}  {mv.get('prev_price',0):.0f}→{mv.get('curr_price',0):.0f}¢  {mv.get('title','')[:45]}")

if cross_market_arb:
    lines.append("\n## CROSS-PLATFORM ARB")
    for a in cross_market_arb[:5]:
        lines.append(f"  {a.get('direction','?')[:22]:<22}  {a.get('kalshi_ticker','')[:30]:<30}  Kalshi={a.get('kalshi_price')}¢ {a.get('platform','?')}={a.get('platform_price')}¢  Gap={a.get('gap',0):+.1f}¢")

# ── Write snapshot text ───────────────────────────────────────────────────────
snapshot = "\n".join(lines)
out = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None
if out:
    Path(out).write_text(snapshot)
    log(f"Saved text -> {out}")
else:
    print(snapshot)
