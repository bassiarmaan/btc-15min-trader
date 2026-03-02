from __future__ import annotations

import logging
import time
from collections import OrderedDict

from config import Settings
from src.feeds.event_bus import EventBus
from src.models import Position, Signal

logger = logging.getLogger(__name__)


class RiskManager:
    """Layer 3: Validates signals against portfolio risk limits before execution."""

    def __init__(self, event_bus: EventBus, settings: Settings):
        self.bus = event_bus
        self.s = settings
        self._sig_queue = event_bus.subscribe("trade_signal", maxsize=50)
        self._positions: dict[str, Position] = {}
        self._balance: float = settings.initial_balance
        self._peak_balance: float = settings.initial_balance
        self._daily_pnl: float = 0.0
        self._total_pnl: float = 0.0
        self._traded_market_ids: OrderedDict[str, None] = OrderedDict()
        self._market_first_signal_time: dict[str, float] = {}

    def sync(
        self,
        balance: float,
        positions: dict[str, Position],
        daily_pnl: float,
        total_pnl: float,
    ):
        self._balance = balance
        self._positions = positions
        self._daily_pnl = daily_pnl
        self._total_pnl = total_pnl
        self._peak_balance = max(self._peak_balance, balance)

    async def run(self):
        while True:
            sig: Signal = await self._sig_queue.get()
            approved = self._check(sig)
            if approved:
                await self.bus.publish("approved_signal", approved)
            else:
                logger.debug("REJECTED %s", sig.opportunity.market.question)

    def _check(self, sig: Signal) -> Signal | None:
        if self._daily_pnl <= self.s.daily_loss_limit:
            logger.warning("Risk REJECTED: daily loss limit hit (pnl=$%.2f)", self._daily_pnl)
            return None

        if len(self._positions) >= self.s.max_concurrent:
            logger.info("Risk REJECTED: max concurrent positions (%d)", self.s.max_concurrent)
            return None

        if self._peak_balance > 0:
            dd = (self._peak_balance - self._balance) / self._peak_balance
            if dd >= self.s.max_drawdown_pct:
                logger.warning("Risk REJECTED: max drawdown hit (%.1f%%)", dd * 100)
                return None

        mid = sig.opportunity.market.id
        if mid in self._positions or mid in self._traded_market_ids:
            logger.debug("Risk REJECTED: already have position or traded %s", mid)
            return None

        warmup_s = self.s.min_seconds_after_market_open
        if warmup_s > 0:
            now_mono = time.monotonic()
            if mid not in self._market_first_signal_time:
                self._market_first_signal_time[mid] = now_mono
                if len(self._market_first_signal_time) > 200:
                    cutoff = now_mono - max(warmup_s * 2, 300)
                    self._market_first_signal_time = {
                        k: v for k, v in self._market_first_signal_time.items() if v > cutoff
                    }
                logger.info(
                    "Risk DEFER: first signal for %s — wait %.0fs before first entry",
                    mid[:24] + "…" if len(mid) > 24 else mid,
                    warmup_s,
                )
                return None
            if (now_mono - self._market_first_signal_time[mid]) < warmup_s:
                logger.debug("Risk DEFER: market %s still in warmup (%.0fs left)", mid, warmup_s - (now_mono - self._market_first_signal_time[mid]))
                return None
            self._market_first_signal_time.pop(mid, None)

        max_size = self._balance * self.s.max_position_pct
        kelly_size = self._balance * sig.kelly_fraction
        size = min(kelly_size, max_size, self._balance * 0.20)
        min_size = self.s.min_position_usd
        if size < min_size:
            if size <= 0:
                logger.info(
                    "Risk REJECTED: size $%.2f < $%.2f min (kelly=$%.2f max_pct=$%.2f)",
                    size, min_size, kelly_size, max_size,
                )
                return None
            size = min(min_size, max_size, self._balance * 0.20)

        self._traded_market_ids[mid] = None
        if len(self._traded_market_ids) > 500:
            while len(self._traded_market_ids) > 200:
                self._traded_market_ids.popitem(last=False)

        approved = sig.model_copy()
        approved.position_size_usd = size
        logger.info(
            "APPROVED %s %s size=$%.2f",
            sig.opportunity.side.value,
            sig.opportunity.market.question,
            size,
        )
        return approved
