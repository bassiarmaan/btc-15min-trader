"""FastAPI WebSocket dashboard — serves UI and streams live bot state."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from src.feeds.event_bus import EventBus

app = FastAPI(title="BTC 5-Min Arbitrage Bot")

_bus: EventBus | None = None
_live_mode = False

_state: dict = {
    "btc_price": 0.0,
    "btc_source": "",
    "btc_updated": "--",
    "balance": 10_000.0,
    "equity": 10_000.0,
    "initial_balance": 10_000.0,
    "total_pnl": 0.0,
    "daily_pnl": 0.0,
    "session_pnl": 0.0,
    "total_trades": 0,
    "win_rate": 0.0,
    "avg_pnl": 0.0,
    "sharpe": 0.0,
    "max_drawdown": 0.0,
    "num_positions": 0,
    "positions": [],
    "spreads": [],
    "trades": [],
    "pnl_history": [],
    "price_history": [],
    "strike": 0.0,
    "market_expiry": "",
    "market_id": "",
    "market_yes_bid": 0.0,
    "market_yes_ask": 0.0,
    "market_no_bid": 0.0,
    "market_no_ask": 0.0,
    "balance_source": "paper",
    "live_mode": _live_mode,
    "trades_note": "",
}

_kalshi_balance_data: dict | None = None


def init(event_bus: EventBus, live_mode: bool = False):
    global _bus, _live_mode
    _bus = event_bus
    _live_mode = bool(live_mode)
    _state["live_mode"] = _live_mode


@app.get("/")
async def root():
    html = (Path(__file__).parent / "index.html").read_text()
    return HTMLResponse(html)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(_state)
            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, Exception):
        pass


async def collect():
    while not _bus:
        await asyncio.sleep(0.1)

    q_btc = _bus.subscribe("btc_price", maxsize=1)
    q_snap = _bus.subscribe("portfolio_state", maxsize=1)
    q_pos = _bus.subscribe("positions_update", maxsize=1)
    q_trd = _bus.subscribe("trades_update", maxsize=1)
    q_spr = _bus.subscribe("spread_opportunities", maxsize=1)
    q_mkt = _bus.subscribe("pm_markets", maxsize=1)
    q_kalshi_bal = _bus.subscribe("kalshi_live_balance", maxsize=1)
    q_kalshi_pos = _bus.subscribe("kalshi_live_positions", maxsize=1)

    async def _btc():
        while True:
            t = await q_btc.get()
            _state["btc_price"] = t.price
            _state["btc_source"] = t.source
            _state["btc_updated"] = t.timestamp.strftime("%H:%M:%S UTC")
            _state["price_history"].append({"t": round(time.time(), 1), "p": round(t.price, 2)})
            if len(_state["price_history"]) > 1800:
                _state["price_history"] = _state["price_history"][-1800:]

    async def _kalshi_balance():
        global _kalshi_balance_data, _live_mode
        while True:
            data = await q_kalshi_bal.get()
            _kalshi_balance_data = data
            # Safety: if live balance stream exists, force dashboard into live mode.
            if not _live_mode:
                _live_mode = True
                _state["live_mode"] = True

    async def _snap():
        global _kalshi_balance_data
        while True:
            s = await q_snap.get()
            initial = getattr(s, "initial_balance", None) or 0.0
            _state["balance"] = round(s.balance, 2)
            _state["equity"] = round(s.equity, 2)
            _state["initial_balance"] = round(initial, 2)
            if _live_mode:
                # In live mode, always anchor session P&L to startup Kalshi balance
                # (captured in initial_balance at process start).
                _state["balance_source"] = "kalshi"
                if _kalshi_balance_data:
                    k_bal = round(_kalshi_balance_data["balance"], 2)
                    k_eq = round(_kalshi_balance_data["portfolio_value"], 2)
                    _state["balance"] = k_bal
                    _state["equity"] = k_eq
                base = _state["initial_balance"]
                # Session P&L in live mode = cash balance now - startup cash balance.
                _state["session_pnl"] = round(_state["balance"] - base, 2) if base > 0 else 0.0
            elif _kalshi_balance_data:
                k_bal = round(_kalshi_balance_data["balance"], 2)
                k_eq = round(_kalshi_balance_data["portfolio_value"], 2)
                _state["balance"] = k_bal
                _state["equity"] = k_eq
                _state["balance_source"] = "kalshi"
                _state["session_pnl"] = round(s.session_pnl, 2)
            else:
                _state["balance_source"] = "paper"
                _state["session_pnl"] = round(s.session_pnl, 2)
            _state["total_pnl"] = round(s.total_pnl, 2)
            _state["daily_pnl"] = round(s.daily_pnl, 2)
            if _live_mode:
                # Avoid showing paper-shadow performance stats as if they were live account results.
                _state["total_trades"] = 0
                _state["win_rate"] = 0.0
                _state["avg_pnl"] = 0.0
                _state["sharpe"] = 0.0
                _state["max_drawdown"] = 0.0
            else:
                _state["total_trades"] = s.total_trades
                _state["win_rate"] = round(s.win_rate, 1)
                _state["avg_pnl"] = round(s.avg_pnl_per_trade, 2)
                _state["sharpe"] = round(s.sharpe_ratio, 2)
                _state["max_drawdown"] = round(s.max_drawdown_pct, 2)
            _state["num_positions"] = s.num_positions
            _state["pnl_history"].append(
                {"t": round(time.time(), 1), "pnl": round(_state["session_pnl"], 2), "eq": round(_state["equity"], 2)}
            )
            if len(_state["pnl_history"]) > 600:
                _state["pnl_history"] = _state["pnl_history"][-600:]

    _kalshi_positions: list[dict] = []

    async def _kalshi_pos():
        nonlocal _kalshi_positions
        while True:
            positions = await q_kalshi_pos.get()
            _kalshi_positions = positions or []

    async def _pos():
        while True:
            positions = await q_pos.get()
            if _live_mode:
                _state["positions"] = [
                    {
                        "market": p["ticker"],
                        "side": p["side"],
                        "entry": "--",
                        "current": "--",
                        "pnl": round(p["market_value"], 2),
                        "cost": p["count"],
                        "expiry": "--",
                    }
                    for p in _kalshi_positions
                ]
            elif _kalshi_positions:
                _state["positions"] = [
                    {
                        "market": p["ticker"],
                        "side": p["side"],
                        "entry": "--",
                        "current": "--",
                        "pnl": round(p["market_value"], 2),
                        "cost": p["count"],
                        "expiry": "--",
                    }
                    for p in _kalshi_positions
                ]
            else:
                _state["positions"] = [
                    {
                        "market": p.market_question,
                        "side": p.side.value,
                        "entry": round(p.entry_price, 4),
                        "current": round(p.current_price, 4),
                        "pnl": round(p.unrealized_pnl, 2),
                        "cost": round(p.cost_basis, 2),
                        "expiry": p.expiry.strftime("%H:%M:%S"),
                    }
                    for p in positions
                ]

    async def _trades():
        while True:
            trades = await q_trd.get()
            if _live_mode:
                _state["trades"] = []
                _state["trades_note"] = "Live mode: dashboard paper trades are hidden. Use Kalshi account history for real fills and realized P&L."
            else:
                _state["trades_note"] = ""
                _state["trades"] = [
                    {
                        "time": t.exit_time.strftime("%H:%M:%S"),
                        "side": t.side.value,
                        "entry": round(t.entry_price, 4),
                        "exit": round(t.exit_price, 2),
                        "pnl": round(t.pnl, 2),
                        "pnl_pct": round(t.pnl_pct, 1),
                        "won": t.won,
                    }
                    for t in trades
                ]

    async def _spreads():
        while True:
            opps = await q_spr.get()
            _state["spreads"] = [
                {
                    "market": o.market.question,
                    "fair": round(o.cex_implied_yes, 4),
                    "pm": round(o.pm_yes_price, 4),
                    "spread": round(o.spread_pct, 1),
                    "side": o.side.value,
                }
                for o in (opps or [])[:8]
            ]

    async def _mkt():
        while True:
            mkts = await q_mkt.get()
            if mkts:
                m = mkts[0]
                _state["strike"] = m.strike
                _state["market_expiry"] = m.expiry.strftime("%H:%M:%S UTC")
                _state["market_id"] = m.id
                _state["market_yes_bid"] = m.yes_bid
                _state["market_yes_ask"] = m.yes_ask
                _state["market_no_bid"] = m.no_bid
                _state["market_no_ask"] = m.no_ask

    await asyncio.gather(_btc(), _snap(), _kalshi_pos(), _pos(), _trades(), _spreads(), _mkt(), _kalshi_balance())


async def serve(port: int = 8080):
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    await server.serve()
