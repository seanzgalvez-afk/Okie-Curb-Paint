"""Test multiple signing formats against Kalshi API to find which one works."""
import os, time, base64, httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as ap

raw = os.environ["KALSHI_PRIVATE_KEY"].strip()
if not raw.startswith("-----"):
    raw = base64.b64decode(raw).decode()

key = serialization.load_pem_private_key(raw.encode(), password=None)
key_id = os.environ["KALSHI_API_KEY_ID"]
url = "https://trading-api.kalshi.com/trade-api/v2/exchange/status"

def req(label, path_sig, use_pss):
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
    print(f"{'PSS' if use_pss else 'v15'}  {path_sig:40s}  HTTP {r.status_code}  {r.text[:60]}")

req("1", "/trade-api/v2/exchange/status", False)
req("2", "/exchange/status",               False)
req("3", "/trade-api/v2/exchange/status",  True)
req("4", "/exchange/status",               True)
