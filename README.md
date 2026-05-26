# Kalshi Bot

A Python CLI for Kalshi market data and AI-driven strategy advice.

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. Generate an RSA keypair

Kalshi uses RSA-256 request signing. Generate a keypair:

```bash
openssl genrsa -out kalshi_private_key.pem 2048
openssl rsa -in kalshi_private_key.pem -pubout -out kalshi_public_key.pem
```

Upload `kalshi_public_key.pem` to **[kalshi.com/settings → API Keys](https://kalshi.com/settings)**.  
**Never commit `kalshi_private_key.pem`** — it's in `.gitignore`.

### 3. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:
```
KALSHI_API_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
KALSHI_ENV=demo   # change to "prod" when ready
```

### 4. Verify everything works

```bash
kalshi status
```

You should see:
```
✓ Exchange reachable
✓ Credentials valid — balance: $X.XX
```

## Usage

```bash
kalshi markets              # list open markets
kalshi market TICKER        # single market deep-dive
kalshi orderbook TICKER     # live order book
kalshi portfolio            # your balance + positions
kalshi advise               # full snapshot for AI analysis
```

## Getting advice from Claude

Run `kalshi advise` to dump a market snapshot, then paste it into Claude and ask:
> "Based on this snapshot, what do you think are the best opportunities right now?"

Or just ask Claude directly — if credentials are configured, Claude can run
`kalshi advise` on its own and give you live, data-backed recommendations.
