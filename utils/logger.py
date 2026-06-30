"""
Trade logger — appends trade records to a JSON-Lines file.
Each line is one JSON object (easy to load with pandas or jq).

positions.log — human-readable position log with entry/exit and P&L.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import colorlog

from config import TRADE_LOG_FILE, LOG_LEVEL

POSITION_LOG_FILE = "positions.log"


def setup_logging() -> None:
    """Configure coloured console logging and a plain file handler."""
    import sys
    import io

    # Force UTF-8 output on Windows console
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    handler = colorlog.StreamHandler(stream=sys.stderr)
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    root.addHandler(handler)

    # File handler (plain, no colours)
    fh = logging.FileHandler("bot.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    ))
    root.addHandler(fh)


def log_trade(record: dict[str, Any]) -> None:
    """Append one trade record to the JSONL file."""
    record.setdefault("logged_at", time.time())
    with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def log_position_open(order_id: str, market_id: str, side: str,
                      entry_price: float, shares: float, score: float,
                      mode: str) -> None:
    """Log position open to positions.log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"[{now}] 开仓 | 模式={mode} | 订单={order_id} | "
        f"市场={market_id[:16]}… | 方向={side} | "
        f"价格={entry_price:.4f} | 数量={shares:.2f} | 评分={score:.1f}\n"
    )
    with open(POSITION_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def log_position_close(order_id: str, market_id: str, side: str,
                       entry_price: float, exit_price: float,
                       shares: float, pnl: float, result: str,
                       mode: str) -> None:
    """Log position close with P&L to positions.log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"[{now}] 平仓 | 模式={mode} | 订单={order_id} | "
        f"市场={market_id[:16]}… | 方向={side} | "
        f"入场={entry_price:.4f} | 出场={exit_price:.4f} | "
        f"数量={shares:.2f} | 盈亏={pnl:+.4f} USD | 结果={result}\n"
    )
    with open(POSITION_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def load_trades() -> list[dict]:
    """Read all trades from the log file. Returns empty list if file missing."""
    if not os.path.exists(TRADE_LOG_FILE):
        return []
    trades = []
    with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return trades
