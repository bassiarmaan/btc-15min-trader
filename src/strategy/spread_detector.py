from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from src.feeds.event_bus import EventBus
from src.models import Market, MarketStatus, PriceTick, Side, SpreadOpportunity
from src.utils import price_binary_option

logger = logging.getLogger(__name__)


class SpreadDetector:
    """Layer 1: Continuously scans for CEX-vs-PM spread opportunities."""

    def __init__(self, event_bus: EventBus, min_spread_pct: float = 2.0):
        self.bus = event_bus
        self.min_spread_pct = min_spread_pct
        self._btc_queue = event_bus.subscribe("btc_price", maxsize=10)
        self._mkt_queue = event_bus.subscribe("pm_markets", maxsize=10)
        self._btc: float | None = None
        self._markets: list[Market] = []

    async def run(self):
        await asyncio.gather(
            self._consume_btc(),
            self._consume_markets(),
            self._scan_loop(),
        )

    async def _consume_btc(self):
        while True:
            tick: PriceTick = await self._btc_queue.get()
            self._btc = tick.price

    async def _consume_markets(self):
        while True:
            mkts = await self._mkt_queue.get()
            self._markets = [m for m in mkts if m.status == MarketStatus.ACTIVE]

    async def _scan_loop(self):
        while True:
            if self._btc and self._markets:
                await self._detect()
            await asyncio.sleep(0.05)

    async def _detect(self):
        btc = self._btc
        now = datetime.now(timezone.utc)
        opportunities: list[SpreadOpportunity] = []

        for mkt in self._markets:
            remaining = (mkt.expiry - now).total_seconds()
            if remaining <= 5:
                continue

            fair_yes = price_binary_option(btc, mkt.strike, remaining)

            # YES opportunity: fair value >> PM ask (PM is stale-low)
            yes_spread = (
                ((fair_yes - mkt.yes_ask) / fair_yes * 100) if fair_yes > 0.02 else 0
            )
            # NO opportunity: (1 - fair_yes) >> PM NO ask
            fair_no = 1 - fair_yes
            no_spread = (
                ((fair_no - mkt.no_ask) / fair_no * 100) if fair_no > 0.02 else 0
            )

            if yes_spread >= self.min_spread_pct:
                opportunities.append(
                    SpreadOpportunity(
                        market=mkt,
                        cex_implied_yes=fair_yes,
                        pm_yes_price=mkt.yes_ask,
                        spread_pct=yes_spread,
                        side=Side.YES,
                        entry_price=mkt.yes_ask,
                    )
                )
            elif no_spread >= self.min_spread_pct:
                opportunities.append(
                    SpreadOpportunity(
                        market=mkt,
                        cex_implied_yes=fair_yes,
                        pm_yes_price=mkt.yes_ask,
                        spread_pct=no_spread,
                        side=Side.NO,
                        entry_price=mkt.no_ask,
                    )
                )

        if opportunities:
            opportunities.sort(key=lambda o: o.spread_pct, reverse=True)
            await self.bus.publish("spread_opportunities", opportunities)
