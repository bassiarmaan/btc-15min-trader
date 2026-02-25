from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx

from config import Settings
from src.feeds.event_bus import EventBus
from src.models import Market, MarketStatus, PriceTick
from src.utils import price_binary_option

logger = logging.getLogger(__name__)


class PolymarketFeed:
    """Polymarket data feed with real API and simulation support.

    In simulation mode, synthetic 5-min binary markets are generated from
    real Binance prices with a configurable lag to model PM price discovery delay.
    """

    def __init__(self, event_bus: EventBus, settings: Settings):
        self.bus = event_bus
        self.settings = settings
        self.markets: dict[str, Market] = {}
        self._price_buffer: deque[PriceTick] = deque(maxlen=10_000)
        self._btc_queue = event_bus.subscribe("btc_price", maxsize=200)

    async def run(self):
        price_task = asyncio.create_task(self._buffer_prices())
        if self.settings.simulation_mode:
            feed_task = asyncio.create_task(self._run_simulation())
        else:
            feed_task = asyncio.create_task(self._run_real())
        await asyncio.gather(price_task, feed_task)

    # ------------------------------------------------------------------ #
    #  Price helpers                                                       #
    # ------------------------------------------------------------------ #

    async def _buffer_prices(self):
        while True:
            tick: PriceTick = await self._btc_queue.get()
            self._price_buffer.append(tick)

    def _current_price(self) -> float | None:
        return self._price_buffer[-1].price if self._price_buffer else None

    def _lagged_price(self) -> float | None:
        if not self._price_buffer:
            return None
        lag = timedelta(milliseconds=self.settings.sim_lag_ms)
        target = datetime.now(timezone.utc) - lag
        lagged = None
        for tick in reversed(self._price_buffer):
            if tick.timestamp <= target:
                lagged = tick.price
                break
        return lagged or self._current_price()

    # ------------------------------------------------------------------ #
    #  Simulation mode                                                     #
    # ------------------------------------------------------------------ #

    async def _run_simulation(self):
        logger.info("Polymarket feed running in SIMULATION mode")
        while not self._price_buffer:
            await asyncio.sleep(0.1)

        while True:
            self._generate_markets()
            expiry = self._next_expiry()

            while datetime.now(timezone.utc) < expiry:
                self._update_sim_prices()
                active = [
                    m for m in self.markets.values() if m.status == MarketStatus.ACTIVE
                ]
                if active:
                    await self.bus.publish("pm_markets", active)
                await asyncio.sleep(0.1)

            await self._resolve_markets()

    def _next_expiry(self) -> datetime:
        now = datetime.now(timezone.utc)
        interval = self.settings.sim_market_interval_s
        epoch_s = int(now.timestamp())
        boundary = ((epoch_s // interval) + 1) * interval
        return datetime.fromtimestamp(boundary, tz=timezone.utc)

    def _generate_markets(self):
        price = self._current_price()
        if not price:
            return
        expiry = self._next_expiry()
        spacing = self.settings.sim_strike_spacing
        n = self.settings.sim_num_strikes
        base = round(price / spacing) * spacing

        for i in range(-(n // 2), (n // 2) + 1):
            strike = base + i * spacing
            mid = f"sim_{int(strike)}_{int(expiry.timestamp())}"
            if mid in self.markets:
                continue

            lagged = self._lagged_price() or price
            yes_fair = price_binary_option(
                lagged,
                strike,
                (expiry - datetime.now(timezone.utc)).total_seconds(),
                self.settings.sim_btc_annual_vol,
            )
            spread = 0.02
            self.markets[mid] = Market(
                id=mid,
                question=f"BTC > ${strike:,.0f} at {expiry.strftime('%H:%M UTC')}?",
                strike=strike,
                expiry=expiry,
                yes_bid=max(0.01, yes_fair - spread / 2),
                yes_ask=min(0.99, yes_fair + spread / 2),
                no_bid=max(0.01, (1 - yes_fair) - spread / 2),
                no_ask=min(0.99, (1 - yes_fair) + spread / 2),
                yes_mid=yes_fair,
                no_mid=1 - yes_fair,
                liquidity_usd=50_000.0,
                simulated=True,
            )

        logger.info(
            "Generated %d sim markets (expiry %s)",
            n,
            expiry.strftime("%H:%M:%S UTC"),
        )

    def _update_sim_prices(self):
        lagged = self._lagged_price()
        if not lagged:
            return
        now = datetime.now(timezone.utc)
        spread = 0.02
        for m in self.markets.values():
            if m.status != MarketStatus.ACTIVE:
                continue
            remaining = (m.expiry - now).total_seconds()
            yes_fair = price_binary_option(
                lagged, m.strike, remaining, self.settings.sim_btc_annual_vol
            )
            m.yes_mid = yes_fair
            m.no_mid = 1 - yes_fair
            m.yes_bid = max(0.01, yes_fair - spread / 2)
            m.yes_ask = min(0.99, yes_fair + spread / 2)
            m.no_bid = max(0.01, (1 - yes_fair) - spread / 2)
            m.no_ask = min(0.99, (1 - yes_fair) + spread / 2)

    async def _resolve_markets(self):
        now = datetime.now(timezone.utc)
        price = self._current_price()
        if not price:
            return

        for m in list(self.markets.values()):
            if m.status != MarketStatus.ACTIVE or now < m.expiry:
                continue
            if price >= m.strike:
                m.status = MarketStatus.RESOLVED_YES
                m.yes_mid, m.no_mid = 1.0, 0.0
            else:
                m.status = MarketStatus.RESOLVED_NO
                m.yes_mid, m.no_mid = 0.0, 1.0
            await self.bus.publish("market_resolved", m)
            logger.info(
                "Resolved: %s -> %s (BTC=$%,.2f)", m.question, m.status.value, price
            )

        cutoff = now - timedelta(minutes=15)
        self.markets = {
            k: v
            for k, v in self.markets.items()
            if v.status == MarketStatus.ACTIVE or v.expiry > cutoff
        }

    # ------------------------------------------------------------------ #
    #  Real API mode                                                       #
    # ------------------------------------------------------------------ #

    async def _run_real(self):
        logger.info("Polymarket feed running in REAL API mode")
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                try:
                    await self._fetch_markets(client)
                except Exception as e:
                    logger.error("PM API error: %s", e)
                await asyncio.sleep(2)

    async def _fetch_markets(self, client: httpx.AsyncClient):
        resp = await client.get(
            f"{self.settings.pm_gamma_url}/markets",
            params={"tag": "crypto", "active": "true", "closed": "false"},
        )
        resp.raise_for_status()

        btc_markets: list[Market] = []
        for raw in resp.json():
            title = (raw.get("question") or "").lower()
            if "bitcoin" not in title and "btc" not in title:
                continue
            strike = self._extract_strike(title)
            if strike is None:
                continue

            tokens = raw.get("tokens", [])
            if len(tokens) < 2:
                continue
            yes_tok = next((t for t in tokens if t.get("outcome") == "Yes"), None)
            if not yes_tok:
                continue

            try:
                book = (
                    await client.get(
                        f"{self.settings.pm_clob_url}/book",
                        params={"token_id": yes_tok["token_id"]},
                    )
                ).json()
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                yes_bid = float(bids[0]["price"]) if bids else 0.01
                yes_ask = float(asks[0]["price"]) if asks else 0.99
            except Exception:
                yes_bid = float(raw.get("bestBid", 0.01))
                yes_ask = float(raw.get("bestAsk", 0.99))

            yes_mid = (yes_bid + yes_ask) / 2
            end = raw.get("endDate") or raw.get("end_date_iso")
            if not end:
                continue
            expiry = datetime.fromisoformat(end.replace("Z", "+00:00"))

            mkt = Market(
                id=raw.get("conditionId", raw.get("id", "")),
                question=raw.get("question", ""),
                strike=strike,
                expiry=expiry,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=max(0.01, 1 - yes_ask),
                no_ask=min(0.99, 1 - yes_bid),
                yes_mid=yes_mid,
                no_mid=1 - yes_mid,
                liquidity_usd=float(raw.get("liquidity", 0)),
                simulated=False,
            )
            btc_markets.append(mkt)
            self.markets[mkt.id] = mkt

        if btc_markets:
            await self.bus.publish("pm_markets", btc_markets)

    @staticmethod
    def _extract_strike(question: str) -> float | None:
        for pat in [
            r"above\s+\$?([\d,]+)",
            r"over\s+\$?([\d,]+)",
            r"below\s+\$?([\d,]+)",
            r"under\s+\$?([\d,]+)",
            r"\$?([\d,]+(?:\.\d+)?)",
        ]:
            m = re.search(pat, question)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    if 10_000 < val < 1_000_000:
                        return val
                except ValueError:
                    continue
        return None
