from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from config import Settings
from src.feeds.event_bus import EventBus
from src.models import Signal, SpreadOpportunity
from src.utils import kelly_criterion

logger = logging.getLogger(__name__)

COOLDOWN_S = 5.0


class SignalGenerator:
    """Layer 2: Converts spread opportunities into validated, sized trade signals.

    Applies a per-market cooldown to avoid duplicate signal spam.
    """

    def __init__(self, event_bus: EventBus, settings: Settings):
        self.bus = event_bus
        self.settings = settings
        self._opp_queue = event_bus.subscribe("spread_opportunities", maxsize=50)
        self._cooldowns: dict[str, float] = {}

    async def run(self):
        while True:
            opps: list[SpreadOpportunity] = await self._opp_queue.get()
            for opp in opps:
                sig = self._evaluate(opp)
                if sig:
                    await self.bus.publish("trade_signal", sig)
                    # fair_yes = P(BTC > strike at expiry). We bet the side that's mispriced vs fair.
                    f = sig.opportunity.cex_implied_yes
                    if sig.opportunity.side.value == "NO":
                        # NO signal when CEX P(up) high = we're value-betting: market has NO cheap vs our fair NO (1 - P(up))
                        logger.info(
                            "SIGNAL NO %s @ $%.4f  edge=%.1f%% conf=%.0f%%  (CEX P(up)=%.0f%% → fair NO=%.0f%%, we buy NO cheap)",
                            sig.opportunity.market.question,
                            sig.opportunity.entry_price,
                            sig.edge_pct,
                            sig.confidence * 100,
                            f * 100,
                            (1 - f) * 100,
                        )
                    else:
                        logger.info(
                            "SIGNAL YES %s @ $%.4f  edge=%.1f%% conf=%.0f%%  (CEX P(up)=%.0f%%, we buy YES cheap)",
                            sig.opportunity.market.question,
                            sig.opportunity.entry_price,
                            sig.edge_pct,
                            sig.confidence * 100,
                            f * 100,
                        )

    def _estimated_execution_cost(self, entry_price: float) -> float:
        """Estimated round-trip execution cost in price units (paper/live agnostic fallback)."""
        if entry_price <= 0:
            return 1.0
        slip = entry_price * (self.settings.slippage_bps / 10_000)
        return slip * 2

    def _evaluate(self, opp: SpreadOpportunity) -> Signal | None:
        now = datetime.now(timezone.utc)
        remaining = (opp.market.expiry - now).total_seconds()
        if remaining <= 10:
            return None

        # Per-market cooldown to avoid duplicate signals
        now_ts = time.monotonic()
        mid = opp.market.id
        if mid in self._cooldowns and (now_ts - self._cooldowns[mid]) < COOLDOWN_S:
            return None

        min_entry = max(self.settings.min_entry_price, self.settings.hard_min_entry_price)
        if opp.entry_price < min_entry:
            return None
        if opp.spread_pct < self.settings.min_edge_pct:
            return None

        est_cost = self._estimated_execution_cost(opp.entry_price)
        est_cost_pct = (est_cost / opp.entry_price) * 100
        net_edge_pct = opp.spread_pct - est_cost_pct
        if net_edge_pct < self.settings.min_net_edge_pct:
            return None

        spread_conf = min(1.0, max(0.0, net_edge_pct) / 20.0)
        time_conf = min(1.0, remaining / 60.0)
        liq_conf = min(1.0, opp.market.liquidity_usd / 10_000)
        confidence = spread_conf * 0.50 + time_conf * 0.30 + liq_conf * 0.20
        if confidence < self.settings.min_confidence:
            return None

        payout_ratio = (1.0 / opp.entry_price) - 1 if opp.entry_price > 0 else 0
        win_prob = (
            opp.cex_implied_yes
            if opp.side.value == "YES"
            else (1 - opp.cex_implied_yes)
        )
        full_kelly = kelly_criterion(win_prob, payout_ratio)
        quarter_kelly = full_kelly * self.settings.kelly_fraction
        if self.settings.kelly_confidence_weight > 0:
            quarter_kelly *= confidence ** self.settings.kelly_confidence_weight
        quarter_kelly = min(quarter_kelly, self.settings.max_signal_kelly_fraction)
        if quarter_kelly <= 0:
            return None

        self._cooldowns[mid] = now_ts
        # Prune old cooldowns
        if len(self._cooldowns) > 200:
            cutoff = now_ts - COOLDOWN_S * 2
            self._cooldowns = {
                k: v for k, v in self._cooldowns.items() if v > cutoff
            }

        return Signal(
            opportunity=opp,
            confidence=confidence,
            edge_pct=net_edge_pct,
            kelly_fraction=quarter_kelly,
            position_size_usd=0,
            timestamp=now,
        )
