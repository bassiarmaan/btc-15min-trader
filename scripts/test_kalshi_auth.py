#!/usr/bin/env python3
"""Verify Kalshi API keys and auth by calling GET portfolio/balance.
Run from repo root: PYTHONPATH=. python scripts/test_kalshi_auth.py
"""
import asyncio
from datetime import datetime, timezone

import httpx

from config import Settings
from src.execution.kalshi_live import _load_private_key, _sign_request


async def main():
    s = Settings()
    if not s.kalshi_api_key_id or not s.kalshi_private_key:
        print("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY in .env")
        return
    try:
        key = _load_private_key(s.kalshi_private_key)
    except Exception as e:
        print(f"Invalid private key: {e}")
        return

    path = "/trade-api/v2/portfolio/balance"
    base = s.kalshi_api_base.rstrip("/")
    url = base + "/portfolio/balance"

    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sig = _sign_request(key, ts, "GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": s.kalshi_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    print(f"GET {url}")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 200:
        data = r.json()
        balance = data.get("balance", 0) / 100
        print(f"Auth OK. Balance: ${balance:.2f}")
    else:
        print(f"Auth failed: {r.status_code} {r.text[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
