#!/usr/bin/env python3
"""
BTC 15-Min Arbitrage Bot — Kalshi / Polymarket vs CEX
=====================================================
Detects pricing lag between Kalshi KXBTC15M binary contracts and
real-time BTC spot from Coinbase, paper-trades the spread.

Run:  python main.py          (dashboard at http://localhost:8080)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from config import Settings
from src.dashboard import web
from src.database import Database
from src.execution.paper_engine import PaperEngine
from src.feeds.binance import BinanceFeed
from src.feeds.event_bus import EventBus
from src.strategy.risk_manager import RiskManager
from src.strategy.signal_generator import SignalGenerator
from src.strategy.spread_detector import SpreadDetector


async def main():
    settings = Settings()

    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-28s | %(levelname)-5s | %(message)s",
        handlers=[
            logging.FileHandler("data/bot.log", mode="a"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    log = logging.getLogger("btc5min")

    # Pick market feed
    if settings.kalshi_mode:
        mode_label = "KALSHI LIVE (KXBTC15M)"
        from src.feeds.kalshi import KalshiFeed

        market_feed = KalshiFeed(EventBus(), settings)  # placeholder, rewired below
    elif settings.simulation_mode:
        mode_label = "SIMULATION"
    else:
        mode_label = "POLYMARKET LIVE"

    # Respect platform-provided PORT (e.g. Railway), fall back to config
    port = int(os.getenv("PORT", str(settings.web_port)))

    log.info("=" * 64)
    log.info("BTC ARB BOT — starting")
    log.info("Mode        : %s", mode_label)
    log.info("Balance     : $%.2f", settings.initial_balance)
    log.info("Dashboard   : http://localhost:%d", port)
    log.info("=" * 64)

    bus = EventBus()
    db = Database(settings.db_path)
    await db.initialize()

    # Layer 0 — Data ingestion
    binance = BinanceFeed(bus)

    if settings.kalshi_mode:
        from src.feeds.kalshi import KalshiFeed

        market_feed = KalshiFeed(bus, settings)
        sim = False
    elif settings.simulation_mode:
        from src.feeds.polymarket import PolymarketFeed

        market_feed = PolymarketFeed(bus, settings)
        sim = True
    else:
        from src.feeds.polymarket import PolymarketFeed

        market_feed = PolymarketFeed(bus, settings)
        sim = False

    # Layer 1+2 — Research & signal generation
    spreads = SpreadDetector(bus, settings.min_spread_pct)
    signals = SignalGenerator(bus, settings)

    # Layer 3 — Risk management
    risk = RiskManager(bus, settings)

    # Layer 4 — Paper execution
    engine = PaperEngine(bus, db, settings)

    # Web dashboard
    web.init(bus)

    async def sync_risk():
        while True:
            risk.sync(
                engine.balance, engine.positions, engine.daily_pnl, engine.total_pnl
            )
            await asyncio.sleep(0.5)

    log.info("All layers initialised — launching")

    try:
        await asyncio.gather(
            binance.run(),
            market_feed.run(),
            spreads.run(),
            signals.run(),
            risk.run(),
            engine.run(),
            sync_risk(),
            web.collect(),
            web.serve(port),
        )
    except asyncio.CancelledError:
        pass
    finally:
        snap = engine.snapshot()
        log.info("--- SHUTDOWN ---")
        log.info("Balance    : $%.2f", snap.balance)
        log.info("Total P&L  : $%.2f", snap.total_pnl)
        log.info("Trades     : %d (%.1f%% win)", snap.total_trades, snap.win_rate)
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
