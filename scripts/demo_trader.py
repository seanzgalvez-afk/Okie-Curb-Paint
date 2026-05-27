"""
Kalshi Demo Trader — places real orders on Kalshi's demo sandbox.

Reads strategy_signals from docs/data.json, places limit orders on the
demo API (demo-api.kalshi.co) using Kelly-sized positions, and tracks
demo portfolio performance.

Usage:
  python scripts/demo_trader.py

Required env vars (GitHub Secrets):
  KALSHI_DEMO_API_KEY_ID    — demo account key ID from demo.kalshi.co
  KALSHI_DEMO_PRIVATE_KEY   — full PEM content of demo private key
                              (paste raw PEM including BEGIN/END lines)
  KALSHI_DEMO_BANKROLL      — starting bankroll in cents (default: 100000 = $1,000)
"""

import base64, json, os, sys, time, traceback, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Config ────────────────────────────────────────────────────────────────────
DEMO_BASE   = "https://demo-api.kalshi.co/trade-api/v2"
API_PREFIX  = "/trade-api/v2"
REPO_ROOT   = Path(__file__).parent.parent
DATA_JSON   = REPO_ROOT / "docs"  / "data.json"
TRADES_JSON = REPO_ROOT / "data"  / "demo_trades.json"

KEY_ID = os.environ.get("KALSHI_DEMO_API_KEY_ID", "")
if not KEY_ID:
    print("KALSHI_DEMO_API_KEY_ID not set — demo trader skipped", file=sys.stderr)
    sys.exit(0)

# Load private key from env (PEM content) or file
pem_raw = os.environ.get("KALSHI_DEMO_PRIVATE_KEY", "").strip()
if pem_raw:
    # Handle escaped newlines from GitHub secrets
    pem_bytes = pem_raw.replace("\\n", "\n").encode()
else:
    key_path = os.environ.get("KALSHI_DEMO_PRIVATE_KEY_PATH", "")
    if not key_path:
        print("Neither KALSHI_DEMO_PRIVATE_KEY nor KALSHI_DEMO_PRIVATE_KEY_PATH set — skipped", file=sys.stderr)
        sys.exit(0)
    pem_bytes = Path(key_path).read_bytes()

try:
    PRIV_KEY = serialization.load_pem_private_key(pem_bytes, password=None)
except Exception as e:
    print(f"Demo private key load error: {e}", file=sys.stderr)
    sys.exit(1)

BANKROLL_CENTS = int(os.environ.get("KALSHI_DEMO_BANKROLL", "100000"))  # $1,000

def log(msg): print(msg, file=sys.stderr)

def auth_headers(method, path):
    ts  = str(int(time.time() * 1000))
    msg = (ts + method.upper() + API_PREFIX + path).encode()
    sig = base64.b64encode(
        PRIV_KEY.sign(msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
    ).decode()
    return {
        "KALSHI-ACCESS-KEY":       KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type":            "application/json",
    }

def api_get(path, params=None):
    try:
        r = httpx.get(f"{DEMO_BASE}{path}", headers=auth_headers("GET", path),
                      params=params, timeout=15)
        log(f"GET {path} -> {r.status_code}")
        if r.is_success:
            return r.json()
        log(f"  body: {r.text[:200]}")
        return None
    except Exception as e:
        log(f"GET {path} error: {e}")
        return None

def api_post(path, body):
    try:
        r = httpx.post(f"{DEMO_BASE}{path}", headers=auth_headers("POST", path),
                       json=body, timeout=15)
        log(f"POST {path} -> {r.status_code} | {r.text[:400]}")
        if r.is_success:
            return r.json()
        return None
    except Exception as e:
        log(f"POST {path} error: {e}")
        return None

# ── Trade persistence ─────────────────────────────────────────────────────────
def load_trades():
    if TRADES_JSON.exists():
        try:
            return json.loads(TRADES_JSON.read_text())
        except Exception:
            pass
    return {"bankroll_cents": BANKROLL_CENTS, "open": [], "closed": [], "orders": []}

def save_trades(state):
    TRADES_JSON.parent.mkdir(exist_ok=True)
    TRADES_JSON.write_text(json.dumps(state, indent=2))

# ── Demo portfolio ────────────────────────────────────────────────────────────
def get_demo_balance():
    data = api_get("/portfolio/balance")
    if data:
        bal = data.get("balance", 0)
        log(f"Demo balance: ${bal/100:.2f}")
        return bal
    return None

def get_demo_positions():
    data = api_get("/portfolio/positions")
    if data:
        return data.get("market_positions", [])
    return []

def get_demo_orders():
    data = api_get("/portfolio/orders", {"status": "resting"})
    if data:
        return data.get("orders", [])
    return []

def get_market_info(ticker):
    """Get current market orderbook top-of-book."""
    data = api_get(f"/markets/{ticker}/orderbook")
    if not data:
        return None
    book = data.get("orderbook", data)
    yes_asks = book.get("yes", [])  # [[price, qty], ...]
    yes_bids  = book.get("no", [])   # NO bids = YES asks equivalent
    best_ask = yes_asks[0][0] if yes_asks else None
    best_bid = (100 - yes_bids[0][0]) if yes_bids else None
    return {"best_bid": best_bid, "best_ask": best_ask}

# ── Order placement ───────────────────────────────────────────────────────────
def place_limit_order(ticker, side, price_cents, quantity, signal_type, rationale):
    """
    Place a limit order on demo Kalshi.
    side: 'yes' or 'no'
    price_cents: limit price (1-99)
    quantity: number of contracts
    Returns order dict or None.
    """
    # Kalshi requires client_order_id (unique per order)
    client_id = str(uuid.uuid4())
    body = {
        "action":          "buy",
        "client_order_id": client_id,
        "ticker":          ticker,
        "type":            "limit",
        "side":            side,
        "count":           quantity,
        "yes_price":       price_cents if side == "yes" else (100 - price_cents),
        "no_price":        (100 - price_cents) if side == "yes" else price_cents,
    }
    log(f"  Placing order: {body}")
    result = api_post("/portfolio/orders", body)
    if result:
        order_id = result.get("order", {}).get("order_id", client_id)
        log(f"  ✓ Order placed: {side.upper()} {ticker} @ {price_cents}¢ x{quantity} | id={order_id[:8]}")
        return {
            "order_id":    order_id,
            "ticker":      ticker,
            "side":        side,
            "price_cents": price_cents,
            "quantity":    quantity,
            "signal_type": signal_type,
            "rationale":   rationale[:100],
            "placed_at":   datetime.now(timezone.utc).isoformat(),
            "status":      "resting",
        }
    return None

def cancel_order(order_id):
    result = api_post(f"/portfolio/orders/{order_id}/decrease", {"reduce_by": 999999})
    return result is not None

# ── Signal processing ─────────────────────────────────────────────────────────
def should_trade_signal(signal, state):
    """
    Returns (should_trade, side, price_cents, quantity) or (False, ...) if skip.
    Rules:
    - Skip if already have open position/order on this ticker
    - Skip if priority > 2 (low priority)
    - Skip if kelly_frac == 0
    - Cap at 5% of bankroll per position
    """
    ticker = signal.get("ticker", "")
    if not ticker:
        return False, None, 0, 0

    # Skip if already have open trade on this ticker
    open_tickers = {t["ticker"] for t in state["open"]}
    order_tickers = {o["ticker"] for o in state["orders"]}
    if ticker in open_tickers or ticker in order_tickers:
        log(f"  Skip {ticker}: already have open position/order")
        return False, None, 0, 0

    priority   = signal.get("priority", 9)
    kelly_frac = signal.get("kelly_frac", 0)
    direction  = signal.get("direction", "")
    price      = signal.get("price", 50)

    if priority > 2:
        return False, None, 0, 0
    if kelly_frac <= 0:
        return False, None, 0, 0

    # Cap position at 5% of bankroll
    max_risk_cents = int(state["bankroll_cents"] * 0.05)
    kelly_risk_cents = int(state["bankroll_cents"] * min(kelly_frac, 0.05))
    risk_cents = min(kelly_risk_cents, max_risk_cents)

    if risk_cents < 10:  # min $0.10 per trade
        return False, None, 0, 0

    side = "yes" if "YES" in direction.upper() else "no"
    # Place limit 1¢ better than current price (maker order)
    if side == "yes":
        limit_price = max(1, price - 1)
    else:
        limit_price = min(99, price + 1)

    # Calculate quantity from risk budget
    cost_per_contract = limit_price  # cost in cents per YES contract
    quantity = max(1, risk_cents // cost_per_contract)

    return True, side, limit_price, quantity

# ── Settlement check ──────────────────────────────────────────────────────────
def check_settlements(state):
    """
    Check if any open paper records or orders have settled.
    Queries demo positions and order history.
    """
    if not state["open"] and not state["orders"]:
        return state

    # Get current resting orders
    live_orders = {o.get("order_id"): o for o in get_demo_orders()}

    # Check tracked orders — move settled ones to closed
    still_open_orders = []
    for order in state["orders"]:
        oid = order.get("order_id", "")
        if oid not in live_orders:
            # Order no longer resting — filled, cancelled, or market resolved
            order["status"] = "filled_or_resolved"
            order["closed_at"] = datetime.now(timezone.utc).isoformat()
            state["closed"].append(order)
            log(f"  Order settled: {order['ticker']} {order['side']}")
        else:
            still_open_orders.append(order)
    state["orders"] = still_open_orders

    return state

# ── Stats calculation ─────────────────────────────────────────────────────────
def calc_stats(state):
    closed = state.get("closed", [])
    if not closed:
        return {
            "total_trades":   0,
            "bankroll_cents": state.get("bankroll_cents", BANKROLL_CENTS),
            "open_count":     len(state.get("orders", [])),
        }

    # For now track order counts (P&L requires market resolution data)
    total = len(closed)
    return {
        "total_trades":    total,
        "open_count":      len(state.get("orders", [])),
        "bankroll_cents":  state.get("bankroll_cents", BANKROLL_CENTS),
        "signals_placed":  total,
        "last_updated":    datetime.now(timezone.utc).isoformat(),
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log("=== Demo Trader Starting ===")

    # 1. Load strategy signals from data.json
    if not DATA_JSON.exists():
        log("data.json not found — run snapshot.py first")
        sys.exit(0)

    try:
        data = json.loads(DATA_JSON.read_text())
    except Exception as e:
        log(f"data.json parse error: {e}")
        sys.exit(1)

    signals = data.get("strategy_signals", [])
    log(f"Loaded {len(signals)} strategy signals")

    # 2. Verify demo API connection
    balance = get_demo_balance()
    if balance is None:
        log("Could not connect to demo API — check KALSHI_DEMO_API_KEY_ID")
        sys.exit(1)

    # 3. Load trade state
    state = load_trades()
    if balance > 0:
        state["bankroll_cents"] = balance  # use real demo balance

    # 4. Check settlements on existing orders
    state = check_settlements(state)

    # 5. Get current demo positions
    positions = get_demo_positions()
    log(f"Demo positions: {len(positions)}")

    # 6. Process new signals
    new_orders = 0
    max_new_orders = 5  # max new orders per run

    high_priority = [s for s in signals if s.get("priority", 9) <= 2]
    log(f"High-priority signals to evaluate: {len(high_priority)}")

    # Pull current Kalshi markets for live prices + close times
    markets_data = data.get("markets", [])
    market_prices = {m["ticker"]: m.get("yes_bid", m.get("yes_ask", 50))
                     for m in markets_data if "ticker" in m}
    market_close  = {m["ticker"]: m.get("close_time", "")
                     for m in markets_data if "ticker" in m}

    def market_is_live(ticker):
        """Return True if market closes more than 2 hours from now."""
        ct = market_close.get(ticker, "")
        if not ct:
            return True  # unknown — allow
        try:
            close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            return (close_dt - datetime.now(timezone.utc)).total_seconds() > 7200
        except Exception:
            return True

    for signal in high_priority[:10]:
        if new_orders >= max_new_orders:
            log(f"  Reached max new orders ({max_new_orders}) for this run")
            break

        sig_type  = signal.get("type", "")
        ticker    = signal.get("ticker", "")
        direction = signal.get("direction", "")

        log(f"\nEvaluating: [{sig_type}] {ticker} {direction}")

        # Skip expired / closing-soon markets
        check_ticker = (signal.get("contracts") or [ticker])[0]
        if not market_is_live(check_ticker):
            log(f"  Skip {ticker}: market closing soon or expired")
            continue

        # ── Bundle arb: buy YES on ALL contracts in the series ──────────────
        if sig_type == "bundle_arb" and direction == "BUY ALL":
            contracts = signal.get("contracts", [])
            if not contracts:
                log("  Skip bundle_arb: no contracts list")
                continue

            # Skip if any leg already tracked
            tracked = {o["ticker"] for o in state["orders"]}
            if any(c in tracked for c in contracts):
                log(f"  Skip bundle_arb: already have leg open")
                continue

            # Size: risk up to 10% of bankroll split across legs
            total_cost = signal.get("price", 80)  # sum of YES prices
            risk_budget = int(state["bankroll_cents"] * 0.10)
            qty = max(1, risk_budget // max(total_cost, 1))
            qty = min(qty, 10)  # hard cap 10 contracts per leg

            log(f"  Bundle arb: {len(contracts)} legs, qty={qty}, total_cost≈{total_cost}¢")
            placed_legs = 0
            for leg_ticker in contracts:
                # Get live price for this contract
                leg_price = market_prices.get(leg_ticker, 50)
                limit_price = max(1, min(99, leg_price))  # at market for arb speed
                order = place_limit_order(
                    ticker=leg_ticker,
                    side="yes",
                    price_cents=limit_price,
                    quantity=qty,
                    signal_type="bundle_arb",
                    rationale=signal.get("rationale", "")[:100],
                )
                if order:
                    order["bundle_series"] = ticker
                    state["orders"].append(order)
                    new_orders += 1
                    placed_legs += 1
            log(f"  → Placed {placed_legs}/{len(contracts)} bundle legs")
            continue

        # ── Standard single-contract signals ────────────────────────────────
        if not ticker:
            continue

        should, side, price, qty = should_trade_signal(signal, state)
        if not should:
            continue

        # Get live market price to verify signal still valid
        book = get_market_info(ticker)
        if book:
            log(f"  Market: bid={book.get('best_bid')}¢ ask={book.get('best_ask')}¢ | signal={signal.get('price')}¢")

        order = place_limit_order(
            ticker=ticker,
            side=side,
            price_cents=price,
            quantity=qty,
            signal_type=sig_type,
            rationale=signal.get("rationale", ""),
        )
        if order:
            state["orders"].append(order)
            new_orders += 1
            log(f"  → Tracked: {ticker}")

    log(f"\nNew orders placed: {new_orders}")
    log(f"Total tracked orders: {len(state['orders'])}")

    # 7. Build output for dashboard
    demo_portfolio = {
        "balance_cents":  balance,
        "open_orders":    state["orders"],
        "closed_trades":  state["closed"][-20:],  # last 20
        "stats":          calc_stats(state),
        "last_run":       datetime.now(timezone.utc).isoformat(),
    }

    # 8. Save state
    save_trades(state)

    # 9. Write to data.json
    try:
        data["demo_portfolio"] = demo_portfolio
        DATA_JSON.write_text(json.dumps(data, indent=2))
        log(f"✓ Updated data.json with demo portfolio")
    except Exception as e:
        log(f"data.json write error: {e}")

    log("=== Demo Trader Done ===")

if __name__ == "__main__":
    main()
