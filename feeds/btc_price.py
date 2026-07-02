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

import aiohttp
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

# ── 1-minute kline (candlestick) REST endpoints, one per source ──
# The dashboard renders an exchange-style 1m candlestick chart; the WS trade
# stream only gives us raw ticks, so we pull recent 1m klines over REST to fill
# the chart, then the frontend keeps the current minute's candle live from the
# WS spot price. Both exchanges accept limit up to (Binance 1000 / OKX 300);
# 180 candles = 3h is plenty to fill and pan the chart.
_KLINE_LIMIT = 180   # 1m × 180 = 3h of history; OKX request clamped to 300 below
_BINANCE_KLINE_URL = (
    f"https://api.binance.com/api/v3/klines"
    f"?symbol=BTCUSDT&interval=1m&limit={_KLINE_LIMIT}"
)
_OKX_KLINE_URL = (
    f"https://www.okx.com/api/v5/market/candles"
    f"?instId=BTC-USDT&bar=1m&limit={min(_KLINE_LIMIT, 300)}"
)


def _parse_binance_klines(data) -> list[dict]:
    """Binance /klines -> [{time, open, high, low, close}] (oldest first already)."""
    out = []
    for k in data or []:
        try:
            out.append({
                "time": int(k[0]) // 1000,          # open time, ms -> s
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
            })
        except (KeyError, IndexError, ValueError):
            continue
    return out


def _parse_okx_klines(data) -> list[dict]:
    """OKX /market/candles -> [{time, open, high, low, close}] (API returns newest first)."""
    rows = (data or {}).get("data") or []
    out = []
    for k in rows:
        try:
            out.append({
                "time": int(k[0]) // 1000,          # open time, ms -> s
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
            })
        except (KeyError, IndexError, ValueError):
            continue
    out.sort(key=lambda c: c["time"])               # oldest first for the chart
    return out


# name -> (kline_rest_url, parse_fn)
_KLINE_SOURCES = {
    "binance": (_BINANCE_KLINE_URL, _parse_binance_klines),
    "okx": (_OKX_KLINE_URL, _parse_okx_klines),
}

# Rolling buffer: (unix_timestamp_float, price_float)
# Keep 10 minutes of history so direction signals have enough lookback
_MAX_HISTORY_SECONDS = 600
_price_history: deque[tuple[float, float]] = deque()
_latest_price: Optional[float] = None
_connected: bool = False

# Recent 1-minute klines for the dashboard candlestick chart, oldest first.
# Refreshed periodically from the exchange REST API by _run_klines().
_klines: list[dict] = []


def get_klines() -> list[dict]:
    """Return a copy of the cached 1m klines (oldest first) for the dashboard."""
    return list(_klines)


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


async def _run_klines() -> None:
    """
    Periodically pull recent 1m klines from the exchange REST API into `_klines`.
    Fetch once on startup, then refresh every 60s so the most-recently-closed
    candle is corrected/added. Respects PROXY_ENABLED like the WS feed.

    Failures are non-fatal (dashboard just shows the live candle built from the
    WS spot price until klines arrive), so errors are logged at debug level.
    """
    global _klines

    url, parse_fn = _KLINE_SOURCES.get(
        BTC_PRICE_SOURCE, _KLINE_SOURCES["binance"]
    )

    # Klines follow PROXY_ENABLED just like the price feed and Polymarket layer:
    # when False we connect directly. Note the exchange REST kline endpoints
    # (api.binance.com / www.okx.com) can be geo-blocked even where the price
    # WebSocket (stream.binance.com) is reachable directly — so from a blocked
    # region the chart needs PROXY_ENABLED=true to fetch history.
    proxy_url = None
    if PROXY_ENABLED:
        proxies = urllib.request.getproxies()
        proxy_url = proxies.get("https") or proxies.get("http")

    timeout = aiohttp.ClientTimeout(total=10)
    warned = False
    while True:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, proxy=proxy_url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            klines = parse_fn(data)
            if not klines:
                raise ValueError("空数据")
            _klines = klines
            if warned:
                log.info(f"K线数据已恢复 ({BTC_PRICE_SOURCE}，共{len(klines)}根)")
                warned = False
        except Exception as exc:
            # First failure gets a visible warning (with a proxy hint) so an
            # empty chart is never an unexplained mystery; repeats stay at debug.
            if not warned:
                hint = "" if PROXY_ENABLED else " — PROXY_ENABLED=false 且该交易所REST可能被墙，如需K线请开启代理"
                log.warning(f"K线获取失败 ({BTC_PRICE_SOURCE}){hint}: {exc}")
                warned = True
            else:
                log.debug(f"K线获取重试失败 ({BTC_PRICE_SOURCE}): {exc}")
        # Once we have data, refresh slowly (just to correct the newest closed
        # candle). Until then, retry quickly so the chart backfills soon after
        # startup rather than waiting a full minute for the first history.
        await asyncio.sleep(60 if _klines else 5)


async def start_feed() -> None:
    """
    Launch the price feed as a background asyncio task.
    Call once at bot startup; it runs forever.
    """
    asyncio.create_task(_run_feed(), name="btc_price_feed")
    asyncio.create_task(_run_klines(), name="btc_klines")
    # Wait until first price arrives before returning
    for _ in range(100):
        if _latest_price is not None:
            log.info(f"已收到初始BTC价格: ${_latest_price:,.2f}")
            return
        await asyncio.sleep(0.1)
    log.warning("BTC价格数据流连接超时 — 继续运行。")
