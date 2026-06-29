"""
BTC spot price feed via Binance WebSocket.

Maintains a rolling time-series of (timestamp, price) in memory.
Other modules call `get_prices_since(t)` or `get_latest()` — no polling needed.
"""
import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

import websockets

log = logging.getLogger(__name__)

# ── Binance stream for BTC/USDT mark price (1s updates, no auth needed)
_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# Rolling buffer: (unix_timestamp_float, price_float)
# Keep 10 minutes of history so direction signals have enough lookback
_MAX_HISTORY_SECONDS = 600
_price_history: deque[tuple[float, float]] = deque()
_latest_price: Optional[float] = None
_connected: bool = False


def get_latest() -> Optional[float]:
    """Return the most recently received BTC price, or None if not yet connected."""
    return _latest_price


def get_prices_since(since_timestamp: float) -> list[tuple[float, float]]:
    """
    Return list of (timestamp, price) tuples newer than `since_timestamp`.
    Thread-safe via the GIL on list reads; asyncio loop handles writes.
    """
    return [(t, p) for t, p in _price_history if t >= since_timestamp]


def get_price_n_seconds_ago(seconds: int) -> Optional[float]:
    """
    Return the closest price sample from `seconds` ago.
    Used by direction filter for momentum calculation.
    """
    target = time.time() - seconds
    candidates = [(t, p) for t, p in _price_history if t <= target]
    if not candidates:
        return None
    # return the most recent sample that is still older than target
    return candidates[-1][1]


def is_connected() -> bool:
    return _connected


async def _run_feed() -> None:
    """
    Internal coroutine that maintains the Binance WebSocket connection.
    Reconnects automatically on any error.
    """
    global _latest_price, _connected
    backoff = 1.0

    while True:
        try:
            log.info("Connecting to Binance BTC/USDT stream …")
            async with websockets.connect(
                _BINANCE_WS_URL,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                _connected = True
                backoff = 1.0
                log.info("Binance WS connected.")

                async for raw in ws:
                    msg = json.loads(raw)
                    price = float(msg["p"])   # aggTrade price field
                    ts = msg["T"] / 1000.0    # trade timestamp in seconds

                    _latest_price = price
                    _price_history.append((ts, price))

                    # evict old samples
                    cutoff = time.time() - _MAX_HISTORY_SECONDS
                    while _price_history and _price_history[0][0] < cutoff:
                        _price_history.popleft()

        except Exception as exc:
            _connected = False
            log.warning(f"Binance WS error: {exc}. Reconnecting in {backoff}s …")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def start_feed() -> None:
    """
    Launch the price feed as a background asyncio task.
    Call once at bot startup; it runs forever.
    """
    asyncio.create_task(_run_feed(), name="btc_price_feed")
    # Wait until first price arrives before returning
    for _ in range(100):
        if _latest_price is not None:
            log.info(f"Initial BTC price received: ${_latest_price:,.2f}")
            return
        await asyncio.sleep(0.1)
    log.warning("BTC price feed took too long to connect — proceeding anyway.")
