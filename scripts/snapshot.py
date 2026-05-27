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
    """Fetch today's games across all sports. Returns list of game dicts with event IDs."""
    all_games = []
    for sport, league in ESPN_SPORTS:
        data = _espn_get(f"{ESPN_SITE}/{sport}/{league}/scoreboard")
        if not data:
            continue
        for event in data.get("events", []):
            comp  = event.get("competitions", [{}])[0]
            teams = comp.get("competitors", [])
            game  = {
                "event_id":   event.get("id", ""),
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
        log(f"ESPN {sport}/{league} scoreboard -> {len(data.get('events',[]))} games")
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
    """Fetch top active Metaculus questions. Tries multiple API versions."""
    try:
        headers = {"Accept": "application/json"}
        # Try multiple endpoints in order
        endpoints = [
            ("https://www.metaculus.com/api/posts/?statuses=open&order_by=-activity&limit=20&post_type=question", "results"),
            ("https://www.metaculus.com/api2/questions/?status=open&order_by=-activity&limit=20", "results"),
        ]
        raw = []
        for url, key in endpoints:
            try:
                r = httpx.get(url, headers=headers, timeout=10)
                log(f"Metaculus {url[:60]} -> {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    raw = data.get(key, data if isinstance(data, list) else [])
                    if raw:
                        log(f"Metaculus: got {len(raw)} results from {url[:40]}")
                        break
                    log(f"Metaculus: empty results from {url[:40]}")
            except Exception as e:
                log(f"Metaculus endpoint error: {e}")
                continue

        if not raw:
            return []

        questions = []
        for q in raw:
            # v3 nests under "question" key; v2 has fields at top level
            inner = q.get("question", q)
            title = inner.get("title") or q.get("title", "")
            qid   = inner.get("id") or q.get("id")

            # Probability — try multiple field names
            prob = None
            for field in ["community_prediction", "cp", "probability"]:
                val = inner.get(field) or q.get(field)
                if val is None:
                    continue
                if isinstance(val, (int, float)):
                    prob = float(val)
                    break
                if isinstance(val, dict):
                    prob = val.get("full", {}).get("q2") or val.get("q2") or val.get("median")
                    if prob is not None:
                        break

            close_time = (inner.get("scheduled_close_time") or
                         inner.get("close_time") or
                         q.get("close_time") or
                         q.get("scheduled_close_time") or "")

            if not title or not qid:
                continue
            questions.append({
                "id":         qid,
                "title":      title,
                "prob":       round(float(prob) * 100, 1) if prob is not None else None,
                "close_time": str(close_time)[:10],
                "url":        f"https://www.metaculus.com/questions/{qid}/",
            })

        log(f"Metaculus: {len(questions)} questions parsed")
        return questions
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
    "unemployment": ("UNRATE",   "Unemployment Rate",    "%"),
    "fed_rate":     ("FEDFUNDS", "Fed Funds Rate",       "%"),
    "treasury_10y": ("DGS10",    "10-Year Treasury",     "%"),
    "treasury_2y":  ("DGS2",     "2-Year Treasury",      "%"),
    "payrolls":     ("PAYEMS",   "Nonfarm Payrolls",     "K"),
    "gdp":          ("GDPC1",    "Real GDP",             "B"),
    "sentiment":    ("UMCSENT",  "Consumer Sentiment",   ""),
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
            try:
                val  = float(val_str)
                prev_val = float(prev_str) if prev_str != "." else None
                change   = round(val - prev_val, 3) if prev_val is not None else None
            except (ValueError, TypeError):
                val, change = None, None
            result[key] = {
                "label":  label,
                "value":  val,
                "unit":   unit,
                "date":   latest.get("date", ""),
                "prev":   prev_val if "prev_val" in dir() else None,
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
            params={"active": "true", "closed": "false", "limit": 100},
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
        # Skip illiquid markets — bias is less reliable with low volume
        if vol < 5:
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

    return signals

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
    # GAME, WINNER, 1H, FUTURES markets with exactly 2 outcomes are exhaustive
    if len(suffixes) == 2:
        exhaustive_types = ["GAME", "WINNER", "1H", "FUTURES", "CHAMP", "ELEC", "POL",
                            "PRES", "SEN", "GOV", "MVE", "MVP"]
        if any(t in s for t in exhaustive_types):
            return True
        # 2-contract non-spread series: assume exhaustive if suffixes look like team names
        if all(len(x) <= 5 and x.isalpha() for x in suffixes):
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
        gap = e.get("gap", 0)
        kp  = e.get("kalshi_price", 50)
        vp  = e.get("vegas_prob", 50)
        if abs(gap) < 5: continue

        # Use Vegas as the "true" probability estimate
        kelly = kelly_size(vp, kp, maker=True, fraction=0.25)

        signals.append({
            "type":              "vegas_divergence",
            "direction":         e.get("direction", ""),
            "ticker":            e.get("ticker", ""),
            "title":             e.get("title", "")[:60],
            "price":             kp,
            "rationale":         f"Vegas implies {vp:.1f}¢, Kalshi at {kp}¢. Gap: {gap:+.1f}¢. Use LIMIT order.",
            "confidence":        "high" if abs(gap) >= 10 else "medium",
            "kelly_frac":        kelly,
            "fee_cents":         kalshi_fee(kp, maker=True),
            "priority":          1 if abs(gap) >= 10 else 2,
            "game":              e.get("game", ""),
            "books":             e.get("books", []),
            "entry_limit_cents": max(1, kp - 3),
            "take_profit_cents": min(99, int(vp)),
            "stop_loss_pct":     0.4,
        })
    return signals

def analyze_cross_platform_arb(cross_arb):
    """
    Convert cross-market arb into strategy signals with fee-adjusted profitability.
    Minimum viable spread: ~3¢ after fees for Kalshi-PolyMarket,
                           ~17¢ for anything involving PredictIt.
    """
    signals = []
    for a in cross_arb:
        gap = abs(a.get("gap", 0))
        source = a.get("source", "")
        kalshi_p = a.get("kalshi_price", 50)
        other_p  = a.get("other_price", 50)

        # Fee thresholds
        kalshi_fee_val = kalshi_fee(kalshi_p, maker=True)
        if source == "predictit":
            min_viable = 17.0  # PredictIt 15% effective fee kills most arb
            other_fee  = 15.0
        else:  # polymarket
            min_viable = 3.0
            other_fee  = 0.02

        net_profit = gap - kalshi_fee_val - other_fee
        if net_profit < 1.0: continue  # not profitable after fees

        signals.append({
            "type":              "cross_platform_arb",
            "direction":         a.get("direction", ""),
            "ticker":            a.get("kalshi_ticker", ""),
            "title":             a.get("kalshi_title", "")[:60],
            "price":             kalshi_p,
            "rationale":         f"{source.title()}: {other_p:.1f}¢ vs Kalshi {kalshi_p:.1f}¢. Net after fees: +{net_profit:.1f}¢. ⚠️ Verify settlement rules match.",
            "confidence":        "medium",
            "kelly_frac":        0.03,  # small fixed size — settlement risk
            "fee_cents":         kalshi_fee_val,
            "net_profit":        round(net_profit, 2),
            "priority":          1 if net_profit >= 5 else 2,
            "warning":           "Verify settlement language matches before entering both legs.",
            "entry_limit_cents": max(1, kalshi_p - 2),
            "take_profit_cents": min(99, int(other_p)),
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
            direction = "BUY YES" if diff >= 0 else "BUY NO"
            city_name = weather_data[matched_city].get("name", matched_city)
            kalshi_prob = yes_p
            model_prob  = int(round(kalshi_prob + diff * 3))  # rough model-implied probability
            signals.append({
                "type":              "weather_edge",
                "direction":         direction,
                "ticker":            ticker,
                "title":             title[:60],
                "price":             yes_p,
                "rationale":         f"Model forecasts {model_high}°F high for {city_name}. Market bucket: {low_temp:.0f}-{high_temp:.0f}°F. Diff: {diff:+.1f}°. Use LIMIT order.",
                "confidence":        "high" if abs(diff) >= 5 else "medium",
                "kelly_frac":        kelly_size(85 if abs(diff) >= 5 else 70, yes_p, maker=True, fraction=0.25),
                "fee_cents":         kalshi_fee(yes_p, maker=True),
                "priority":          1 if abs(diff) >= 5 else 2,
                "model_high":        model_high,
                "entry_limit_cents": max(1, int(kalshi_prob - 5)),
                "take_profit_cents": max(1, min(99, model_prob)),
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
    # Sort by volume to find top traders
    sorted_markets = sorted(
        [m for m in markets if m.get("volume", 0) > 50],
        key=lambda m: m.get("volume", 0),
        reverse=True
    )
    # Take top 10% by volume (or top 10, whichever smaller)
    top_n = max(3, len(sorted_markets) // 10)
    hot_markets = sorted_markets[:top_n]

    if not hot_markets:
        return signals

    # Compute median volume for reference
    vols = [m.get("volume", 0) for m in sorted_markets]
    median_vol = sorted(vols)[len(vols) // 2] if vols else 100

    for m in hot_markets:
        ticker = m.get("ticker", "")
        title  = m.get("title", ticker)
        vol    = m.get("volume", 0)
        price  = m.get("_yes_price", m.get("yes_ask", 50))

        if not ticker or not price:
            continue

        vol_ratio = vol / median_vol if median_vol else 1
        if vol_ratio < 3.0:  # Need 3x median volume to qualify
            continue

        # Strategy: high volume on undecided markets = follow the money
        if 35 <= price <= 65:
            # Neutral market with high volume = someone knows something
            # Direction: buy YES if price is above 50, NO if below
            direction = "BUY YES" if price >= 50 else "BUY NO"
            side_price = price if "YES" in direction else (100 - price)
            kelly = kelly_size(price, side_price, maker=True)
            signals.append({
                "type":              "volume_spike",
                "direction":         direction,
                "ticker":            ticker,
                "title":             title,
                "price":             price,
                "rationale":         f"Volume spike {vol_ratio:.1f}x median ({vol:,} trades). Neutral price {price}¢ — informed traders positioning.",
                "confidence":        "medium",
                "kelly_frac":        kelly * 0.5,  # half-Kelly for momentum
                "fee_cents":         kalshi_fee(price),
                "priority":          2,
                "volume":            vol,
                "vol_ratio":         round(vol_ratio, 1),
                "entry_limit_cents": max(1, price - 2),
                "take_profit_cents": min(99, price + 5),
                "stop_loss_pct":     0.35,
            })
        elif price > 85:
            # Heavy volume on near-certainty = high confidence follow
            kelly = kelly_size(price, price, maker=True)
            signals.append({
                "type":              "volume_spike",
                "direction":         "BUY YES",
                "ticker":            ticker,
                "title":             title,
                "price":             price,
                "rationale":         f"Volume spike {vol_ratio:.1f}x ({vol:,} trades) on {price}¢ favourite. Heavy confirmation.",
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
            # High volume longshot — but longshot bias says fades, so BUY NO
            kelly = kelly_size(100 - price, 100 - price, maker=True)
            signals.append({
                "type":              "volume_spike",
                "direction":         "BUY NO",
                "ticker":            ticker,
                "title":             title,
                "price":             price,
                "rationale":         f"Volume spike {vol_ratio:.1f}x ({vol:,} trades) on {price}¢ longshot. Bias + confirmation = fade.",
                "confidence":        "medium",
                "kelly_frac":        kelly * 0.3,
                "fee_cents":         kalshi_fee(100 - price),
                "priority":          2,
                "volume":            vol,
                "vol_ratio":         round(vol_ratio, 1),
                "entry_limit_cents": max(1, price - 2),
                "take_profit_cents": min(99, price + 5),
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
                direction = "BUY NO"
                price = kalshi_price
                prob  = 100 - espn_prob
            else:  # Kalshi underprices → buy YES
                direction = "BUY YES"
                price = kalshi_price
                prob  = espn_prob

            kelly = kelly_size(prob, price, maker=True)
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
                "fee_cents":         kalshi_fee(price),
                "priority":          1 if abs(gap) >= 10 else 2,
                "espn_prob":         round(espn_prob, 1),
                "gap":               round(gap, 1),
                "entry_limit_cents": max(1, kalshi_price - 3),
                "take_profit_cents": min(99, int(espn_prob)),
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

    if 20 < fg < 80:
        return signals  # neutral — no edge

    is_fear = fg <= 20
    fg_label = fear_greed_data.get("label", "")

    crypto_kws = ["BTC", "ETH", "SOL", "CRYPTO", "KXBTC", "KXETH", "KXSOL"]
    direction_kws = ["UP", "ABOVE", "HIGH", "PRICE"]

    for m in markets:
        ticker = m.get("ticker", "").upper()
        title  = m.get("title", "").upper()

        # Only target crypto direction markets
        if not any(kw in ticker for kw in crypto_kws):
            continue
        is_direction = any(kw in ticker or kw in title for kw in direction_kws)
        if not is_direction:
            continue

        price = m.get("_yes_price")
        if price is None or not (5 <= price <= 95):
            continue

        if is_fear:
            # Extreme fear → price action likely to recover → buy YES on up markets
            direction = "BUY YES"
            prob = min(75, price + 15)  # sentiment edge: ~15% adjustment
            kelly = kelly_size(prob, price, maker=True)
            rationale = f"Fear & Greed = {fg} ({fg_label}). Extreme fear → sentiment reversal edge. Crypto markets priced too bearish."
        else:
            # Extreme greed → likely to cool → fade the move
            direction = "BUY NO"
            prob = min(75, (100 - price) + 15)
            kelly = kelly_size(prob, 100 - price, maker=True)
            rationale = f"Fear & Greed = {fg} ({fg_label}). Extreme greed → distribution phase. Crypto markets priced too bullish."

        if kelly <= 0:
            continue

        signals.append({
            "type":              "fear_greed_edge",
            "direction":         direction,
            "ticker":            m.get("ticker", ""),
            "title":             m.get("title", ""),
            "price":             price,
            "rationale":         rationale,
            "confidence":        "high" if (fg <= 10 or fg >= 90) else "medium",
            "kelly_frac":        kelly * 0.4,
            "fee_cents":         kalshi_fee(price),
            "priority":          2,
            "fear_greed":        fg,
            "entry_limit_cents": max(1, price - 2),
            "take_profit_cents": min(99, price + 5),
            "stop_loss_pct":     0.4,
        })

    signals.sort(key=lambda x: x.get("kelly_frac", 0), reverse=True)
    log(f"Fear/Greed edge: {len(signals)} signals (F&G={fg})")
    return signals[:3]


def analyze_fred_edge(markets, fred_data):
    """
    Use FRED economic data to find mispriced economic/political Kalshi markets.

    Key relationships:
    - Fed Funds Rate (FEDFUNDS): affects "Fed rate hike/cut" markets
    - CPI/CPILFESL: affects "inflation" markets
    - UNRATE: affects "unemployment" markets
    - 10Y Treasury (GS10): affects "yield" markets

    Strategy: when FRED data strongly suggests a direction and Kalshi price
    disagrees, that's an edge.
    """
    signals = []
    if not fred_data or not markets:
        return signals

    # Get current values
    fed_rate = None
    cpi      = None
    unrate   = None

    for series_id, info in fred_data.items():
        val = info.get("value") if isinstance(info, dict) else None
        if val is None:
            try:
                val = float(info)
            except (TypeError, ValueError):
                pass
        if val is None:
            continue
        sid = series_id.upper()
        if "FEDFUNDS" in sid or "FEDRATE" in sid or sid == "FED_RATE":
            try: fed_rate = float(val)
            except: pass
        elif "CPILFE" in sid or ("CPI" in sid and "CORE" in sid):
            try: cpi = float(val)
            except: pass
        elif "UNRATE" in sid or sid == "UNEMPLOYMENT":
            try: unrate = float(val)
            except: pass

    # Cross-reference with Kalshi markets
    for m in markets:
        ticker = m.get("ticker", "").upper()
        title  = (m.get("title") or "").upper()
        yp     = m.get("_yes_price")
        if yp is None:
            continue

        # Fed rate markets
        if ("FED" in ticker or "RATE" in ticker) and fed_rate is not None:
            if "HIKE" in title or "RAISE" in title or "INCREASE" in title:
                # Fed unlikely to hike if rate already high (>5.5%)
                if fed_rate > 5.5 and yp > 40:
                    signals.append({
                        "type":              "fred_edge",
                        "direction":         "BUY NO",
                        "ticker":            m.get("ticker", ""),
                        "title":             m.get("title", ""),
                        "price":             yp,
                        "rationale":         f"FRED: Fed Funds Rate = {fed_rate:.2f}%. Rate hike unlikely at this level. Kalshi prices {yp}¢.",
                        "confidence":        "medium",
                        "kelly_frac":        0.01,
                        "fee_cents":         kalshi_fee(100 - yp),
                        "priority":          2,
                        "entry_limit_cents": max(1, yp - 2),
                        "take_profit_cents": min(99, yp + 5),
                        "stop_loss_pct":     0.4,
                    })

        # Unemployment markets
        if "UNEMP" in ticker or "UNRATE" in ticker or "JOBLESS" in ticker:
            if unrate is not None:
                # High unrate > 5% usually means "will unemployment stay above X?" YES
                if unrate > 5.0 and "ABOVE" in title and yp < 40:
                    signals.append({
                        "type":              "fred_edge",
                        "direction":         "BUY YES",
                        "ticker":            m.get("ticker", ""),
                        "title":             m.get("title", ""),
                        "price":             yp,
                        "rationale":         f"FRED: Unemployment = {unrate:.1f}%. Historical persistence suggests above-threshold likely.",
                        "confidence":        "low",
                        "kelly_frac":        0.01,
                        "fee_cents":         kalshi_fee(yp),
                        "priority":          3,
                        "entry_limit_cents": max(1, yp - 2),
                        "take_profit_cents": min(99, yp + 5),
                        "stop_loss_pct":     0.4,
                    })

    log(f"FRED edge: {len(signals)} signals")
    return signals[:3]


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
        yb     = m.get("yes_bid") or 0
        ya     = m.get("yes_ask") or 99
        spread = ya - yb

        if yp is None or not ticker:
            continue

        t_up = ticker.upper()
        # Only look at GAME and WINNER markets (binary outcomes)
        if not any(x in t_up for x in ["GAME", "WINNER", "1H"]):
            continue

        # Require decent volume (market is liquid)
        if vol < 15:
            continue

        # Tight spread required (< 4¢)
        if spread > 4:
            continue

        # Mid-range underdog: 20-35¢ YES in a binary game market
        # Research: these markets slightly underprice the trailing team
        if 20 <= yp <= 35 and vol > 20:
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
        if 72 <= yp <= 82 and vol > 15:
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

        # Require at least 4 keyword matches for a confident match
        if best_overlap < 4 or best_market is None:
            continue

        kalshi_price = best_market.get("_yes_price", 50)
        gap = mprob - kalshi_price  # positive = Metaculus more bullish than Kalshi

        if abs(gap) < 10:
            continue  # not a big enough gap to trade

        # Direction
        if gap > 10:   # Metaculus says YES is underpriced on Kalshi
            direction = "BUY YES"
            prob = mprob
            price = kalshi_price
        else:          # Metaculus says NO is better value
            direction = "BUY NO"
            prob = 100 - mprob
            price = kalshi_price

        kelly = kelly_size(prob, price, maker=True, fraction=0.25)
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
            "fee_cents":         kalshi_fee(price),
            "priority":          1 if abs(gap) >= 15 else 2,
            "gap":               round(gap, 1),
            "espn_prob":         mprob,   # reuse field for display
            "entry_limit_cents": max(1, price - 3),
            "take_profit_cents": min(99, int(mprob)),
            "stop_loss_pct":     0.3,
        })

    signals.sort(key=lambda x: abs(x.get("gap", 0)), reverse=True)
    log(f"Metaculus edge: {len(signals)} signals")
    return signals[:3]


def run_strategy_engine(markets, edges, cross_arb, weather_data, espn_games=None,
                        metaculus_qs=None):
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

    # Consensus detection: count how many strategies agree per ticker+direction
    from collections import defaultdict
    consensus = defaultdict(list)  # (ticker, direction) → [signal_types]
    for s in all_signals:
        key = (s.get("ticker", ""), s.get("direction", ""))
        if key[0]:
            consensus[key].append(s.get("type", ""))

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
            yb = mkt.get("yes_bid") or 0
            ya = mkt.get("yes_ask") or 99
            spread = ya - yb
            spread_mult = max(0.5, 1.0 - spread / 20.0)  # penalize wide spreads
            s["quality_score"] = round(vol_mult * spread_mult, 3)
            s["spread_cents"]  = spread
        else:
            s["quality_score"] = 0.5  # unknown liquidity

    # Sort by priority (1=highest), then by kelly_frac * quality_score descending
    live_signals.sort(key=lambda x: (x.get("priority", 9), -(x.get("kelly_frac", 0) * x.get("quality_score", 0.5))))

    # Enrich each signal with kelly_pct and close_time from market lookup
    for i, s in enumerate(live_signals):
        s["rank"] = i + 1
        # Add kelly_pct for easy display (percentage form)
        if "kelly_frac" in s and "kelly_pct" not in s:
            s["kelly_pct"] = round(s["kelly_frac"] * 100, 1)
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
        if mkt and "volume" not in s:
            s["volume"] = mkt.get("volume", 0) or 0

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
        # Political: Senate, House, President
        {"event_ticker": "KXSENATE", "limit": 10},
        {"event_ticker": "KXHOUSE",  "limit": 10},
        # Economic: Fed, CPI, Jobs
        {"event_ticker": "KXFED",    "limit": 10},
        {"event_ticker": "KXCPI",    "limit": 10},
        {"event_ticker": "KXJOBS",   "limit": 10},
        # World Cup 2026 (KXMENWORLDCUP confirmed in trades feed)
        {"event_ticker": "KXMENWORLDCUP", "limit": 20},
        # Crypto daily markets
        {"event_ticker": "KXBTCD",   "limit": 5},
        {"event_ticker": "KXETHD",   "limit": 5},
    ]

    seen = set()
    for params in filters:
        try:
            resp = get("/markets", params)
            if not resp:
                continue
            mkts = resp.get("markets", [])
            for m in mkts[:5]:  # max 5 per filter
                ticker = m.get("ticker", "")
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)

                # Get detailed market data
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


def find_cross_market_arb(kalshi_markets, poly_markets, pi_markets):
    """Find price gaps ≥5¢ between Kalshi and PolyMarket/PredictIt."""
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

    for km in kalshi_markets:
        kp = km.get("yes_bid") or km.get("last_price")
        if kp is None:
            continue
        kwords = set(km.get("title", "").lower().split())

        # vs PolyMarket
        for pwords, pm in poly_idx:
            common = len(kwords & pwords)
            total  = len(kwords | pwords)
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
            common = len(kwords & piwords)
            total  = len(kwords | piwords)
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

    arb.sort(key=lambda x: abs(x["gap"]), reverse=True)
    return arb[:15]

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

# Fetch supplemental political/economic markets (for deeper strategy coverage)
if _on_interval(30):  # Only every 30 minutes to save API credits
    try:
        supp_markets = fetch_supplemental_markets()
        existing_tickers = {m["ticker"] for m in markets_list}
        for m in supp_markets:
            try:
                yes_bid  = cents(m.get("yes_bid_dollars"))
                no_bid   = cents(m.get("no_bid_dollars"))
                yes_ask  = cents(m.get("yes_ask_dollars"))
                no_ask   = cents(m.get("no_ask_dollars"))
                last_p   = cents(m.get("last_price_dollars"))
                yes_price = yes_bid or yes_ask or last_p
                ticker_str = m.get("ticker", "")
                if not ticker_str or ticker_str in existing_tickers:
                    continue
                existing_tickers.add(ticker_str)
                close_raw = str(m.get("close_time", ""))
                markets_list.append({
                    "ticker":      ticker_str,
                    "title":       m.get("title", ""),
                    "yes_bid":     yes_bid,
                    "no_bid":      no_bid,
                    "yes_ask":     yes_ask,
                    "no_ask":      no_ask,
                    "last_price":  last_p,
                    "volume":      m.get("volume", 0) or 0,
                    "close_time":  close_raw,
                    "category":    "Politics" if any(x in ticker_str.upper() for x in ["SENATE","HOUSE","PRES","POL","GOV"]) else "Economics",
                    "_yes_price":  yes_price,
                    "_trade_count": 0,
                    "_supplemental": True,
                })
            except Exception:
                continue
        log(f"Markets list after supplemental: {len(markets_list)}")
    except Exception as e:
        log(f"Supplemental market fetch block error: {e}")

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

try:
    cross_market_arb = find_cross_market_arb(markets_list, poly_markets, pi_markets)
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

# ── Vegas vs Kalshi divergences ───────────────────────────────────────────────
edges = []
line_movements = []
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

# ── Strategy engine ───────────────────────────────────────────────────────────
strategy_signals = []
try:
    strategy_signals = run_strategy_engine(markets_list, edges, cross_market_arb, weather_data,
                                            espn_games=espn_games, metaculus_qs=metaculus_qs)
    log(f"Strategy signals: {len(strategy_signals)}")
except Exception as e:
    log(f"Strategy engine error: {e}")
    traceback.print_exc(file=sys.stderr)

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
    "metaculus":    f"ok_{len(metaculus_qs)}" if metaculus_qs else "empty_or_error",
    "line_movements":  f"ok_{len(line_movements)}" if ODDS_API_KEY else "no_key",
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
signal_summary = {
    "total":     len(strategy_signals),
    "by_type":   dict(sig_counts),
    "high_conf": len([s for s in strategy_signals if s.get("confidence") == "high"]),
    "top_kelly": round(max((s.get("kelly_frac", 0) for s in strategy_signals), default=0) * 100, 1),
}

(docs_dir / "data.json").write_text(json.dumps({
    "generated":     ts_str,
    "balance_cents": balance_cents,
    "positions":     positions_list,
    "markets":       clean_markets,
    "best_picks":    best_picks,
    "edges":         edges,
    "espn_games":    espn_games,
    "espn_injuries": espn_injuries,
    "espn_news":     espn_news,
    "crypto":        crypto_prices,
    "crypto_global": crypto_global,
    "crypto_movers": crypto_movers,
    "sparklines":    crypto_sparklines,
    "fear_greed":    fear_greed,
    "metaculus":     metaculus_qs,
    "fred":           fred_data,
    "polymarket":     poly_markets,
    "predictit":      pi_markets,
    "cross_arb":      cross_market_arb,
    "weather":          weather_data,
    "line_movements":   line_movements,
    "strategy_signals": strategy_signals,
    "signal_summary":   signal_summary,
    "health":         health,
}, indent=2))
log(f"Saved JSON -> {docs_dir / 'data.json'}")
