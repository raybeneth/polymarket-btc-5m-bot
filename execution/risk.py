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

from config import (
    MAX_DAILY_LOSS_USD, MAX_CONCURRENT_POSITIONS, TRADE_SIZE_USD,
    CIRCUIT_BREAKER_CONSECUTIVE_LOSSES, CIRCUIT_BREAKER_PAUSE_SECONDS,
)
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
    market_end_ts: float = 0.0  # market resolution time (unix s); 0 = unknown (legacy)
    usd_size: float = 0.0  # capital committed at open — credited back (+ pnl) on close


@dataclass
class RiskState:
    open_positions: list[OpenPosition] = field(default_factory=list)
    daily_pnl: float = 0.0
    _day_key: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    balance: float = 0.0  # paper-mode virtual account balance; synced from/to MySQL by callers
    consecutive_losses: int = 0       # count of unbroken loss streak (reset on any win)
    circuit_breaker_until: float = 0.0  # unix ts; no new entries before this time

    def _refresh_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_key:
            log.info(f"新的一天 {today} — 重置日盈亏（前值 {self.daily_pnl:+.2f}）")
            self.daily_pnl = 0.0
            self._day_key = today
            self.trades_today = 0
            self.wins_today = 0
            self.losses_today = 0

    def can_trade(self, trade_size_usd: float = 0.0) -> tuple[bool, str]:
        self._refresh_day()
        if len(self.open_positions) >= MAX_CONCURRENT_POSITIONS:
            return False, f"已有 {len(self.open_positions)} 个持仓"
        if self.daily_pnl <= -MAX_DAILY_LOSS_USD:
            return False, f"日亏损上限已触发: {self.daily_pnl:.2f} USD"
        if trade_size_usd > 0 and self.balance < trade_size_usd:
            return False, f"余额不足: 剩余{self.balance:.2f} < 需要{trade_size_usd:.2f}"
        if self.circuit_breaker_until > 0 and time.time() < self.circuit_breaker_until:
            remaining_s = int(self.circuit_breaker_until - time.time())
            return False, f"熔断中: 连续{CIRCUIT_BREAKER_CONSECUTIVE_LOSSES}亏，{remaining_s}s 后恢复"
        return True, "OK"

    def record_open(self, position: OpenPosition) -> None:
        self.open_positions.append(position)
        self.trades_today += 1
        self.balance -= position.usd_size

    def record_close(self, market_id: str, exit_price: float) -> Optional[float]:
        """Remove position and return realised P&L (None if not found)."""
        for i, pos in enumerate(self.open_positions):
            if pos.market_id == market_id:
                pnl = (exit_price - pos.entry_price) * pos.shares
                self.daily_pnl += pnl
                self.balance += pos.usd_size + pnl
                self.open_positions.pop(i)
                if pnl > 0:
                    self.wins_today += 1
                    self.consecutive_losses = 0  # reset streak on any win
                else:
                    self.losses_today += 1
                    self.consecutive_losses += 1
                    if self.consecutive_losses >= CIRCUIT_BREAKER_CONSECUTIVE_LOSSES:
                        self.circuit_breaker_until = time.time() + CIRCUIT_BREAKER_PAUSE_SECONDS
                        log.warning(
                            f"熔断触发: 连续 {self.consecutive_losses} 笔亏损 — "
                            f"暂停入场 {CIRCUIT_BREAKER_PAUSE_SECONDS:.0f}s"
                        )
                log.info(
                    f"平仓 {pos.side} {market_id[:12]}… "
                    f"入场={pos.entry_price:.3f} 出场={exit_price:.3f} "
                    f"盈亏={pnl:+.4f} 日累计={self.daily_pnl:+.4f} 余额={self.balance:.2f}"
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
            f"日期 {self._day_key} | 交易={self.trades_today} "
            f"胜/负={w}/{l} ({wr:.0f}%) | 盈亏={self.daily_pnl:+.4f} USD"
        )

    def positions_summary(self) -> str:
        """Return a formatted string of all open positions."""
        if not self.open_positions:
            return "无持仓"
        lines = []
        for pos in self.open_positions:
            age = time.time() - pos.opened_at
            lines.append(
                f"  {pos.side:4s} {pos.market_id[:12]}… "
                f"入场={pos.entry_price:.3f} 数量={pos.shares:.2f} "
                f"持仓时间={age:.0f}s"
            )
        return "\n".join(lines)


# Singleton accessible across the bot
_state = RiskState()


def get_state() -> RiskState:
    return _state
