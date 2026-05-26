# Kalshi Bot — Claude Context

This repo gives Claude live access to Kalshi market data so you can get
real-time trading advice at any time.

## Quick start for advice sessions

When the user wants strategy advice, run:

```bash
cd /home/user/Okie-Curb-Paint
source .venv/bin/activate   # or: pip install -e . first
kalshi advise               # dumps a full snapshot to stdout
```

Then use that snapshot to answer questions like:
- "What should I bet on right now?"
- "What's mispriced?"
- "How's my portfolio looking?"

## Common commands

| Command | What it does |
|---|---|
| `kalshi status` | Verify credentials & exchange connectivity |
| `kalshi markets` | List open markets (price, volume, close date) |
| `kalshi market TICKER` | Deep-dive a single market |
| `kalshi orderbook TICKER` | Live order book |
| `kalshi portfolio` | Balance + open positions |
| `kalshi advise` | Full snapshot formatted for AI analysis |
| `kalshi advise --tickers TICKER1,TICKER2` | Add live order books for specific tickers |

## Project layout

```
kalshi_bot/
  client.py     ← Kalshi REST API + RSA auth
  advisor.py    ← Builds snapshot text for AI analysis
  display.py    ← Rich terminal tables
  cli.py        ← `kalshi` CLI entry point
```

## Credentials

Credentials live in `.env` (gitignored). See `.env.example`.

- `KALSHI_API_KEY_ID` — your API key ID from kalshi.com/settings → API Keys
- `KALSHI_PRIVATE_KEY_PATH` — path to your RSA private key PEM
- `KALSHI_ENV` — `demo` or `prod`

### Generating an RSA keypair (one-time setup)

```bash
# Generate 2048-bit RSA key
openssl genrsa -out kalshi_private_key.pem 2048
openssl rsa -in kalshi_private_key.pem -pubout -out kalshi_public_key.pem

# Upload kalshi_public_key.pem to https://kalshi.com/settings → API Keys
# Keep kalshi_private_key.pem secret — never commit it
```

## Environment setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit .env with your key ID and private key path
kalshi status   # should show ✓ on both checks
```

## Strategy notes

Kalshi markets are binary: YES pays $1 if the event happens, NO pays $1 if not.
- Price in cents = implied probability (50¢ ≈ 50% chance)
- Edge = when your probability estimate differs from the market price
- Volume and tight spreads = liquid markets worth trading
- Watch for sudden price moves on events with upcoming catalysts
