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

from .client import KalshiClient
from .display import markets_to_text


def build_context(
    market_limit: int = 30,
    include_positions: bool = True,
    include_orderbooks_for: list[str] | None = None,
) -> str:
    """
    Build a comprehensive text snapshot of your Kalshi account and the
    current market landscape.  Paste this block into a Claude conversation
    to get strategy advice.
    """
    client = KalshiClient()
    sections: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections.append(f"# Kalshi Market Context  [{ts}]")

    # ── Exchange status ────────────────────────────────────────────────────────
    try:
        status = client.get_exchange_status()
        sections.append(f"\n## Exchange status\n{json.dumps(status, indent=2)}")
    except Exception as e:
        sections.append(f"\n## Exchange status\nERROR: {e}")

    # ── Open markets ───────────────────────────────────────────────────────────
    try:
        resp = client.get_markets(status="open", limit=market_limit)
        markets = resp.get("markets", [])
        sections.append(f"\n## Open markets (top {len(markets)})")
        sections.append(markets_to_text(markets))
    except Exception as e:
        sections.append(f"\n## Open markets\nERROR: {e}")
        markets = []

    # ── Portfolio ──────────────────────────────────────────────────────────────
    if include_positions:
        try:
            balance = client.get_balance()
            sections.append(f"\n## Portfolio balance\n{json.dumps(balance, indent=2)}")
        except Exception as e:
            sections.append(f"\n## Portfolio balance\nERROR: {e}")

        try:
            pos_resp = client.get_positions()
            positions = pos_resp.get("market_positions", [])
            if positions:
                lines = ["## My open positions"]
                for p in positions:
                    lines.append(
                        f"  {p.get('ticker','')}  "
                        f"qty={p.get('position',0)}  "
                        f"exposure=${p.get('market_exposure',0)/100:.2f}"
                    )
                sections.append("\n" + "\n".join(lines))
            else:
                sections.append("\n## My open positions\n  (none)")
        except Exception as e:
            sections.append(f"\n## My open positions\nERROR: {e}")

    # ── Specific orderbooks ────────────────────────────────────────────────────
    for ticker in (include_orderbooks_for or []):
        try:
            ob = client.get_market_orderbook(ticker)
            sections.append(f"\n## Orderbook — {ticker}\n{json.dumps(ob, indent=2)}")
        except Exception as e:
            sections.append(f"\n## Orderbook — {ticker}\nERROR: {e}")

    return "\n".join(sections)
