"""Live Kalshi KXBTC15M (15-min BTC up/down) market feed.

Polls the Kalshi REST API for active markets, publishes them through the
event bus in the same Market model the rest of the bot expects, and detects
resolutions when Kalshi settles.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from config import Settings
from src.feeds.event_bus import EventBus
from src.models import Market, MarketStatus

logger = logging.getLogger(__name__)


class KalshiFeed:
    def __init__(self, event_bus: EventBus, settings: Settings):
        self.bus = event_bus
        self.s = settings
        self.markets: dict[str, Market] = {}
        self._prev_active_ids: set[str] = set()

    async def run(self):
        logger.info(
            "Kalshi feed starting — series=%s, poll=%.1fs",
            self.s.kalshi_series,
            self.s.kalshi_poll_s,
        )
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                try:
                    await self._poll(client)
                except Exception as e:
                    logger.error("Kalshi poll error: %s", e)
                await asyncio.sleep(self.s.kalshi_poll_s)

    # ------------------------------------------------------------------ #
    #  Main poll cycle                                                     #
    # ------------------------------------------------------------------ #

    async def _poll(self, client: httpx.AsyncClient):
        # 1. Fetch currently open markets
        resp = await client.get(
            f"{self.s.kalshi_api_base}/markets",
            params={
                "series_ticker": self.s.kalshi_series,
                "limit": 20,
                "status": "open",
            },
        )
        resp.raise_for_status()
        raw_active = resp.json().get("markets", [])

        active_ids: set[str] = set()
        active_markets: list[Market] = []

        for raw in raw_active:
            mkt = self._convert(raw)
            if mkt is None:
                continue
            self.markets[mkt.id] = mkt
            active_ids.add(mkt.id)
            active_markets.append(mkt)

        if active_markets:
            await self.bus.publish("pm_markets", active_markets)
            # Log only when a new market appears
            new = active_ids - self._prev_active_ids
            for nid in new:
                m = self.markets[nid]
                logger.info(
                    "NEW MARKET: %s  strike=$%.2f  close=%s  bid/ask=%.0f/%.0f",
                    m.id,
                    m.strike,
                    m.expiry.strftime("%H:%M:%S UTC"),
                    m.yes_bid * 100,
                    m.yes_ask * 100,
                )

        # 2. Check for resolutions — markets that were active last tick
        #    but are no longer in the active set
        just_closed = self._prev_active_ids - active_ids
        for mid in just_closed:
            await self._check_resolution(client, mid)

        # 3. Also re-check any unresolved markets past their close time
        now = datetime.now(timezone.utc)
        for mid, mkt in list(self.markets.items()):
            if mkt.status != MarketStatus.ACTIVE:
                continue
            if mid not in active_ids and now > mkt.expiry:
                await self._check_resolution(client, mid)

        self._prev_active_ids = active_ids

        # 4. Cleanup old resolved markets
        cutoff = now - timedelta(minutes=30)
        self.markets = {
            k: v
            for k, v in self.markets.items()
            if v.status == MarketStatus.ACTIVE or v.expiry > cutoff
        }

    # ------------------------------------------------------------------ #
    #  Resolution                                                          #
    # ------------------------------------------------------------------ #

    async def _check_resolution(self, client: httpx.AsyncClient, mid: str):
        mkt = self.markets.get(mid)
        if not mkt or mkt.status != MarketStatus.ACTIVE:
            return

        try:
            resp = await client.get(f"{self.s.kalshi_api_base}/markets/{mid}")
            resp.raise_for_status()
            raw = resp.json().get("market", {})
        except Exception as e:
            logger.debug("Resolution check failed for %s: %s", mid, e)
            return

        result = raw.get("result", "")
        if not result:
            # Not settled yet — check again next cycle
            return

        if result == "yes":
            mkt.status = MarketStatus.RESOLVED_YES
            mkt.yes_mid, mkt.no_mid = 1.0, 0.0
        else:
            mkt.status = MarketStatus.RESOLVED_NO
            mkt.yes_mid, mkt.no_mid = 0.0, 1.0

        await self.bus.publish("market_resolved", mkt)
        logger.info(
            "RESOLVED %s -> %s (strike=$%.2f)",
            mkt.id,
            mkt.status.value,
            mkt.strike,
        )

    # ------------------------------------------------------------------ #
    #  Conversion                                                          #
    # ------------------------------------------------------------------ #

    def _convert(self, raw: dict) -> Market | None:
        ticker = raw.get("ticker", "")
        close = raw.get("close_time")
        if not close or not ticker:
            return None

        # Kalshi prices are in cents (0-100) → convert to 0.00-1.00
        yb = raw.get("yes_bid", 0) / 100
        ya = raw.get("yes_ask", 0) / 100
        nb = raw.get("no_bid", 0) / 100
        na = raw.get("no_ask", 0) / 100

        # floor_strike = BTC price at window open
        strike = float(raw.get("floor_strike", 0) or 0)

        expiry = datetime.fromisoformat(close.replace("Z", "+00:00"))
        oi = float(raw.get("open_interest", 0) or 0)
        vol = float(raw.get("volume", 0) or 0)

        return Market(
            id=ticker,
            question=raw.get("title") or f"BTC up in 15 min? (>{strike:,.0f})",
            strike=strike,
            expiry=expiry,
            yes_bid=max(0.01, yb),
            yes_ask=min(0.99, ya) if ya > 0 else 0.99,
            no_bid=max(0.01, nb),
            no_ask=min(0.99, na) if na > 0 else 0.99,
            yes_mid=(yb + ya) / 2 if (yb + ya) > 0 else 0.50,
            no_mid=(nb + na) / 2 if (nb + na) > 0 else 0.50,
            liquidity_usd=oi + vol,
            simulated=False,
        )
