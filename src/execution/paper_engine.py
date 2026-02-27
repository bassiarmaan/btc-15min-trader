from __future__ import annotations

import asyncio
import logging
import statistics
import uuid
from datetime import date, datetime, timezone

from config import Settings
from src.database import Database
from src.feeds.event_bus import EventBus
from src.models import (
    Market,
    MarketStatus,
    PortfolioSnapshot,
    Position,
    Side,
    Signal,
    Trade,
)

logger = logging.getLogger(__name__)


class PaperEngine:
    """Layer 4: Simulated execution engine for paper trading."""

    def __init__(self, event_bus: EventBus, database: Database, settings: Settings):
        self.bus = event_bus
        self.db = database
        self.s = settings

        self.balance: float = settings.initial_balance
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self.peak_balance: float = settings.initial_balance
        self._today: date = datetime.now(timezone.utc).date()

        self._sig_queue = event_bus.subscribe("approved_signal", maxsize=50)
        self._resolve_queue = event_bus.subscribe("market_resolved", maxsize=50)
        self._mkt_queue = event_bus.subscribe("pm_markets", maxsize=10)
        self._live_markets: dict[str, Market] = {}
        self._reversal_placed: set[str] = set()

    async def run(self):
        await asyncio.gather(
            self._process_signals(),
            self._process_resolutions(),
            self._track_markets(),
            self._publish_state(),
            self._snapshot_loop(),
        )

    # ------------------------------------------------------------------ #
    #  Market tracking — keep position current prices up-to-date          #
    # ------------------------------------------------------------------ #

    async def _track_markets(self):
        while True:
            mkts = await self._mkt_queue.get()
            for m in mkts:
                self._live_markets[m.id] = m
                if m.id not in self.positions:
                    continue
                pos = self.positions[m.id]
                mid = m.yes_mid if pos.side == Side.YES else m.no_mid
                pos.current_price = mid
                pos.unrealized_pnl = (mid - pos.entry_price) * pos.num_tokens

                if pos.cost_basis <= 0:
                    continue
                loss_ratio = abs(min(0, pos.unrealized_pnl)) / pos.cost_basis
                profit_ratio = max(0, pos.unrealized_pnl) / pos.cost_basis

                if (
                    self.s.early_exit_profit_pct > 0
                    and pos.unrealized_pnl > 0
                    and profit_ratio >= self.s.early_exit_profit_pct
                ):
                    self._early_exit(pos, m, resolved="EARLY_EXIT", won=True)
                    self.positions.pop(m.id, None)
                    await self.bus.publish(
                        "position_closed",
                        {"market_id": m.id, "side": pos.side, "count": max(1, int(round(pos.num_tokens))), "sell_price": pos.current_price},
                    )
                elif (
                    self.s.early_exit_loss_pct > 0
                    and pos.unrealized_pnl < 0
                    and loss_ratio >= self.s.early_exit_loss_pct
                ):
                    self._early_exit(pos, m, resolved="EARLY_EXIT_LOSS", won=False)
                    self.positions.pop(m.id, None)
                    await self.bus.publish(
                        "position_closed",
                        {"market_id": m.id, "side": pos.side, "count": max(1, int(round(pos.num_tokens))), "sell_price": pos.current_price},
                    )
                    if self.s.reversal_on_loss_cap and m.id not in self._reversal_placed:
                        logger.info("REVERSAL: attempting after cut-loss (market=%s)", m.id)
                        if self._place_reversal_bet(pos, m):
                            self._reversal_placed.add(m.id)

    def _early_exit(self, pos: Position, mkt: Market, resolved: str = "EARLY_EXIT", won: bool = True):
        """Close position at current (mid) price. Used for take-profit and cut-loss."""
        self._roll_day()
        exit_price = pos.current_price
        revenue = pos.num_tokens * exit_price
        pnl = revenue - pos.cost_basis
        pnl_pct = (pnl / pos.cost_basis * 100) if pos.cost_basis > 0 else 0

        self.balance += revenue
        self.daily_pnl += pnl
        self.total_pnl += pnl
        self.peak_balance = max(self.peak_balance, self.balance)

        trade = Trade(
            id=uuid.uuid4().hex[:8],
            market_id=mkt.id,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            num_tokens=pos.num_tokens,
            cost_basis=pos.cost_basis,
            revenue=revenue,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            resolved=resolved,
            won=won,
        )
        self.trades.append(trade)
        asyncio.get_event_loop().create_task(self.db.save_trade(trade))

        logger.info(
            "[%s] %s  PnL=$%+.2f (%+.1f%%)",
            resolved,
            pos.market_question,
            pnl,
            pnl_pct,
        )

    def _place_reversal_bet(self, closed_pos: Position, mkt: Market) -> bool:
        opp_side = Side.NO if closed_pos.side == Side.YES else Side.YES
        ask = mkt.no_ask if opp_side == Side.NO else mkt.yes_ask
        if ask >= 0.99:
            logger.warning("REVERSAL skipped: opposite side %s ask=%.2f (>= 0.99)", opp_side.value, ask)
            return False
        slippage = ask * (self.s.slippage_bps / 10_000)
        fill = ask + slippage
        if fill >= 0.99:
            return False
        cost = min(
            closed_pos.cost_basis * self.s.reversal_bet_fraction,
            self.balance * 0.95,
        )
        min_cost = getattr(self.s, "min_position_usd", 0.50)
        if cost <= 0:
            return False
        if cost < min_cost:
            cost = min(min_cost, self.balance * 0.95)
        tokens = cost / fill
        self.balance -= cost
        pos = Position(
            id=uuid.uuid4().hex[:8],
            market_id=mkt.id,
            market_question=mkt.question,
            side=opp_side,
            entry_price=fill,
            num_tokens=tokens,
            cost_basis=cost,
            entry_time=datetime.now(timezone.utc),
            expiry=mkt.expiry,
            current_price=fill,
        )
        self.positions[mkt.id] = pos
        logger.info("REVERSAL OPEN %s %s @ $%.4f x %.1f tokens = $%.2f", opp_side.value, mkt.question, fill, tokens, cost)
        return True

    # ------------------------------------------------------------------ #
    #  Trade execution                                                     #
    # ------------------------------------------------------------------ #

    async def _process_signals(self):
        while True:
            sig: Signal = await self._sig_queue.get()
            self._execute(sig)

    def _execute(self, sig: Signal):
        self._roll_day()
        opp = sig.opportunity
        entry = opp.entry_price

        slippage = entry * (self.s.slippage_bps / 10_000)
        fill = entry + slippage
        if fill >= 0.99:
            return

        cost = min(sig.position_size_usd, self.balance * 0.95)
        min_cost = getattr(self.s, "min_position_usd", 0.50)
        if cost < min_cost:
            return
        tokens = cost / fill

        self.balance -= cost
        pos = Position(
            id=uuid.uuid4().hex[:8],
            market_id=opp.market.id,
            market_question=opp.market.question,
            side=opp.side,
            entry_price=fill,
            num_tokens=tokens,
            cost_basis=cost,
            entry_time=datetime.now(timezone.utc),
            expiry=opp.market.expiry,
            current_price=fill,
        )
        self.positions[opp.market.id] = pos
        logger.info(
            "OPEN %s %s @ $%.4f x %.1f tokens = $%.2f",
            opp.side.value,
            opp.market.question,
            fill,
            tokens,
            cost,
        )

    # ------------------------------------------------------------------ #
    #  Resolution                                                          #
    # ------------------------------------------------------------------ #

    async def _process_resolutions(self):
        while True:
            mkt: Market = await self._resolve_queue.get()
            self._resolve(mkt)

    def _resolve(self, mkt: Market):
        self._reversal_placed.discard(mkt.id)
        if mkt.id not in self.positions:
            return

        self._roll_day()
        pos = self.positions.pop(mkt.id)
        resolved_yes = mkt.status == MarketStatus.RESOLVED_YES

        if pos.side == Side.YES:
            exit_price = 1.0 if resolved_yes else 0.0
            won = resolved_yes
        else:
            exit_price = 1.0 if not resolved_yes else 0.0
            won = not resolved_yes

        revenue = pos.num_tokens * exit_price
        pnl = revenue - pos.cost_basis
        pnl_pct = (pnl / pos.cost_basis * 100) if pos.cost_basis > 0 else 0

        self.balance += revenue
        self.daily_pnl += pnl
        self.total_pnl += pnl
        self.peak_balance = max(self.peak_balance, self.balance)

        trade = Trade(
            id=uuid.uuid4().hex[:8],
            market_id=mkt.id,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            num_tokens=pos.num_tokens,
            cost_basis=pos.cost_basis,
            revenue=revenue,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            resolved=mkt.status.value,
            won=won,
        )
        self.trades.append(trade)
        asyncio.get_event_loop().create_task(self.db.save_trade(trade))

        tag = "WIN" if won else "LOSS"
        logger.info(
            "[%s] %s  PnL=$%+.2f (%+.1f%%)",
            tag,
            pos.market_question,
            pnl,
            pnl_pct,
        )

    # ------------------------------------------------------------------ #
    #  Portfolio state                                                     #
    # ------------------------------------------------------------------ #

    def snapshot(self) -> PortfolioSnapshot:
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        equity = self.balance + unrealized
        n = len(self.trades)
        wins = sum(1 for t in self.trades if t.won)
        wr = (wins / n * 100) if n else 0
        avg = (self.total_pnl / n) if n else 0

        dd = 0.0
        if self.peak_balance > 0:
            dd = (self.peak_balance - min(self.balance, equity)) / self.peak_balance * 100

        sharpe = 0.0
        if n >= 3:
            pnls = [t.pnl for t in self.trades]
            mu = statistics.mean(pnls)
            sigma = statistics.stdev(pnls)
            if sigma > 0:
                # Annualized: 288 five-min intervals/day * 252 trading days
                sharpe = (mu / sigma) * (288 * 252) ** 0.5

        return PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            balance=self.balance,
            equity=equity,
            num_positions=len(self.positions),
            daily_pnl=self.daily_pnl,
            total_pnl=self.total_pnl,
            total_trades=n,
            win_rate=wr,
            avg_pnl_per_trade=avg,
            max_drawdown_pct=dd,
            sharpe_ratio=sharpe,
        )

    async def _publish_state(self):
        while True:
            snap = self.snapshot()
            await self.bus.publish("portfolio_state", snap)
            await self.bus.publish("positions_update", list(self.positions.values()))
            await self.bus.publish("trades_update", self.trades[-20:])
            await asyncio.sleep(0.5)

    async def _snapshot_loop(self):
        while True:
            await asyncio.sleep(60)
            await self.db.save_snapshot(self.snapshot())

    def _roll_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self._today:
            self._today = today
            self.daily_pnl = 0.0
