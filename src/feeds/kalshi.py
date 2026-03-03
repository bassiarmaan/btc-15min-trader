"""Live Kalshi KXBTC15M (15-min BTC up/down) market feed.

Polls the Kalshi REST API for active markets, publishes them through the
event bus in the same Market model the rest of the bot expects, and detects
resolutions when Kalshi settles.
"""
from __future__ import annotations

import asyncio
import logging
import re
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

    async def _poll(self, client: httpx.AsyncClient):
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

        # Always get exact strike from Kalshi: GET /markets/{ticker} has floor_strike; list often omits it
        for mkt in list(active_markets):
            full = await self._fetch_full_market(client, mkt.id)
            if full:
                mkt_full = self._convert(full)
                if mkt_full:
                    mkt.strike = mkt_full.strike
                    mkt.question = mkt_full.question
                    mkt.yes_bid = mkt_full.yes_bid
                    mkt.yes_ask = mkt_full.yes_ask
                    mkt.no_bid = mkt_full.no_bid
                    mkt.no_ask = mkt_full.no_ask
                    mkt.yes_mid = mkt_full.yes_mid
                    mkt.no_mid = mkt_full.no_mid
                    if mkt.strike <= 0:
                        await asyncio.sleep(0.5)
                        full_retry = await self._fetch_full_market(client, mkt.id)
                        if full_retry:
                            mkt_retry = self._convert(full_retry)
                            if mkt_retry and mkt_retry.strike > 0:
                                mkt.strike = mkt_retry.strike
                        if mkt.strike <= 0:
                            fallback = await self._fetch_strike(client, mkt.id)
                            if fallback > 0:
                                mkt.strike = fallback
                            if mkt.strike <= 0:
                                logger.warning(
                                    "Strike still 0 for %s (Kalshi did not return floor_strike)",
                                    mkt.id,
                                )
            else:
                if mkt.strike <= 0:
                    fallback = await self._fetch_strike(client, mkt.id)
                    if fallback > 0:
                        mkt.strike = fallback
                    if mkt.strike <= 0:
                        logger.warning("Could not fetch full market %s for strike", mkt.id)

        # Never publish or trade markets with strike=0 — only use Kalshi's exact strike for live
        active_markets = [m for m in active_markets if m.strike > 0]
        active_ids = {m.id for m in active_markets}

        if active_markets:
            await self.bus.publish("pm_markets", active_markets)
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

        just_closed = self._prev_active_ids - active_ids
        for mid in just_closed:
            await self._check_resolution(client, mid)

        now = datetime.now(timezone.utc)
        for mid, mkt in list(self.markets.items()):
            if mkt.status != MarketStatus.ACTIVE:
                continue
            if mid not in active_ids and now > mkt.expiry:
                await self._check_resolution(client, mid)

        self._prev_active_ids = active_ids

        cutoff = now - timedelta(minutes=30)
        self.markets = {
            k: v
            for k, v in self.markets.items()
            if v.status == MarketStatus.ACTIVE or v.expiry > cutoff
        }

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
            return
        if result == "yes":
            mkt.status = MarketStatus.RESOLVED_YES
            mkt.yes_mid, mkt.no_mid = 1.0, 0.0
        else:
            mkt.status = MarketStatus.RESOLVED_NO
            mkt.yes_mid, mkt.no_mid = 0.0, 1.0
        await self.bus.publish("market_resolved", mkt)
        logger.info("RESOLVED %s -> %s (strike=$%.2f)", mkt.id, mkt.status.value, mkt.strike)

    async def _fetch_full_market(self, client: httpx.AsyncClient, ticker: str) -> dict | None:
        """GET single market so we have Kalshi's exact floor_strike (list often omits it)."""
        try:
            resp = await client.get(f"{self.s.kalshi_api_base}/markets/{ticker}")
            resp.raise_for_status()
            return resp.json().get("market") or None
        except Exception as e:
            logger.debug("Full market fetch for %s: %s", ticker, e)
        return None

    def _parse_price(self, raw: dict, key_cents: str, key_dollars: str, default: float = 0) -> float:
        """Price can be yes_bid (0-100 cents) or yes_bid_dollars (e.g. '0.52')."""
        d = raw.get(key_dollars)
        if d is not None and d != "":
            try:
                return float(str(d).strip())
            except (ValueError, TypeError):
                pass
        c = raw.get(key_cents)
        if c is not None:
            try:
                return float(c) / 100.0
            except (ValueError, TypeError):
                pass
        return default

    async def _fetch_strike(self, client: httpx.AsyncClient, ticker: str) -> float:
        """Used by resolution check; prefer _fetch_full_market + _convert for exact strike."""
        full = await self._fetch_full_market(client, ticker)
        if not full:
            return 0.0
        strike = full.get("floor_strike")
        if strike is not None:
            s = float(strike)
            return s if s > 0 else 0.0
        ev = full.get("expiration_value")
        if ev and isinstance(ev, str):
            try:
                val = float(ev.replace(",", "").strip())
                if val > 10000:
                    return val
            except ValueError:
                pass
        for text in (
            full.get("rules_primary") or "",
            full.get("title") or "",
            full.get("subtitle") or "",
        ):
            for match in re.finditer(r"\$?([\d,]+(?:\.\d+)?)", text):
                val = float(match.group(1).replace(",", ""))
                if val > 10000:
                    return val
        return 0.0

    def _convert(self, raw: dict) -> Market | None:
        ticker = raw.get("ticker", "")
        close = raw.get("close_time")
        if not close or not ticker:
            return None
        yb = self._parse_price(raw, "yes_bid", "yes_bid_dollars", 0)
        ya = self._parse_price(raw, "yes_ask", "yes_ask_dollars", 0)
        nb = self._parse_price(raw, "no_bid", "no_bid_dollars", 0)
        na = self._parse_price(raw, "no_ask", "no_ask_dollars", 0)
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
