"""
Kalshi bot CLI  —  `kalshi <command>`
"""

from __future__ import annotations

import json
from typing import Optional

import typer
from rich.console import Console

from .client import KalshiClient
from .display import console, print_markets, print_orderbook, print_positions
from .advisor import build_context

app = typer.Typer(
    name="kalshi",
    help="Kalshi market data & advisor CLI",
    add_completion=False,
)


def _client() -> KalshiClient:
    return KalshiClient()


# ── markets ────────────────────────────────────────────────────────────────────
@app.command()
def markets(
    status: str = typer.Option("open", help="open | closed | settled"),
    limit: int = typer.Option(30, help="Max markets to fetch"),
    raw: bool = typer.Option(False, help="Dump raw JSON"),
):
    """List Kalshi markets."""
    resp = _client().get_markets(status=status, limit=limit)
    if raw:
        typer.echo(json.dumps(resp, indent=2))
    else:
        print_markets(resp.get("markets", []), title=f"{status.title()} Markets")


# ── market ─────────────────────────────────────────────────────────────────────
@app.command()
def market(
    ticker: str = typer.Argument(..., help="Market ticker, e.g. HIGHNY-23DEC25-T63"),
    raw: bool = typer.Option(False),
):
    """Get a single market's details."""
    resp = _client().get_market(ticker)
    if raw:
        typer.echo(json.dumps(resp, indent=2))
    else:
        m = resp.get("market", resp)
        console.print_json(json.dumps(m))


# ── orderbook ─────────────────────────────────────────────────────────────────
@app.command()
def orderbook(
    ticker: str = typer.Argument(...),
    depth: int = typer.Option(5, help="Levels per side"),
):
    """Show the order book for a market."""
    ob = _client().get_market_orderbook(ticker, depth=depth)
    print_orderbook(ticker, ob)


# ── history ────────────────────────────────────────────────────────────────────
@app.command()
def history(
    ticker: str = typer.Argument(...),
    limit: int = typer.Option(50),
):
    """Price history for a market."""
    resp = _client().get_market_history(ticker, limit=limit)
    typer.echo(json.dumps(resp, indent=2))


# ── trades ─────────────────────────────────────────────────────────────────────
@app.command()
def trades(
    ticker: Optional[str] = typer.Argument(None, help="Filter to one market"),
    limit: int = typer.Option(20),
):
    """Recent trades."""
    resp = _client().get_trades(ticker=ticker, limit=limit)
    typer.echo(json.dumps(resp, indent=2))


# ── events ─────────────────────────────────────────────────────────────────────
@app.command()
def events(
    status: str = typer.Option("open"),
    limit: int = typer.Option(30),
):
    """List events (groups of markets)."""
    resp = _client().get_events(status=status, limit=limit)
    typer.echo(json.dumps(resp, indent=2))


# ── portfolio ──────────────────────────────────────────────────────────────────
@app.command()
def portfolio():
    """Show your balance and open positions."""
    c = _client()
    balance = c.get_balance()
    console.rule("[bold cyan]Balance")
    console.print_json(json.dumps(balance))

    pos_resp = c.get_positions()
    positions = pos_resp.get("market_positions", [])
    console.rule("[bold cyan]Positions")
    if positions:
        print_positions(positions)
    else:
        console.print("[dim]No open positions[/]")


# ── advise ─────────────────────────────────────────────────────────────────────
@app.command()
def advise(
    limit: int = typer.Option(30, help="Markets to include in snapshot"),
    tickers: Optional[str] = typer.Option(
        None, help="Comma-separated tickers to include orderbooks for"
    ),
    out: Optional[str] = typer.Option(None, help="Save context to a file"),
):
    """
    Build a rich market snapshot for Claude to analyse.

    Run this, then paste the output into your Claude conversation and ask
    for trading advice.
    """
    ob_tickers = [t.strip() for t in tickers.split(",")] if tickers else []
    ctx = build_context(
        market_limit=limit,
        include_positions=True,
        include_orderbooks_for=ob_tickers,
    )
    if out:
        with open(out, "w") as f:
            f.write(ctx)
        console.print(f"[green]Context saved to {out}[/]")
    else:
        typer.echo(ctx)


# ── status ─────────────────────────────────────────────────────────────────────
@app.command()
def status():
    """Check exchange connectivity and credential validity."""
    c = _client()
    try:
        ex = c.get_exchange_status()
        console.print("[green]✓ Exchange reachable[/]")
        console.print_json(json.dumps(ex))
    except Exception as e:
        console.print(f"[red]✗ Exchange error: {e}[/]")
        raise typer.Exit(1)

    try:
        bal = c.get_balance()
        console.print(
            f"[green]✓ Credentials valid — balance: "
            f"${bal.get('balance',0)/100:.2f}[/]"
        )
    except Exception as e:
        console.print(f"[red]✗ Auth error: {e}[/]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
