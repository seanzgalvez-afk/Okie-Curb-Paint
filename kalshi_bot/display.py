"""
Rich-based display helpers — formats Kalshi data for the terminal
and for Claude analysis prompts.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


def _pct(v: Any) -> str:
    """Convert a Kalshi cent price (0-100) to a % string."""
    try:
        return f"{int(v):>3}¢  ({int(v)}%)"
    except (TypeError, ValueError):
        return str(v)


def print_markets(markets: list[dict], title: str = "Open Markets") -> None:
    t = Table(title=title, box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("Ticker", style="cyan", no_wrap=True)
    t.add_column("Title", style="white", max_width=55)
    t.add_column("Yes", style="green", justify="right")
    t.add_column("No", style="red", justify="right")
    t.add_column("Vol", justify="right")
    t.add_column("Closes", style="dim")
    for m in markets:
        t.add_row(
            m.get("ticker", ""),
            m.get("title", ""),
            _pct(m.get("yes_bid", m.get("last_price", "?"))),
            _pct(m.get("no_bid", "?")),
            f"{m.get('volume', 0):,}",
            str(m.get("close_time", ""))[:10],
        )
    console.print(t)


def print_orderbook(ticker: str, ob: dict) -> None:
    yes_asks = ob.get("orderbook", {}).get("yes", [])
    no_asks = ob.get("orderbook", {}).get("no", [])
    t = Table(title=f"Order book — {ticker}", box=box.SIMPLE_HEAVY)
    t.add_column("Yes price", style="green", justify="right")
    t.add_column("Yes qty", justify="right")
    t.add_column("No price", style="red", justify="right")
    t.add_column("No qty", justify="right")
    rows = max(len(yes_asks), len(no_asks))
    for i in range(rows):
        yp = yq = np_ = nq = ""
        if i < len(yes_asks):
            yp, yq = f"{yes_asks[i][0]}¢", str(yes_asks[i][1])
        if i < len(no_asks):
            np_, nq = f"{no_asks[i][0]}¢", str(no_asks[i][1])
        t.add_row(yp, yq, np_, nq)
    console.print(t)


def print_positions(positions: list[dict]) -> None:
    t = Table(title="My Positions", box=box.SIMPLE_HEAVY)
    t.add_column("Ticker", style="cyan")
    t.add_column("Side", style="yellow")
    t.add_column("Qty", justify="right")
    t.add_column("Avg cost", justify="right")
    t.add_column("Mkt value", justify="right")
    for p in positions:
        side = "YES" if p.get("position", 0) > 0 else "NO"
        qty = abs(p.get("position", 0))
        t.add_row(
            p.get("ticker", ""),
            side,
            str(qty),
            f"{p.get('market_exposure', 0) / max(qty, 1):.1f}¢",
            f"${p.get('market_exposure', 0) / 100:.2f}",
        )
    console.print(t)


def markets_to_text(markets: list[dict]) -> str:
    """Compact text representation for pasting into an AI prompt."""
    lines = ["KALSHI MARKETS SNAPSHOT", "=" * 60]
    for m in markets:
        yes = m.get("yes_bid", m.get("last_price", "?"))
        no = m.get("no_bid", "?")
        lines.append(
            f"[{m.get('ticker','')}] {m.get('title','')}\n"
            f"  Yes={yes}¢  No={no}¢  Vol={m.get('volume',0):,}  "
            f"Closes={str(m.get('close_time',''))[:10]}"
        )
    return "\n".join(lines)
