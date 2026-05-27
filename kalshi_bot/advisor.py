"""
advisor.py — Pulls live Kalshi data and formats a rich context
block so Claude (or any LLM) can give you specific, data-backed
trading advice.

Usage:
    from kalshi_bot.advisor import build_context
    print(build_context())          # paste this into your Claude chat
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .client import KalshiClient
from .display import markets_to_text

# Path to pre-computed data.json from the GitHub Actions snapshot
_DATA_JSON = Path(__file__).parent.parent / "docs" / "data.json"
_SNAPSHOT_TXT = Path(__file__).parent.parent / "data" / "snapshot.txt"


def _load_precomputed() -> dict:
    """Load the pre-computed snapshot data if available."""
    if _DATA_JSON.exists():
        try:
            data = json.loads(_DATA_JSON.read_text())
            return data
        except Exception:
            pass
    return {}


def _fmt_strategy_signals(signals: list) -> str:
    """Format strategy signals as a readable table."""
    if not signals:
        return "  No strategy signals available."
    lines = [
        f"  {'#':<3} {'Type':<20} {'Dir':<8} {'Ticker':<36} {'P':>3} {'Kelly':>6}  Conf  Days  Rationale"
    ]
    lines.append("  " + "-" * 112)
    for i, s in enumerate(signals[:15], 1):
        direction_short = "YES" if "YES" in s.get("direction", "") else (
            "NO" if "NO" in s.get("direction", "") else "ALL")
        kelly_str = f"{s.get('kelly_frac', 0) * 100:.1f}%"
        conf = (s.get("confidence", "?")[:3]).upper()
        days = s.get("days_until_close")
        days_str = f"{days:.0f}d" if days is not None else "  ? "
        rationale = s.get("rationale", "")[:50]
        # Live bid/ask for execution reference
        lb = s.get("live_bid")
        la = s.get("live_ask")
        live_str = f" [{lb}↔{la}]" if lb and la else ""
        lines.append(
            f"  {i:<3} {s.get('type', '?')[:20]:<20} {direction_short:<8} "
            f"{s.get('ticker', '?')[:36]:<36} {s.get('price', 0):>3}¢ {kelly_str:>6}  {conf:<5} {days_str:<5}  {rationale}{live_str}"
        )
    return "\n".join(lines)


def build_context(
    market_limit: int = 30,
    include_positions: bool = True,
    include_orderbooks_for: list[str] | None = None,
) -> str:
    """
    Build a comprehensive text snapshot of your Kalshi account and the
    current market landscape.  Paste this block into a Claude conversation
    to get strategy advice.

    Enriched with pre-computed strategy signals, Vegas edges, FRED data,
    Fear & Greed index, and other indicators from the automated snapshot.
    """
    client = KalshiClient()
    sections: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections.append(f"# Kalshi Market Context  [{ts}]")
    sections.append("=" * 70)

    # ── Load pre-computed data from snapshot ─────────────────────────────────
    precomputed = _load_precomputed()
    pc_age = ""
    if precomputed.get("generated"):
        gen_ts = precomputed["generated"]
        sections.append(f"\n⏰ Pre-computed data from: {gen_ts}")
        pc_age = f" (as of {gen_ts})"

    # ── Portfolio ──────────────────────────────────────────────────────────────
    if include_positions:
        try:
            balance = client.get_balance()
            bal_cents = balance.get("balance", 0) or 0
            sections.append(f"\n## 💰 Portfolio Balance: ${bal_cents / 100:.2f}")
        except Exception as e:
            sections.append(f"\n## Portfolio balance\nERROR: {e}")

        try:
            pos_resp = client.get_positions()
            positions = pos_resp.get("market_positions", [])
            if positions:
                lines = [f"\n## 📊 Open Positions ({len(positions)})"]
                for p in positions:
                    qty = p.get("position", 0)
                    side = "YES" if qty > 0 else "NO"
                    exp = p.get("market_exposure", 0) or 0
                    lines.append(
                        f"  {side:3} {abs(qty):>4} contracts  "
                        f"exposure=${exp / 100:.2f}  "
                        f"{p.get('ticker', '')}"
                    )
                sections.append("\n".join(lines))
            else:
                sections.append("\n## Open Positions\n  (none)")
        except Exception as e:
            sections.append(f"\n## Open positions\nERROR: {e}")

    # ── Strategy Signals (pre-computed) ───────────────────────────────────────
    signals = precomputed.get("strategy_signals", [])
    sig_summary = precomputed.get("signal_summary", {})
    if signals:
        sections.append(
            f"\n## ⚡ Strategy Signals — {len(signals)} signals{pc_age}\n"
            f"   {sig_summary.get('high_conf', 0)} high-confidence · "
            f"Top Kelly: {sig_summary.get('top_kelly', 0)}%"
        )
        sections.append(_fmt_strategy_signals(signals))

        # Best picks summary
        best = precomputed.get("best_picks", [])
        if best:
            sections.append(f"\n### 🎯 Best Picks")
            for p in best[:5]:
                conf_tag = f"[{p.get('confidence', '').upper()[:3]}]" if p.get("confidence") else ""
                sections.append(
                    f"  {p.get('side', '?'):3} {p.get('price', 0):>3}¢  {conf_tag:5}  "
                    f"{p.get('ticker', '')[:36]:<36}  {p.get('title', '')[:40]}"
                )

    # ── Vegas Edges ────────────────────────────────────────────────────────────
    edges = precomputed.get("edges", [])
    if edges:
        sections.append(f"\n## 🎰 Vegas Edge Alerts — {len(edges)} edges")
        for e in edges[:5]:
            sections.append(
                f"  {e.get('direction', '?'):12}  {e.get('ticker', '')[:36]:<36}  "
                f"Kalshi={e.get('kalshi_price')}¢ Vegas={e.get('vegas_prob')}¢  "
                f"Gap={e.get('gap', 0):+.1f}¢"
            )

    # ── Cross-Platform Arb ────────────────────────────────────────────────────
    arb = precomputed.get("cross_arb", [])
    if arb:
        sections.append(f"\n## 🔄 Cross-Platform Arb — {len(arb)} opportunities")
        for a in arb[:3]:
            sections.append(
                f"  {a.get('direction', '?')[:50]}  "
                f"Platform={a.get('platform', '?')} {a.get('platform_price', '?')}¢  "
                f"Gap={a.get('gap', 0):+.1f}¢"
            )

    # ── Market Conditions ─────────────────────────────────────────────────────
    sections.append("\n## 📈 Market Conditions")

    # Fear & Greed
    fg = precomputed.get("fear_greed", {})
    if fg.get("value") is not None:
        fgv = fg["value"]
        sentiment = "EXTREME FEAR 🔴" if fgv <= 20 else (
            "Fear 🟠" if fgv <= 40 else (
            "Neutral 🟡" if fgv <= 60 else (
            "Greed 🟢" if fgv <= 80 else "EXTREME GREED 🔥")))
        sections.append(f"  Fear & Greed Index: {fgv}/100 — {sentiment}")
        if fgv <= 20:
            sections.append("  → Strong contrarian signal: consider BUY YES on crypto up-markets")
        elif fgv >= 80:
            sections.append("  → Bubble warning: consider BUY NO on crypto up-markets")

    # FRED economic data
    fred = precomputed.get("fred", {})
    if fred:
        fred_lines = ["  Economic indicators:"]
        for key, d in list(fred.items())[:5]:
            val = d.get("value")
            change = d.get("change")
            if val is not None:
                chg_str = f" ({'+' if change and change > 0 else ''}{change:.2f})" if change else ""
                fred_lines.append(
                    f"    {d.get('label', key):<25}: {val}{d.get('unit', '')}{chg_str}"
                )
        sections.append("\n".join(fred_lines))

    # Crypto prices
    crypto = precomputed.get("crypto", {})
    if crypto:
        crypto_lines = ["  Crypto prices:"]
        for sym in ["btc", "eth", "sol"]:
            c = crypto.get(sym)
            if c:
                chg = c.get("change_24h", 0)
                arr = "▲" if chg >= 0 else "▼"
                crypto_lines.append(
                    f"    {sym.upper():<6}: ${c.get('usd', 0):>10,.0f}  {arr}{abs(chg):.1f}% 24h"
                )
        sections.append("\n".join(crypto_lines))

    # ── Sharp Price Moves (Kalshi) ─────────────────────────────────────────────
    price_moves = precomputed.get("price_movements", [])
    if price_moves:
        sections.append(f"\n## 💸 Sharp Price Moves on Kalshi — {len(price_moves)} moves")
        for m in price_moves[:5]:
            sections.append(
                f"  {'▲' if m.get('move',0)>0 else '▼'} {abs(m.get('move',0)):.1f}¢  "
                f"{m.get('ticker', '')[:36]:<36}  "
                f"{m.get('prev_price','?')}¢ → {m.get('curr_price','?')}¢"
            )

    # ── ESPN Sports Context ────────────────────────────────────────────────────
    espn_games = precomputed.get("espn_games", [])
    injuries = precomputed.get("espn_injuries", [])
    if espn_games:
        sections.append(f"\n## 🏟 Upcoming Games — {len(espn_games)} games")
        for g in espn_games[:6]:
            date_str = g.get("date", "")[:10]
            sections.append(
                f"  {g.get('away_team', '?')[:20]} @ {g.get('home_team', '?')[:20]}"
                f"  {date_str}  [{g.get('status', '')}]"
            )
    if injuries:
        out_players = [i for i in injuries if i.get("status") in ("Out", "Doubtful")][:5]
        if out_players:
            sections.append(f"\n  ⚠️ Injuries: " + ", ".join(
                f"{i.get('player','')} ({i.get('team','')}) {i.get('status','')}"
                for i in out_players
            ))

    # ── Playoff Series ─────────────────────────────────────────────────────────
    playoff = precomputed.get("espn_playoff_series", [])
    if playoff:
        sections.append(f"\n## 🏆 Playoff Series Standings")
        for s in playoff:
            hw, aw = s.get("home_wins", 0), s.get("away_wins", 0)
            leader_text = f"{s.get('leader', '?')} leads {max(hw,aw)}-{min(hw,aw)}" if hw != aw else "Tied"
            sections.append(
                f"  [{s.get('league','?').upper()}] {s.get('away_team','')} @ {s.get('home_team','')}  —  {leader_text}"
            )

    # ── Live Markets ──────────────────────────────────────────────────────────
    try:
        resp = client.get_markets(status="open", limit=market_limit)
        live_markets = resp.get("markets", [])
        sections.append(f"\n## 📋 Live Open Markets (top {len(live_markets)})")
        sections.append(markets_to_text(live_markets))
    except Exception as e:
        sections.append(f"\n## Open markets\nERROR: {e}")
        live_markets = []

    # ── Specific orderbooks ────────────────────────────────────────────────────
    for ticker in (include_orderbooks_for or []):
        try:
            ob = client.get_market_orderbook(ticker)
            sections.append(f"\n## 📖 Orderbook — {ticker}\n{json.dumps(ob, indent=2)}")
        except Exception as e:
            sections.append(f"\n## Orderbook — {ticker}\nERROR: {e}")

    # ── Pre-computed snapshot text (if available) ─────────────────────────────
    if _SNAPSHOT_TXT.exists():
        try:
            snap = _SNAPSHOT_TXT.read_text()
            # Only include the strategy section from snapshot.txt (not the raw market list)
            if "## STRATEGY SIGNALS" in snap:
                strat_section = snap[snap.index("## STRATEGY SIGNALS"):]
                sections.append(f"\n## 📊 Full Snapshot Analysis\n{strat_section[:3000]}")
        except Exception:
            pass

    return "\n".join(sections)
