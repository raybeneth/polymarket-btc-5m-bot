"""
Terminal dashboard — fixed panel UI with real-time stats and scrolling logs.

Uses `rich` library for rendering. The dashboard has three sections:
  1. Header: BTC price, market count, trade stats, P&L
  2. Positions: currently open positions with entry/stop/hold time
  3. Logs: scrolling log area (most recent N lines)

Color scheme: cyan primary, green/red for P&L only.
"""
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import STOP_LOSS_RATIO, MAX_DAILY_LOSS_USD, PAPER_TRADING


# ─────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────

_MAX_LOG_LINES = 100

# Style constants
_BORDER = "cyan"
_DIM = "bright_black"
_LABEL = "cyan"
_VALUE = "white"
_POS = "green" if True else "red"


class Dashboard:
    def __init__(self):
        self.console = Console()
        self._live: Optional[Live] = None
        self._start_time = time.time()

        # Live state (updated every cycle)
        self._btc_price: Optional[float] = None
        self._btc_connected: bool = False
        self._markets_count: int = 0
        self._trades: int = 0
        self._wins: int = 0
        self._losses: int = 0
        self._daily_pnl: float = 0.0
        self._positions: list = []
        self._logs: deque = deque(maxlen=_MAX_LOG_LINES)

        # Historical stats (loaded once from positions.log)
        self._hist_total_trades: int = 0
        self._hist_wins: int = 0
        self._hist_losses: int = 0
        self._hist_total_pnl: float = 0.0

    # ── Public API ──────────────────────────────

    def load_history(self, history: dict) -> None:
        """Load historical stats from positions.log parse result."""
        self._hist_total_trades = history.get("total_trades", 0)
        self._hist_wins = history.get("wins", 0)
        self._hist_losses = history.get("losses", 0)
        self._hist_total_pnl = history.get("total_pnl", 0.0)

        # Seed today's counters from history
        self._trades = history.get("today_trades", 0)
        self._wins = history.get("today_wins", 0)
        self._losses = history.get("today_losses", 0)
        self._daily_pnl = history.get("today_pnl", 0.0)

        # Log area only shows real-time events, no history seeding

    def start(self) -> None:
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=2,
            screen=True,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def update_stats(
        self,
        btc_price: Optional[float] = None,
        btc_connected: bool = False,
        markets_count: int = 0,
        trades: int = 0,
        wins: int = 0,
        losses: int = 0,
        daily_pnl: float = 0.0,
    ) -> None:
        self._btc_price = btc_price
        self._btc_connected = btc_connected
        self._markets_count = markets_count
        self._trades = trades
        self._wins = wins
        self._losses = losses
        self._daily_pnl = daily_pnl
        self._refresh()

    def update_positions(self, positions: list) -> None:
        self._positions = list(positions)
        self._refresh()

    def add_log(self, message: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self._logs.append(f"[{_DIM}]{now}[/]  {message}")
        self._refresh()

    # ── Rendering ───────────────────────────────

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._render())

    def _render(self) -> Panel:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=6),
            Layout(name="positions", size=5),
            Layout(name="logs"),
        )
        layout["header"].update(self._render_header())
        layout["positions"].update(self._render_positions())
        layout["logs"].update(self._render_logs())

        uptime = int(time.time() - self._start_time)
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        mode = "PAPER" if PAPER_TRADING else "LIVE"

        return Panel(
            layout,
            title=f"[bold cyan] Polymarket BTC 5min Bot [/]",
            subtitle=f"[{_DIM}]{mode} | uptime {h:02d}:{m:02d}:{s:02d}[/]",
            border_style=_BORDER,
            padding=(0, 1),
        )

    def _render_header(self) -> Table:
        table = Table.grid(padding=(0, 3), expand=True)
        table.add_column(ratio=1)
        table.add_column(ratio=1)
        table.add_column(ratio=1)

        # Row 1: BTC + Markets + Today P&L
        btc_str = f"${self._btc_price:,.2f}" if self._btc_price else "---"
        conn_dot = f"[green]●[/]" if self._btc_connected else f"[red]●[/]"
        pnl_color = "green" if self._daily_pnl >= 0 else "red"
        pnl_str = f"[{pnl_color}]{self._daily_pnl:+.2f}[/]"

        table.add_row(
            Text.from_markup(f"[{_LABEL}]BTC[/]  {conn_dot} [{_VALUE}]{btc_str}[/]"),
            Text.from_markup(f"[{_LABEL}]MARKETS[/]  [{_VALUE}]{self._markets_count}[/]"),
            Text.from_markup(f"[{_LABEL}]TODAY P&L[/]  {pnl_str} USD"),
        )

        # Row 2: Today W/L + Loss Limit
        wr = self._wins / (self._wins + self._losses) * 100 if (self._wins + self._losses) > 0 else 0
        loss_used = abs(min(0, self._daily_pnl))
        limit_str = f"[{_DIM}]{loss_used:.1f}/{MAX_DAILY_LOSS_USD:.0f}[/]"

        table.add_row(
            Text.from_markup(f"[{_LABEL}]TODAY[/]  [{_VALUE}]{self._trades}[/] [{_DIM}]trades[/]"),
            Text.from_markup(
                f"[{_LABEL}]W/L[/]  [green]{self._wins}[/]"
                f"[{_DIM}]/[/][red]{self._losses}[/]"
                f"  [{_DIM}]{wr:.0f}%[/]"
            ),
            Text.from_markup(f"[{_LABEL}]LOSS LIMIT[/]  {limit_str}"),
        )

        # Row 3: Historical cumulative stats (history already includes today,
        # but session may have new trades not yet in the log file — use the
        # larger of history vs live counters for today's portion)
        all_trades = self._hist_total_trades + max(0, self._trades - self._hist_total_trades)
        all_wins = self._hist_wins + max(0, self._wins - self._hist_wins)
        all_losses = self._hist_losses + max(0, self._losses - self._hist_losses)
        all_pnl = self._hist_total_pnl + (self._daily_pnl - self._hist_total_pnl) \
            if self._trades > 0 else self._hist_total_pnl
        all_wr = all_wins / all_trades * 100 if all_trades > 0 else 0
        all_pnl_color = "green" if all_pnl >= 0 else "red"

        table.add_row(
            Text.from_markup(
                f"[{_LABEL}]ALL TIME[/]  [{_VALUE}]{all_trades}[/] [{_DIM}]trades[/]"
            ),
            Text.from_markup(
                f"[{_DIM}]W/L[/]  [{_DIM}]{all_wins}/{all_losses} "
                f"{all_wr:.0f}%[/]"
            ),
            Text.from_markup(
                f"[{_DIM}]P&L[/]  [{all_pnl_color}]{all_pnl:+.2f}[/] [{_DIM}]USD[/]"
            ),
        )

        return table

    def _render_positions(self) -> Panel:
        if not self._positions:
            content = Text("  -- empty --", style=_DIM)
        else:
            lines = []
            now = time.time()
            for pos in self._positions:
                side_icon = "[green]▲[/]" if pos.side == "YES" else "[red]▼[/]"
                side_color = "green" if pos.side == "YES" else "red"
                hold = int(now - pos.opened_at)
                stop_price = pos.entry_price * (1 - STOP_LOSS_RATIO)
                remaining = max(0, int(pos.market_end_ts - now)) if pos.market_end_ts > 0 else "?"

                line = (
                    f"  {side_icon} [{side_color}]{pos.side:3s}[/]  "
                    f"[{_VALUE}]@ {pos.entry_price:.3f}[/]  "
                    f"[{_DIM}]qty={pos.shares:.2f}  "
                    f"hold={hold}s  "
                    f"stop={stop_price:.3f}  "
                    f"exp={remaining}s  "
                    f"{pos.order_id}[/]"
                )
                lines.append(line)
            content = Text.from_markup("\n".join(lines))

        return Panel(
            content,
            title=f"[{_LABEL}] POSITIONS [/]",
            title_align="left",
            border_style=_DIM,
            height=5,
            padding=(0, 0),
        )

    def _render_logs(self) -> Panel:
        # Calculate how many lines the log panel can fit:
        # terminal height - header(6) - positions(5) - outer panel border(2) - log panel border(2)
        term_h = self.console.height
        available = max(3, term_h - 6 - 5 - 2 - 2)

        if not self._logs:
            content = Text("  waiting for events...", style=_DIM)
        else:
            # Only take the last N lines that fit
            visible = list(self._logs)[-available:]
            content = Text.from_markup("\n".join(visible))

        return Panel(
            content,
            title=f"[{_LABEL}] LOG [/]",
            title_align="left",
            border_style=_DIM,
            padding=(0, 0),
        )


# ─────────────────────────────────────────────
# Logging Handler
# ─────────────────────────────────────────────

class DashboardLogHandler(logging.Handler):
    """Routes log records to the dashboard's log panel."""

    def __init__(self, dashboard: Dashboard):
        super().__init__()
        self._dashboard = dashboard

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            # Strip leading whitespace that was added for old console formatting
            msg = msg.strip()
            if not msg:
                return

            # Color by level
            if record.levelno >= logging.ERROR:
                msg = f"[red]{msg}[/]"
            elif record.levelno >= logging.WARNING:
                msg = f"[yellow]{msg}[/]"

            self._dashboard.add_log(msg)
        except Exception:
            pass  # never let logging errors crash the bot
