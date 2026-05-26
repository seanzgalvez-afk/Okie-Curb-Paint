"""Test multiple signing formats against Kalshi API to find which one works."""
import os, sys, time, base64, traceback
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as ap

try:
    raw = os.environ["KALSHI_PRIVATE_KEY"].strip()
    if not raw.startswith("-----"):
        raw = base64.b64decode(raw).decode()
    key = serialization.load_pem_private_key(raw.encode(), password=None)
    print("✓ Private key loaded OK")
except Exception as e:
    print(f"✗ Failed to load private key: {e}")
    traceback.print_exc()
    sys.exit(1)

key_id = os.environ.get("KALSHI_API_KEY_ID", "")
print(f"  Key ID: {key_id}")
url = "https://trading-api.kalshi.com/trade-api/v2/exchange/status"

def req(label, path_sig, use_pss):
    try:
        ts = int(time.time() * 1000)
        msg = f"{ts}GET{path_sig}".encode()
        pad = (
            ap.PSS(mgf=ap.MGF1(hashes.SHA256()), salt_length=ap.PSS.DIGEST_LENGTH)
            if use_pss else ap.PKCS1v15()
        )
        sig = base64.b64encode(key.sign(msg, pad, hashes.SHA256())).decode()
        r = httpx.get(
            url,
            headers={
                "KALSHI-ACCESS-KEY": key_id,
                "KALSHI-ACCESS-TIMESTAMP": str(ts),
                "KALSHI-ACCESS-SIGNATURE": sig,
            },
            timeout=10,
        )
        print(f"{'PSS' if use_pss else 'v15'}  {path_sig:<40}  HTTP {r.status_code}  {r.text[:80]}")
    except Exception as e:
        print(f"{'PSS' if use_pss else 'v15'}  {path_sig:<40}  ERROR: {e}")

req("1", "/trade-api/v2/exchange/status", False)
req("2", "/exchange/status",               False)
req("3", "/trade-api/v2/exchange/status",  True)
req("4", "/exchange/status",               True)
