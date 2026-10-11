"""
Logging setup — coloured console output + plain bot.log file.

SQLite (see storage/db.py) is the source of truth for position records and
stats. trades.jsonl / positions.log below are a local backup only, written
alongside the DB writes, for manual inspection/debugging.
"""
import json
import logging
import logging.handlers
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import colorlog

from config import LOG_LEVEL, LOG_DEBUG_MODULES, TRADE_LOG_FILE

POSITION_LOG_FILE = "positions.log"
ERROR_LOG_FILE = "errors.log"

# Third-party loggers that log every request at INFO (httpx: one line per
# CLOB call) — they drowned out real problems in the console and dashboard.
_NOISY_LOGGERS = ("httpx", "httpcore", "websockets", "asyncio.selector_events")

CST = timezone(timedelta(hours=8))


def _cst_converter(secs: float) -> time.struct_time:
    """Render log timestamps in UTC+8 regardless of the host's system timezone."""
    return time.gmtime(secs + 8 * 3600)


def setup_logging() -> None:
    """Configure coloured console logging and a plain file handler."""
    import sys
    import io

    # Force UTF-8 output on Windows console
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    console_formatter = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    )
    console_formatter.converter = _cst_converter

    handler = colorlog.StreamHandler(stream=sys.stderr)
    handler.setFormatter(console_formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    root.addHandler(handler)

    # File handlers (plain, no colours). Rotating so a long run — or a
    # temporary LOG_DEBUG_MODULES session — can't fill the disk.
    file_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    )
    file_formatter.converter = _cst_converter
    fh = logging.handlers.RotatingFileHandler(
        "bot.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(file_formatter)
    root.addHandler(fh)

    # WARNING+ only, with full tracebacks — the one place to look for
    # problems without scrolling past routine INFO/DEBUG lines.
    eh = logging.handlers.RotatingFileHandler(
        ERROR_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    eh.setLevel(logging.WARNING)
    eh.setFormatter(file_formatter)
    root.addHandler(eh)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in LOG_DEBUG_MODULES:
        logging.getLogger(name).setLevel(logging.DEBUG)

    # Unhandled exceptions in background tasks (feeds, scanner) otherwise
    # surface only as a vague "Task exception was never retrieved" at GC time.
    def _excepthook(exc_type, exc, tb):
        logging.getLogger("unhandled").critical("未捕获异常", exc_info=(exc_type, exc, tb))
    sys.excepthook = _excepthook


def attach_web_logger() -> None:
    """Attach web log handler so log lines are pushed to the web dashboard."""
    from web.server import WebLogHandler
    handler = WebLogHandler()
    handler.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    logging.getLogger().addHandler(handler)


def log_trade(record: dict[str, Any]) -> None:
    """Append one trade record to the JSONL backup file."""
    record.setdefault("logged_at", time.time())
    with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def log_position_open(order_id: str, market_id: str, side: str,
                      entry_price: float, shares: float, score: float,
                      mode: str) -> None:
    """Log position open to positions.log (backup)."""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
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
    """Log position close with P&L to positions.log (backup)."""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"[{now}] 平仓 | 模式={mode} | 订单={order_id} | "
        f"市场={market_id[:16]}… | 方向={side} | "
        f"入场={entry_price:.4f} | 出场={exit_price:.4f} | "
        f"数量={shares:.2f} | 盈亏={pnl:+.4f} USD | 结果={result}\n"
    )
    with open(POSITION_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
