from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone

from config import Settings
from src.feeds.event_bus import EventBus
from src.models import Market, MarketStatus, PriceTick, Side, SpreadOpportunity
from src.utils import price_binary_option

logger = logging.getLogger(__name__)

# Keep ~5 min of 1-sec granularity for momentum (300 points)
BTC_HISTORY_MAXLEN = 300


class SpreadDetector:
    """Layer 1: Continuously scans for CEX-vs-PM spread opportunities."""

    def __init__(self, event_bus: EventBus, min_spread_pct: float = 2.0, settings: Settings | None = None):
        self.bus = event_bus
        self.min_spread_pct = min_spread_pct
        self.settings = settings or None
        self._btc_queue = event_bus.subscribe("btc_price", maxsize=10)
        self._mkt_queue = event_bus.subscribe("pm_markets", maxsize=10)
        self._btc: float | None = None
        self._markets: list[Market] = []
        self._btc_history: deque[tuple[float, float]] = deque(maxlen=BTC_HISTORY_MAXLEN)
        self._last_btc_update: float = 0.0
        self._last_market_update: float = 0.0
        self._last_stale_log: float = 0.0

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
            now = time.monotonic()
            self._btc_history.append((now, tick.price))
            self._last_btc_update = now

    async def _consume_markets(self):
        while True:
            mkts = await self._mkt_queue.get()
            self._markets = [m for m in mkts if m.status == MarketStatus.ACTIVE]
            self._last_market_update = time.monotonic()

    def _data_fresh(self) -> bool:
        if not self.settings:
            return True
        now = time.monotonic()
        btc_stale = self.settings.max_btc_stale_s > 0 and (
            (self._last_btc_update <= 0) or (now - self._last_btc_update > self.settings.max_btc_stale_s)
        )
        market_stale = self.settings.max_market_stale_s > 0 and (
            (self._last_market_update <= 0) or (now - self._last_market_update > self.settings.max_market_stale_s)
        )
        if btc_stale or market_stale:
            if now - self._last_stale_log >= 15:
                logger.warning(
                    "Spread scan paused: stale data (btc_age=%.1fs, market_age=%.1fs)",
                    now - self._last_btc_update if self._last_btc_update > 0 else -1.0,
                    now - self._last_market_update if self._last_market_update > 0 else -1.0,
                )
                self._last_stale_log = now
            return False
        return True

    async def _scan_loop(self):
        while True:
            if self._btc and self._markets and self._data_fresh():
                await self._detect()
            await asyncio.sleep(0.05)

    def _direction_alignment_ok(self, side: Side, btc: float, strike: float) -> bool:
        """Only YES when spot >= strike, only NO when spot <= strike (don't fight the trend)."""
        if not self.settings or not self.settings.require_directional_alignment:
            return True
        if side == Side.YES:
            return btc >= strike
        return btc <= strike

    def _momentum_ok(self, side: Side, btc: float) -> bool:
        """If momentum_lookback_seconds > 0: YES only when price trend up, NO only when down."""
        lookback = self.settings.momentum_lookback_seconds if self.settings else 0.0
        if lookback <= 0 or len(self._btc_history) < 2:
            return True
        now = time.monotonic()
        cutoff = now - lookback
        past_price: float | None = None
        for t, p in self._btc_history:
            if t <= cutoff:
                past_price = p
            else:
                break
        if past_price is None or past_price <= 0:
            return True
        # momentum up = btc > past_price, momentum down = btc < past_price
        if side == Side.YES:
            return btc >= past_price
        return btc <= past_price

    async def _detect(self):
        btc = self._btc
        now = datetime.now(timezone.utc)
        opportunities: list[SpreadOpportunity] = []

        vol = self.settings.btc_pricing_vol if self.settings else 0.70
        min_edge_prob = self.settings.min_edge_prob if self.settings else 0.03
        shrink = self.settings.fair_value_shrink if self.settings else 0.0

        for mkt in self._markets:
            remaining = (mkt.expiry - now).total_seconds()
            if remaining <= 5:
                continue

            fair_yes = price_binary_option(btc, mkt.strike, remaining, annual_vol=vol)
            if shrink > 0:
                fair_yes = 0.5 + (1.0 - shrink) * (fair_yes - 0.5)
            fair_no = 1.0 - fair_yes

            # Edge in probability (absolute); spread_pct for filtering/ordering
            yes_edge_prob = fair_yes - mkt.yes_ask
            no_edge_prob = fair_no - mkt.no_ask
            yes_spread = (
                (yes_edge_prob / fair_yes * 100) if fair_yes > 0.02 else 0
            )
            no_spread = (
                (no_edge_prob / fair_no * 100) if fair_no > 0.02 else 0
            )

            min_fair = self.settings.min_fair_probability if self.settings else 0.55
            yes_ok = (
                yes_spread >= self.min_spread_pct
                and yes_edge_prob >= min_edge_prob
                and fair_yes >= min_fair
                and self._direction_alignment_ok(Side.YES, btc, mkt.strike)
                and self._momentum_ok(Side.YES, btc)
            )
            no_ok = (
                no_spread >= self.min_spread_pct
                and no_edge_prob >= min_edge_prob
                and fair_no >= min_fair
                and self._direction_alignment_ok(Side.NO, btc, mkt.strike)
                and self._momentum_ok(Side.NO, btc)
            )

            # When both sides have edge + direction, take the one with the *larger* spread
            if yes_ok and no_ok:
                if yes_spread >= no_spread:
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
                else:
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
            elif yes_ok:
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
            elif no_ok:
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
