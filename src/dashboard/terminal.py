from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.feeds.event_bus import EventBus
from src.models import (
    PortfolioSnapshot,
    Position,
    PriceTick,
    Side,
    SpreadOpportunity,
    Trade,
)


class TerminalDashboard:
    """Full-screen Rich terminal dashboard with live updates."""

    def __init__(self, event_bus: EventBus, simulation: bool = True):
        self.bus = event_bus
        self.console = Console()
        self._sim = simulation

        self._btc: float = 0.0
        self._btc_ts: str = "--"
        self._snap: PortfolioSnapshot | None = None
        self._positions: list[Position] = []
        self._trades: list[Trade] = []
        self._spreads: list[SpreadOpportunity] = []

        self._q_btc = event_bus.subscribe("btc_price", maxsize=1)
        self._q_snap = event_bus.subscribe("portfolio_state", maxsize=1)
        self._q_pos = event_bus.subscribe("positions_update", maxsize=1)
        self._q_trades = event_bus.subscribe("trades_update", maxsize=1)
        self._q_spreads = event_bus.subscribe("spread_opportunities", maxsize=1)

    async def run(self):
        await asyncio.gather(self._collect(), self._render_loop())

    async def _collect(self):
        async def _btc():
            while True:
                t: PriceTick = await self._q_btc.get()
                self._btc = t.price
                self._btc_ts = t.timestamp.strftime("%H:%M:%S UTC")

        async def _snap():
            while True:
                self._snap = await self._q_snap.get()

        async def _pos():
            while True:
                self._positions = await self._q_pos.get()

        async def _trd():
            while True:
                self._trades = await self._q_trades.get()

        async def _spr():
            while True:
                self._spreads = await self._q_spreads.get()

        await asyncio.gather(_btc(), _snap(), _pos(), _trd(), _spr())

    async def _render_loop(self):
        with Live(
            self._build(), console=self.console, refresh_per_second=4, screen=True
        ) as live:
            while True:
                live.update(self._build())
                await asyncio.sleep(0.25)

    # ------------------------------------------------------------------ #
    #  Layout builder                                                      #
    # ------------------------------------------------------------------ #

    def _build(self) -> Layout:
        root = Layout()
        root.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )

        # -- header --
        h = Text(justify="center")
        h.append("  BTC 5-MIN ARBITRAGE BOT  ", style="bold white on blue")
        h.append("  Paper Trading  ", style="bold white on dark_green")
        if self._snap:
            h.append(f"  Balance: ${self._snap.balance:,.2f}  ", style="bold white on dark_red")
        root["header"].update(Panel(h, style="bold"))

        # -- body --
        root["body"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=3),
        )
        root["left"].split_column(
            Layout(name="market_data", size=6),
            Layout(name="performance"),
        )
        root["right"].split_column(
            Layout(name="positions"),
            Layout(name="spreads", size=12),
            Layout(name="recent"),
        )

        root["market_data"].update(self._panel_market())
        root["performance"].update(self._panel_perf())
        root["positions"].update(self._panel_positions())
        root["spreads"].update(self._panel_spreads())
        root["recent"].update(self._panel_trades())

        # -- footer --
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        root["footer"].update(
            Panel(
                Text(f"  {now}  |  Ctrl+C to stop", style="dim", justify="center"),
                style="dim",
            )
        )
        return root

    # ------------------------------------------------------------------ #
    #  Individual panels                                                   #
    # ------------------------------------------------------------------ #

    def _panel_market(self) -> Panel:
        t = Table(show_header=False, box=None, padding=(0, 1))
        t.add_column("k", style="bold", min_width=14)
        t.add_column("v")
        t.add_row("BTC Spot", f"${self._btc:,.2f}" if self._btc else "connecting...")
        t.add_row("Updated", self._btc_ts)
        t.add_row("Mode", "SIMULATION" if self._sim else "LIVE API")
        return Panel(t, title="[cyan]Market Data[/]", border_style="cyan")

    def _panel_perf(self) -> Panel:
        t = Table(show_header=False, box=None, padding=(0, 1))
        t.add_column("Metric", style="bold", min_width=18)
        t.add_column("Value", min_width=14)
        if self._snap:
            s = self._snap
            _g, _r = "green", "red"
            t.add_row("Equity", f"${s.equity:,.2f}")
            t.add_row("Total Trades", str(s.total_trades))
            t.add_row("Win Rate", f"{s.win_rate:.1f}%")
            t.add_row(
                "Total P&L",
                Text(f"${s.total_pnl:+,.2f}", style=_g if s.total_pnl >= 0 else _r),
            )
            t.add_row(
                "Daily P&L",
                Text(f"${s.daily_pnl:+,.2f}", style=_g if s.daily_pnl >= 0 else _r),
            )
            t.add_row("Avg P&L/Trade", f"${s.avg_pnl_per_trade:+,.2f}")
            t.add_row("Sharpe (ann.)", f"{s.sharpe_ratio:.2f}")
            t.add_row("Max Drawdown", f"{s.max_drawdown_pct:.2f}%")
        else:
            t.add_row("Status", "Waiting for data...")
        return Panel(t, title="[green]Performance[/]", border_style="green")

    def _panel_positions(self) -> Panel:
        t = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=True)
        t.add_column("Market", style="bold", max_width=32, no_wrap=True)
        t.add_column("Side", justify="center", width=5)
        t.add_column("Entry", justify="right", width=8)
        t.add_column("Now", justify="right", width=8)
        t.add_column("P&L", justify="right", width=10)
        t.add_column("Expiry", justify="right", width=9)

        for p in self._positions:
            s = "green" if p.side == Side.YES else "red"
            ps = "green" if p.unrealized_pnl >= 0 else "red"
            t.add_row(
                p.market_question[:32],
                Text(p.side.value, style=s),
                f"${p.entry_price:.4f}",
                f"${p.current_price:.4f}",
                Text(f"${p.unrealized_pnl:+.2f}", style=ps),
                p.expiry.strftime("%H:%M:%S"),
            )

        n = len(self._positions)
        return Panel(
            t,
            title=f"[yellow]Positions ({n}/5)[/]",
            border_style="yellow",
        )

    def _panel_spreads(self) -> Panel:
        t = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=True)
        t.add_column("Market", style="bold", max_width=32, no_wrap=True)
        t.add_column("Fair", justify="right", width=7)
        t.add_column("PM", justify="right", width=7)
        t.add_column("Spread", justify="right", width=8)
        t.add_column("Signal", justify="center", width=10)

        for o in (self._spreads or [])[:6]:
            sig_style = "bold green" if o.spread_pct >= 5 else "yellow"
            t.add_row(
                o.market.question[:32],
                f"${o.cex_implied_yes:.4f}",
                f"${o.pm_yes_price:.4f}",
                Text(f"{o.spread_pct:.1f}%", style="bold cyan"),
                Text(f"BUY {o.side.value}", style=sig_style),
            )
        return Panel(t, title="[cyan]Spread Monitor[/]", border_style="cyan")

    def _panel_trades(self) -> Panel:
        t = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=True)
        t.add_column("Time", width=9)
        t.add_column("Side", justify="center", width=5)
        t.add_column("Entry", justify="right", width=8)
        t.add_column("Exit", justify="right", width=6)
        t.add_column("P&L", justify="right", width=10)
        t.add_column("Result", justify="center", width=6)

        for tr in reversed((self._trades or [])[-8:]):
            ps = "green" if tr.won else "red"
            t.add_row(
                tr.exit_time.strftime("%H:%M:%S"),
                tr.side.value,
                f"${tr.entry_price:.4f}",
                f"${tr.exit_price:.1f}",
                Text(f"${tr.pnl:+.2f}", style=ps),
                Text("WIN" if tr.won else "LOSS", style=f"bold {ps}"),
            )
        return Panel(t, title="[magenta]Recent Trades[/]", border_style="magenta")
