"""
Trade logger — appends trade records to a JSON-Lines file.
Each line is one JSON object (easy to load with pandas or jq).

positions.log — human-readable position log with entry/exit and P&L.
"""
import json
import logging
import os
import re
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


def attach_web_logger() -> None:
    """Attach web log handler so log lines are pushed to the web dashboard."""
    from web.server import WebLogHandler
    handler = WebLogHandler()
    handler.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    logging.getLogger().addHandler(handler)


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


def load_position_history() -> dict:
    """
    Parse positions.log to compute historical stats.

    Returns dict with:
        total_trades, wins, losses, total_pnl,
        today_trades, today_wins, today_losses, today_pnl,
    """
    result = {
        "total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0,
        "today_trades": 0, "today_wins": 0, "today_losses": 0, "today_pnl": 0.0,
    }
    if not os.path.exists(POSITION_LOG_FILE):
        return result

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl_pattern = re.compile(r"盈亏=([+-]?\d+\.?\d*)\s*USD")
    result_pattern = re.compile(r"结果=(\S+)")

    with open(POSITION_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "平仓" not in line:
                continue

            pnl_match = pnl_pattern.search(line)
            result_match = result_pattern.search(line)
            if not pnl_match:
                continue

            pnl = float(pnl_match.group(1))
            res = result_match.group(1) if result_match else ""
            is_win = "WIN" in res

            result["total_trades"] += 1
            result["total_pnl"] += pnl
            if is_win:
                result["wins"] += 1
            else:
                result["losses"] += 1

            if today_str in line[:30]:
                result["today_trades"] += 1
                result["today_pnl"] += pnl
                if is_win:
                    result["today_wins"] += 1
                else:
                    result["today_losses"] += 1

    return result
