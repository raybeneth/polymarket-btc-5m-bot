"""
BTC spot price feed via exchange WebSocket.

Maintains a rolling time-series of (timestamp, price) in memory.
Other modules call `get_prices_since(t)` or `get_latest()` — no polling needed.

Source is switchable via config.BTC_PRICE_SOURCE ("binance" | "okx") since
Binance's WS can be geo-blocked (HTTP 451) depending on the server's region —
OKX is kept as a drop-in alternative with the same (timestamp, price) output.
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

from config import BTC_PRICE_SOURCE, PROXY_ENABLED

log = logging.getLogger(__name__)

# ── Binance: raw trade stream, one JSON object per trade
_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# ── OKX: public WS requires a subscribe frame after connecting; trades
# arrive batched in msg["data"]
_OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
_OKX_SUBSCRIBE_MSG = json.dumps({
    "op": "subscribe",
    "args": [{"channel": "trades", "instId": "BTC-USDT"}],
})


def _parse_binance(msg: dict) -> list[tuple[float, float]]:
    """aggTrade message -> [(timestamp, price)]."""
    if "p" not in msg or "T" not in msg:
        return []
    return [(msg["T"] / 1000.0, float(msg["p"]))]


def _parse_okx(msg: dict) -> list[tuple[float, float]]:
    """trades-channel message -> [(timestamp, price), ...] (empty for subscribe acks)."""
    out = []
    for trade in msg.get("data") or []:
        try:
            out.append((float(trade["ts"]) / 1000.0, float(trade["px"])))
        except (KeyError, ValueError):
            continue
    return out


# name -> (ws_url, subscribe_message_or_None, parse_fn, display_label)
_SOURCES = {
    "binance": (_BINANCE_WS_URL, None, _parse_binance, "币安"),
    "okx": (_OKX_WS_URL, _OKX_SUBSCRIBE_MSG, _parse_okx, "OKX"),
}

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


def get_chart_series(seconds: float = _MAX_HISTORY_SECONDS, bucket: float = 2.0) -> list[tuple[float, float]]:
    """
    Downsampled (timestamp, price) series for the dashboard chart — at most
    one point per `bucket` seconds (last price seen in that bucket), covering
    the last `seconds`. The raw feed can emit many ticks per second; shipping
    every one over the websocket on each broadcast would bloat the payload
    for no visual benefit at chart resolution.
    """
    cutoff = time.time() - seconds
    out: list[tuple[float, float]] = []
    last_bucket = None
    for t, p in _price_history:
        if t < cutoff:
            continue
        b = int(t // bucket)
        if b != last_bucket:
            out.append((t, p))
            last_bucket = b
        else:
            out[-1] = (t, p)
    return out


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


def get_price_at(target_ts: float, tolerance: float = 30.0) -> Optional[float]:
    """
    Return the price sample closest to absolute timestamp `target_ts`.

    Unlike `get_price_n_seconds_ago` (which is relative to now), this looks up
    an absolute point in time — needed for settling a market against its own
    open/close window (market_end_ts - 300 .. market_end_ts).

    Returns None if no sample lies within `tolerance` seconds of the target
    (e.g. the target predates our rolling buffer).
    """
    best: Optional[tuple[float, float]] = None
    best_dist = tolerance
    for t, p in _price_history:
        dist = abs(t - target_ts)
        if dist <= best_dist:
            best_dist = dist
            best = (t, p)
    return best[1] if best is not None else None


def is_connected() -> bool:
    return _connected


async def _run_feed() -> None:
    """
    Internal coroutine that maintains the exchange WebSocket connection
    (source selected by config.BTC_PRICE_SOURCE). Reconnects automatically
    on any error. Uses system proxy if available.
    """
    global _latest_price, _connected
    backoff = 1.0

    ws_url, subscribe_msg, parse_fn, label = _SOURCES.get(
        BTC_PRICE_SOURCE, _SOURCES["binance"]
    )
    if BTC_PRICE_SOURCE not in _SOURCES:
        log.warning(f"未知的 BTC_PRICE_SOURCE={BTC_PRICE_SOURCE!r}，回退到默认的币安数据源")

    # Detect system proxy, unless disabled via PROXY_ENABLED
    proxy_url = None
    if PROXY_ENABLED:
        proxies = urllib.request.getproxies()
        proxy_url = proxies.get("https") or proxies.get("http")

    while True:
        try:
            log.info(f"正在连接 {label} BTC/USDT 数据流 …")
            if proxy_url:
                proxy = Proxy.from_url(proxy_url)
                ws_ctx = proxy_connect(
                    ws_url,
                    proxy=proxy,
                    ping_interval=20,
                    ping_timeout=10,
                )
            else:
                ws_ctx = websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                )

            async with ws_ctx as ws:
                if subscribe_msg:
                    await ws.send(subscribe_msg)

                _connected = True
                backoff = 1.0
                log.info(f"{label} WebSocket 已连接。")

                async for raw in ws:
                    msg = json.loads(raw)
                    for ts, price in parse_fn(msg):
                        _latest_price = price
                        _price_history.append((ts, price))

                    # evict old samples
                    cutoff = time.time() - _MAX_HISTORY_SECONDS
                    while _price_history and _price_history[0][0] < cutoff:
                        _price_history.popleft()

        except Exception as exc:
            _connected = False
            log.warning(f"{label} WebSocket 错误: {exc}。将在 {backoff}s 后重连 …")
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
