"""
BTC spot price feed via Binance WebSocket.

Maintains a rolling time-series of (timestamp, price) in memory.
Other modules call `get_prices_since(t)` or `get_latest()` — no polling needed.
"""
import asyncio
import json
import logging
import time
import urllib.request
from collections import deque
from typing import Optional

import websockets
from websockets_proxy import Proxy, proxy_connect

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
    Reconnects automatically on any error. Uses system proxy if available.
    """
    global _latest_price, _connected
    backoff = 1.0

    # Detect system proxy
    proxies = urllib.request.getproxies()
    proxy_url = proxies.get("https") or proxies.get("http")

    while True:
        try:
            log.info("正在连接币安 BTC/USDT 数据流 …")
            if proxy_url:
                proxy = Proxy.from_url(proxy_url)
                ws_ctx = proxy_connect(
                    _BINANCE_WS_URL,
                    proxy=proxy,
                    ping_interval=20,
                    ping_timeout=10,
                )
            else:
                ws_ctx = websockets.connect(
                    _BINANCE_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                )

            async with ws_ctx as ws:
                _connected = True
                backoff = 1.0
                log.info("币安 WebSocket 已连接。")

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
            log.warning(f"币安 WebSocket 错误: {exc}。将在 {backoff}s 后重连 …")
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
            log.info(f"已收到初始BTC价格: ${_latest_price:,.2f}")
            return
        await asyncio.sleep(0.1)
    log.warning("BTC价格数据流连接超时 — 继续运行。")
