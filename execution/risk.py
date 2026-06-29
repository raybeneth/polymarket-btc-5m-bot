"""
Risk manager — enforces daily loss limits and prevents double-entry.

Tracks:
  - realised P&L for the current UTC day
  - currently open position (only 1 at a time)
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import MAX_DAILY_LOSS_USD, MAX_CONCURRENT_POSITIONS, TRADE_SIZE_USD
import logging

log = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    market_id: str
    token_id: str
    side: str            # "YES" or "NO"
    entry_price: float
    shares: float
    opened_at: float     # unix timestamp
    order_id: str


@dataclass
class RiskState:
    open_positions: list[OpenPosition] = field(default_factory=list)
    daily_pnl: float = 0.0
    _day_key: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0

    def _refresh_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_key:
            log.info(f"New day {today} — resetting daily P&L (was {self.daily_pnl:+.2f})")
            self.daily_pnl = 0.0
            self._day_key = today
            self.trades_today = 0
            self.wins_today = 0
            self.losses_today = 0

    def can_trade(self) -> tuple[bool, str]:
        self._refresh_day()
        if len(self.open_positions) >= MAX_CONCURRENT_POSITIONS:
            return False, f"Already have {len(self.open_positions)} open position(s)"
        if self.daily_pnl <= -MAX_DAILY_LOSS_USD:
            return False, f"Daily loss limit hit: {self.daily_pnl:.2f} USD"
        return True, "OK"

    def record_open(self, position: OpenPosition) -> None:
        self.open_positions.append(position)
        self.trades_today += 1

    def record_close(self, market_id: str, exit_price: float) -> Optional[float]:
        """Remove position and return realised P&L (None if not found)."""
        for i, pos in enumerate(self.open_positions):
            if pos.market_id == market_id:
                pnl = (exit_price - pos.entry_price) * pos.shares
                self.daily_pnl += pnl
                self.open_positions.pop(i)
                if pnl > 0:
                    self.wins_today += 1
                else:
                    self.losses_today += 1
                log.info(
                    f"Closed {pos.side} on {market_id[:12]}… "
                    f"entry={pos.entry_price:.3f} exit={exit_price:.3f} "
                    f"pnl={pnl:+.4f} daily_total={self.daily_pnl:+.4f}"
                )
                return pnl
        return None

    def position_for_market(self, market_id: str) -> Optional[OpenPosition]:
        return next((p for p in self.open_positions if p.market_id == market_id), None)

    def summary(self) -> str:
        w = self.wins_today
        l = self.losses_today
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        return (
            f"Day {self._day_key} | trades={self.trades_today} "
            f"W/L={w}/{l} ({wr:.0f}%) | PnL={self.daily_pnl:+.4f} USD"
        )


# Singleton accessible across the bot
_state = RiskState()


def get_state() -> RiskState:
    return _state
