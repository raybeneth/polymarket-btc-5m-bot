"""
Trade logger — appends trade records to a JSON-Lines file.
Each line is one JSON object (easy to load with pandas or jq).
"""
import json
import logging
import os
import time
from typing import Any

import colorlog

from config import TRADE_LOG_FILE, LOG_LEVEL


def setup_logging() -> None:
    """Configure coloured console logging and a plain file handler."""
    handler = colorlog.StreamHandler()
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
