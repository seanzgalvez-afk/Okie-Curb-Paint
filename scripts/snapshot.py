"""
Fetch Kalshi market data using the official kalshi-python SDK.
Writes a snapshot to data/snapshot.txt for Claude to analyze.
"""
import base64
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Write private key to temp file (SDK requires a file path)
raw = os.environ.get("KALSHI_PRIVATE_KEY", "")
if not raw:
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
else:
    raw = raw.strip()
    if not raw.startswith("-----"):
        raw = base64.b64decode(raw).decode()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pem", mode="w")
    tmp.write(raw)
    tmp.close()
    key_path = tmp.name

key_id  = os.environ.get("KALSHI_API_KEY_ID", "")
env     = os.environ.get("KALSHI_ENV", "prod")
out     = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-o" else None

if not key_id:
    sys.exit("ERROR: KALSHI_API_KEY_ID not set")

import kalshi_python
from kalshi_python import ApiClient, Configuration
from kalshi_python.api.market_api import MarketApi
from kalshi_python.api.portfolio_api import PortfolioApi

cfg = Configuration()
cfg.host = (
    "https://trading-api.kalshi.com/trade-api/v2" if env == "prod"
    else "https://demo-api.kalshi.co/trade-api/v2"
)

client = ApiClient(configuration=cfg)
client.key_id = key_id
with open(key_path, "rb") as f:
    from cryptography.hazmat.primitives import serialization
    client.private_key = serialization.load_pem_private_key(f.read(), password=None)

# Patch the client to use Kalshi auth headers
import time, hashlib
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

original_call = client.call_api.__func__ if hasattr(client.call_api, '__func__') else None

# Monkey-patch REST client to inject auth headers
from kalshi_python.rest import RESTClientObject
orig_request = RESTClientObject.request

def authed_request(self, method, url, *args, **kwargs):
    ts = int(time.time() * 1000)
    from urllib.parse import urlparse
    path = urlparse(url).path
    msg = f"{ts}{method.upper()}{path}".encode()
    sig = base64.b64encode(
        client.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    ).decode()
    headers = kwargs.get("headers", {}) or {}
    headers.update({
        "KALSHI-ACCESS-KEY": client.key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": sig,
    })
    kwargs["headers"] = headers
    return orig_request(self, method, url, *args, **kwargs)

RESTClientObject.request = authed_request

ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
lines = [f"# Kalshi Market Snapshot\n# Generated: {ts_str}\n{'='*70}"]

market_api = MarketApi(client)
portfolio_api = PortfolioApi(client)

# Balance
try:
    bal = portfolio_api.get_balance()
    cents = getattr(bal, 'balance', 0) or 0
    lines.append(f"\n## Balance: ${cents/100:.2f}")
except Exception as e:
    lines.append(f"\n## Balance: ERROR — {e}")

# Positions
try:
    pos = portfolio_api.get_positions(limit=50)
    positions = getattr(pos, 'market_positions', []) or []
    if positions:
        lines.append("\n## Open positions:")
        for p in positions:
            qty  = getattr(p, 'position', 0) or 0
            side = "YES" if qty > 0 else "NO"
            exp  = (getattr(p, 'market_exposure', 0) or 0) / 100
            lines.append(f"  {getattr(p, 'ticker', '')}  {side}  qty={abs(qty)}  exposure=${exp:.2f}")
    else:
        lines.append("\n## Open positions: none")
except Exception as e:
    lines.append(f"\n## Open positions: ERROR — {e}")

# Markets
try:
    resp = market_api.get_markets(status="open", limit=40)
    markets = getattr(resp, 'markets', []) or []
    lines.append(f"\n## Open markets ({len(markets)}):")
    lines.append(f"  {'Ticker':<36} {'Yes':>5} {'No':>5} {'Vol':>8}  Closes")
    lines.append("  " + "-"*65)
    for m in markets:
        yes   = getattr(m, 'yes_bid', None) or getattr(m, 'last_price', '?')
        no_p  = getattr(m, 'no_bid', '?')
        vol   = getattr(m, 'volume', 0) or 0
        close = str(getattr(m, 'close_time', ''))[:10]
        title = (getattr(m, 'title', '') or '')[:55]
        ticker = getattr(m, 'ticker', '')
        lines.append(f"  {ticker:<36} {str(yes):>4}¢ {str(no_p):>4}¢ {vol:>8,}  {close}")
        lines.append(f"    {title}")
except Exception as e:
    lines.append(f"\n## Markets: ERROR — {e}")

snapshot = "\n".join(lines)
if out:
    Path(out).write_text(snapshot)
    print(f"Saved to {out}")
else:
    print(snapshot)
