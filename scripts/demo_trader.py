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

_api_errors = []  # collect errors for dashboard display

def api_post(path, body):
    try:
        r = httpx.post(f"{DEMO_BASE}{path}", headers=auth_headers("POST", path),
                       json=body, timeout=15)
        log(f"POST {path} -> {r.status_code} | {r.text[:400]}")
        if r.is_success:
            return r.json()
        _api_errors.append({"path": path, "status": r.status_code, "body": r.text[:200]})
        return None
    except Exception as e:
        log(f"POST {path} error: {e}")
        _api_errors.append({"path": path, "error": str(e)})
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
def place_limit_order(ticker, side, price_cents, quantity, signal_type, rationale,
                      action="buy"):
    """
    Place a limit order on demo Kalshi.
    side:     'yes' or 'no'
    price_cents: limit price (1-99)
    quantity: number of contracts
    action:   'buy' (default) or 'sell'
    Returns order dict or None.
    """
    # Kalshi requires client_order_id (unique per order)
    client_id = str(uuid.uuid4())
    body = {
        "action":          action,
        "client_order_id": client_id,
        "ticker":          ticker,
        "type":            "limit",
        "side":            side,
        "count":           quantity,
        "yes_price":       price_cents if side == "yes" else (100 - price_cents),
        "no_price":        (100 - price_cents) if side == "yes" else price_cents,
    }
    log(f"  Placing {action.upper()} order: {body}")
    result = api_post("/portfolio/orders", body)
    if result:
        order_id = result.get("order", {}).get("order_id", client_id)
        log(f"  ✓ {action.upper()} order: {side.upper()} {ticker} @ {price_cents}¢ x{quantity} | id={order_id[:8]}")
        return {
            "order_id":         order_id,
            "ticker":           ticker,
            "side":             side,
            "action":           action,
            "price_cents":      price_cents,
            "quantity":         quantity,
            "cost_basis_cents": price_cents * quantity if action == "buy" else 0,
            "signal_type":      signal_type,
            "rationale":        rationale[:100],
            "placed_at":        datetime.now(timezone.utc).isoformat(),
            "status":           "resting",
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

    # Skip conflicted signals — genuine uncertainty, don't trade
    if signal.get("conflict_detected"):
        log(f"  Skip {ticker}: conflict detected ({signal.get('warning','')})")
        return False, None, 0, 0

    # Skip if already have open trade on this ticker
    open_tickers = {t["ticker"] for t in state["open"]}
    order_tickers = {o["ticker"] for o in state["orders"]}
    if ticker in open_tickers or ticker in order_tickers:
        log(f"  Skip {ticker}: already have open position/order")
        return False, None, 0, 0

    priority   = signal.get("priority", 9)
    kelly_frac = signal.get("kelly_frac", 0)
    confidence = signal.get("confidence", "low")
    direction  = signal.get("direction", "")

    # Allow priority ≤2 always, OR priority 3 with high confidence
    if priority > 3:
        return False, None, 0, 0
    if priority == 3 and confidence != "high":
        return False, None, 0, 0
    if kelly_frac <= 0:
        return False, None, 0, 0

    # Cap position at 5% of bankroll; reduce to 2.5% for priority 3
    pct_cap = 0.025 if priority == 3 else 0.05
    max_risk_cents = int(state["bankroll_cents"] * pct_cap)
    kelly_risk_cents = int(state["bankroll_cents"] * min(kelly_frac, pct_cap))
    risk_cents = min(kelly_risk_cents, max_risk_cents)

    if risk_cents < 10:  # min $0.10 per trade
        return False, None, 0, 0

    side = "yes" if "YES" in direction.upper() else "no"

    # Use entry_limit_cents from signal (already in the correct side's price convention)
    # entry_limit_cents = YES price for BUY YES, NO price for BUY NO
    entry = signal.get("entry_limit_cents")
    if entry and 1 <= entry <= 99:
        limit_price = entry
    elif side == "yes":
        limit_price = max(1, (signal.get("price", 50) or 50) - 1)
    else:
        # Fallback: derive NO price from YES price
        yes_price = signal.get("price", 50) or 50
        limit_price = min(99, (100 - yes_price) - 1)

    # Calculate quantity from risk budget (cost_per_contract = limit_price for both sides)
    quantity = max(1, risk_cents // limit_price)

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

    # Check tracked orders — move settled ones to closed, place take-profits
    still_open_orders = []
    new_tp_orders = []  # take-profit sell orders to add
    for order in state["orders"]:
        oid = order.get("order_id", "")
        if oid not in live_orders:
            # Order no longer resting — filled, cancelled, or market resolved
            order["status"] = "filled_or_resolved"
            order["closed_at"] = datetime.now(timezone.utc).isoformat()
            if "entry_price_cents" not in order:
                order["entry_price_cents"] = order.get("price_cents", 50)

            # If this was a BUY order that filled, place take-profit SELL
            tp = order.get("take_profit_cents")
            if (tp and order.get("action", "buy") == "buy" and
                    order.get("status") == "filled_or_resolved" and
                    not order.get("tp_placed")):
                log(f"  Placing take-profit sell: {order['ticker']} {order.get('side','yes').upper()} @ {tp}¢")
                tp_order = place_limit_order(
                    ticker=order["ticker"],
                    side=order.get("side", "yes"),
                    price_cents=tp,
                    quantity=order.get("quantity", 1),
                    signal_type=f"tp_{order.get('signal_type','?')}",
                    rationale=f"Take-profit sell for {order['ticker']} entry @ {order.get('price_cents')}¢ → target {tp}¢",
                    action="sell",  # ← SELL to close the position
                )
                if tp_order:
                    tp_order["is_take_profit"] = True
                    tp_order["parent_order_id"] = oid
                    new_tp_orders.append(tp_order)
                    order["tp_placed"] = True

            state["closed"].append(order)
            log(f"  Order settled: {order['ticker']} {order['side']}")
        else:
            # Fetch current market price for open positions
            book = get_market_info(order.get("ticker", ""))
            if book:
                mid = None
                if book.get("best_bid") is not None and book.get("best_ask") is not None:
                    mid = (book["best_bid"] + book["best_ask"]) / 2
                elif book.get("best_bid") is not None:
                    mid = book["best_bid"]
                elif book.get("best_ask") is not None:
                    mid = book["best_ask"]
                if mid is not None:
                    order["last_known_price"] = mid

            # Stop-loss check: if market moved badly, cancel and close
            sl_pct = order.get("stop_loss_pct")
            if sl_pct and order.get("last_known_price") and order.get("price_cents"):
                entry   = order["price_cents"]   # YES price for YES orders, NO price for NO orders
                yes_mid = order["last_known_price"]  # always YES mid from get_market_info
                if order.get("side") == "yes":
                    # YES position: loss when YES price falls below entry
                    loss_pct = (entry - yes_mid) / entry if entry > 0 else 0
                    pnl_cents = int((yes_mid - entry) * order.get("quantity", 1))
                else:
                    # NO position: entry is NO price, current NO = 100 - yes_mid
                    no_current = 100 - yes_mid
                    loss_pct = (entry - no_current) / entry if entry > 0 else 0
                    pnl_cents = int((no_current - entry) * order.get("quantity", 1))
                if loss_pct > sl_pct:
                    log(f"  Stop-loss triggered: {order['ticker']} {order.get('side','yes').upper()} loss={loss_pct:.0%} > {sl_pct:.0%} limit")
                    cancel_order(oid)
                    order["status"] = "stop_loss_triggered"
                    order["closed_at"] = datetime.now(timezone.utc).isoformat()
                    order["pnl_cents"] = pnl_cents
                    state["closed"].append(order)
                    continue

            still_open_orders.append(order)

    state["orders"] = still_open_orders + new_tp_orders

    return state

# ── Stats calculation ─────────────────────────────────────────────────────────
def compute_portfolio_stats(state):
    """
    Compute CLV-aware portfolio statistics from the trade state.

    CLV (Closing Line Value) = entry_price_cents - 50.
    Positive CLV means we bought when the market implied we had an edge
    relative to the 50¢ breakeven; negative means the opposite.

    Returns a dict with:
      total_trades, wins, losses, win_rate, avg_clv,
      total_pnl_cents, roi_pct, open_count, bankroll_cents, last_updated.
    """
    closed = state.get("closed", [])
    total  = len(closed)
    open_count = len(state.get("orders", []))
    bankroll   = state.get("bankroll_cents", BANKROLL_CENTS)

    if not closed:
        return {
            "total_trades":   0,
            "wins":           0,
            "losses":         0,
            "win_rate":       None,
            "avg_clv":        None,
            "total_pnl_cents":0,
            "roi_pct":        None,
            "open_count":     open_count,
            "bankroll_cents": bankroll,
            "last_updated":   datetime.now(timezone.utc).isoformat(),
        }

    wins   = sum(1 for t in closed if (t.get("pnl_cents") or 0) > 0)
    losses = sum(1 for t in closed if (t.get("pnl_cents") or 0) < 0)

    # CLV: entry_price_cents - 50 (positive = bought with edge vs. breakeven)
    clv_values = [
        t["entry_price_cents"] - 50
        for t in closed
        if "entry_price_cents" in t
    ]
    avg_clv = (sum(clv_values) / len(clv_values)) if clv_values else None

    total_pnl    = sum((t.get("pnl_cents") or 0) for t in closed)
    cost_bases   = [t.get("cost_basis_cents") or 0 for t in closed]
    total_cost   = sum(cost_bases)
    roi_pct      = (total_pnl / total_cost * 100) if total_cost else None

    # Per-strategy breakdown
    strategy_stats = {}
    for order in closed:
        sig_type = order.get("signal_type", "unknown")
        if sig_type not in strategy_stats:
            strategy_stats[sig_type] = {"wins": 0, "losses": 0, "pnl": 0}
        if (order.get("pnl_cents") or 0) > 0:
            strategy_stats[sig_type]["wins"] += 1
            strategy_stats[sig_type]["pnl"] += order.get("pnl_cents", 0)
        else:
            strategy_stats[sig_type]["losses"] += 1
            strategy_stats[sig_type]["pnl"] -= order.get("cost_basis_cents", 0)

    return {
        "total_trades":    total,
        "wins":            wins,
        "losses":          losses,
        "resolved":        total,
        "win_rate":        (wins / total) if total else None,
        "avg_clv":         avg_clv,
        "total_pnl":       total_pnl,
        "total_pnl_cents": total_pnl,
        "roi_pct":         roi_pct,
        "open_count":      open_count,
        "bankroll_cents":  bankroll,
        "signals_placed":  total,
        "by_strategy":     strategy_stats,
        "last_updated":    datetime.now(timezone.utc).isoformat(),
    }


def calc_stats(state):
    """Thin wrapper kept for backwards-compat; delegates to compute_portfolio_stats."""
    return compute_portfolio_stats(state)

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

    # Consensus signals (multi-strategy agreement) get top priority
    # Sort: consensus_count DESC, then priority ASC, then kelly_frac DESC
    consensus_signals = sorted(
        [s for s in signals if (s.get("consensus_count") or 0) >= 2],
        key=lambda s: (-s.get("consensus_count", 0), s.get("priority", 9), -s.get("kelly_frac", 0))
    )
    high_priority = consensus_signals + [
        s for s in signals
        if s.get("ticker") not in {x.get("ticker") for x in consensus_signals}
        and (s.get("priority", 9) <= 2
             or (s.get("priority", 9) == 3 and s.get("confidence") == "high"))
    ]
    log(f"High-priority signals: {len(high_priority)} ({len(consensus_signals)} consensus)")

    # Pull current Kalshi markets for live prices + close times
    # Note: data.json has "clean_markets" (no _-prefixed fields), so use what's available
    markets_data = data.get("markets", [])
    market_prices = {}
    market_close  = {}
    for m in markets_data:
        tk = m.get("ticker")
        if not tk:
            continue
        # Best price: yes_bid → yes_ask → last_price → 50
        price = m.get("yes_bid") or m.get("yes_ask") or m.get("last_price") or 50
        market_prices[tk] = price
        market_close[tk] = m.get("close_time", "")

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
                # For bundle arb: ALWAYS use individual market price, not the aggregate
                # entry_limit_cents on bundle signals = TOTAL cost (sum), not per-leg
                leg_price = market_prices.get(leg_ticker, 50)
                limit_price = max(1, min(99, leg_price))
                order = place_limit_order(
                    ticker=leg_ticker,
                    side="yes",
                    price_cents=limit_price,
                    quantity=qty,
                    signal_type="bundle_arb",
                    rationale=signal.get("rationale", "")[:100],
                )
                if order:
                    order["bundle_series"]     = ticker
                    order["take_profit_cents"] = signal.get("take_profit_cents")
                    order["stop_loss_pct"]     = signal.get("stop_loss_pct")
                    order["fair_value_cents"]  = signal.get("take_profit_cents")
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
            order["take_profit_cents"] = signal.get("take_profit_cents")
            order["stop_loss_pct"]     = signal.get("stop_loss_pct")
            order["fair_value_cents"]  = signal.get("take_profit_cents")
            state["orders"].append(order)
            new_orders += 1
            log(f"  → Tracked: {ticker}")

    log(f"\nNew orders placed: {new_orders}")
    log(f"Total tracked orders: {len(state['orders'])}")

    if high_priority:
        best = max(high_priority, key=lambda s: (s.get("kelly_frac", 0) * (s.get("take_profit_cents", 0) or 0)))
        if best.get("kelly_frac", 0) > 0:
            log(f"★ BEST SIGNAL: [{best.get('type','?')}] {best.get('ticker','')} {best.get('direction','')} @ {best.get('entry_limit_cents','?')}¢ → TP {best.get('take_profit_cents','?')}¢ | Kelly {best.get('kelly_frac',0)*100:.1f}%")

    # 7. Build output for dashboard

    # Track P&L history for charting (keep last 100 snapshots)
    stats = compute_portfolio_stats(state)
    pnl_snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pnl_cents": stats.get("total_pnl", 0),
        "win_rate": stats.get("win_rate", 0),
        "roi_pct": stats.get("roi_pct", 0),
        "n_trades": stats.get("resolved", 0),
    }
    if "pnl_history" not in state:
        state["pnl_history"] = []
    state["pnl_history"].append(pnl_snapshot)
    state["pnl_history"] = state["pnl_history"][-100:]  # keep last 100

    portfolio_stats = stats
    demo_portfolio = {
        "balance_cents":  balance,
        "open_orders":    state["orders"],
        "closed_trades":  state["closed"][-20:],  # last 20
        "stats":          portfolio_stats,
        "pnl_history":    state.get("pnl_history", []),
        "last_run":       datetime.now(timezone.utc).isoformat(),
        "run_summary": {
            "signals_evaluated": len(high_priority[:10]),
            "orders_placed": new_orders,
            "signals_skipped_expired": sum(
                1 for s in high_priority[:10]
                if not market_is_live((s.get("contracts") or [s.get("ticker","")])[0])
            ),
            "signals_skipped_low_priority": len([s for s in signals if s.get("priority",9) > 2]),
            "api_key_set": bool(KEY_ID),
            "api_errors": _api_errors[-5:],  # last 5 errors
        },
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
