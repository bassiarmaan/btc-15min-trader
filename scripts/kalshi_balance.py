#!/usr/bin/env python3
"""Print current Kalshi cash balance. Run from repo root: PYTHONPATH=. python scripts/kalshi_balance.py"""
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
    url = s.kalshi_api_base.rstrip("/") + "/portfolio/balance"
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sig = _sign_request(key, ts, "GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": s.kalshi_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 200:
        data = r.json()
        balance = data.get("balance", 0) / 100
        portfolio_value = data.get("portfolio_value", 0) / 100
        print(f"Balance:        ${balance:,.2f}")
        print(f"Portfolio val: ${portfolio_value:,.2f}")
    else:
        print(f"Error {r.status_code}: {r.text[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
