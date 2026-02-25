from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class MarketStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RESOLVED_YES = "RESOLVED_YES"
    RESOLVED_NO = "RESOLVED_NO"


class PriceTick(BaseModel):
    price: float
    timestamp: datetime
    source: str


class Market(BaseModel):
    id: str
    question: str
    strike: float
    expiry: datetime
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_mid: float
    no_mid: float
    status: MarketStatus = MarketStatus.ACTIVE
    liquidity_usd: float = 0.0
    simulated: bool = False


class SpreadOpportunity(BaseModel):
    market: Market
    cex_implied_yes: float
    pm_yes_price: float
    spread_pct: float
    side: Side
    entry_price: float


class Signal(BaseModel):
    opportunity: SpreadOpportunity
    confidence: float
    edge_pct: float
    kelly_fraction: float
    position_size_usd: float
    timestamp: datetime


class Position(BaseModel):
    id: str
    market_id: str
    market_question: str
    side: Side
    entry_price: float
    num_tokens: float
    cost_basis: float
    entry_time: datetime
    expiry: datetime
    current_price: float = 0.0
    unrealized_pnl: float = 0.0


class Trade(BaseModel):
    id: str
    market_id: str
    side: Side
    entry_price: float
    exit_price: float
    num_tokens: float
    cost_basis: float
    revenue: float
    pnl: float
    pnl_pct: float
    entry_time: datetime
    exit_time: datetime
    resolved: str
    won: bool


class PortfolioSnapshot(BaseModel):
    timestamp: datetime
    balance: float
    equity: float
    num_positions: int
    daily_pnl: float
    total_pnl: float
    total_trades: int
    win_rate: float
    avg_pnl_per_trade: float
    max_drawdown_pct: float
    sharpe_ratio: float
