#!/usr/bin/env python3
"""
BTC 15-Min Arbitrage Bot — Kalshi / Polymarket vs CEX
=====================================================
Detects pricing lag between Kalshi KXBTC15M binary contracts and
real-time BTC spot from Coinbase, paper-trades the spread.

Run:  python main.py
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from config import Settings
from src.database import Database
from src.execution.paper_engine import PaperEngine
from src.feeds.binance import PriceFeed
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

    if settings.kalshi_mode:
        mode_label = "KALSHI (paper)" if not settings.kalshi_live_execution else "KALSHI LIVE (KXBTC15M)"
    else:
        mode_label = "POLYMARKET" if not settings.simulation_mode else "SIMULATION"

    port = int(os.getenv("PORT", str(settings.web_port)))

    log.info("=" * 64)
    log.info("BTC ARB BOT — starting")
    log.info("Mode        : %s", mode_label)

    startup_balance = settings.initial_balance
    if settings.kalshi_mode and settings.kalshi_live_execution:
        try:
            from src.execution.kalshi_live import fetch_kalshi_balance as _fetch_bal
            _bal_data = await _fetch_bal(settings)
            if _bal_data:
                startup_balance = _bal_data["balance"]
        except Exception:
            pass

    if startup_balance != settings.initial_balance:
        settings.initial_balance = startup_balance
        log.info("Balance     : $%.2f (Kalshi live)", startup_balance)
    else:
        log.info("Balance     : $%.2f", startup_balance)
    log.info("=" * 64)

    bus = EventBus()
    db = Database(settings.db_path)
    await db.initialize()

    price_feed = PriceFeed(bus)
    if settings.kalshi_mode:
        from src.feeds.kalshi import KalshiFeed
        market_feed = KalshiFeed(bus, settings)
    else:
        from src.feeds.polymarket import PolymarketFeed
        market_feed = PolymarketFeed(bus, settings)

    spreads = SpreadDetector(bus, settings.min_spread_pct, settings)
    signals = SignalGenerator(bus, settings)
    risk = RiskManager(bus, settings)
    engine = PaperEngine(bus, db, settings)

    kalshi_live = None
    fetch_kalshi_balance = None
    fetch_kalshi_positions = None
    if settings.kalshi_mode and settings.kalshi_live_execution:
        try:
            from src.execution.kalshi_live import KalshiLiveEngine, fetch_kalshi_balance, fetch_kalshi_positions
            kalshi_live = KalshiLiveEngine(bus, settings)
        except ImportError:
            log.warning("Kalshi live execution disabled: src.execution.kalshi_live not found")

    try:
        from src.dashboard.web import init as web_init, collect as web_collect, serve as web_serve
        web_init(bus)
        has_web = True
    except ImportError:
        has_web = False
        from src.dashboard.terminal import TerminalDashboard
        dashboard = TerminalDashboard(bus, simulation=not settings.kalshi_mode)

    async def sync_risk():
        while True:
            risk.sync(
                engine.balance, engine.positions, engine.daily_pnl, engine.total_pnl
            )
            await asyncio.sleep(0.5)

    async def publish_kalshi_balance():
        if not fetch_kalshi_balance:
            return
        interval = 5.0 if settings.kalshi_live_execution else 30.0
        while True:
            data = await fetch_kalshi_balance(settings)
            if data:
                await bus.publish("kalshi_live_balance", data)
            await asyncio.sleep(interval)

    async def publish_kalshi_positions():
        if not fetch_kalshi_positions:
            return
        while True:
            positions = await fetch_kalshi_positions(settings)
            if positions is not None:
                await bus.publish("kalshi_live_positions", positions)
            await asyncio.sleep(5.0)

    tasks = [
        price_feed.run(),
        market_feed.run(),
        spreads.run(),
        signals.run(),
        risk.run(),
        engine.run(),
        sync_risk(),
    ]
    if kalshi_live is not None:
        tasks.append(kalshi_live.run())
    if fetch_kalshi_balance is not None:
        tasks.append(publish_kalshi_balance())
    if fetch_kalshi_positions is not None:
        tasks.append(publish_kalshi_positions())
    if has_web:
        tasks.append(web_collect())
        tasks.append(web_serve(port))
        log.info("Dashboard   : http://localhost:%d", port)
    else:
        tasks.append(dashboard.run())

    log.info("All layers initialised — launching")
    if kalshi_live is not None:
        log.warning("LIVE Kalshi execution is ON — real orders will be placed")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("--- SHUTDOWN ---")
        if fetch_kalshi_balance:
            bal_data = await fetch_kalshi_balance(settings)
            if bal_data:
                k_eq = bal_data["portfolio_value"]
                k_bal = bal_data["balance"]
                session = k_eq - startup_balance
                log.info("Kalshi Bal : $%.2f", k_bal)
                log.info("Kalshi Eq  : $%.2f", k_eq)
                log.info("Session P&L: $%+.2f", session)
            else:
                snap = engine.snapshot()
                log.info("Balance    : $%.2f", snap.balance)
                log.info("Session P&L: $%+.2f", snap.session_pnl)
        else:
            snap = engine.snapshot()
            log.info("Balance    : $%.2f", snap.balance)
            log.info("Session P&L: $%+.2f", snap.session_pnl)
        log.info("Trades     : %d", engine.snapshot().total_trades)
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
