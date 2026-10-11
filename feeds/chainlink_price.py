"""
Chainlink BTC/USD price feed via Polymarket's Real-Time Data Socket (RTDS).

Polymarket's BTC 5-minute Up/Down markets resolve on Chainlink's BTC/USD
price — not on any exchange's BTCUSDT print — so this feed is the reference
for the window's start price ("price to beat"), paper-mode settlement, and the
fair-value model. The exchange feed (feeds/btc_price.py) stays the source for
short-horizon momentum, where its faster tick rate matters more.

Protocol (public, no auth):
  connect   wss://ws-live-data.polymarket.com
  subscribe {"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink",
             "type":"*","filters":"{\"symbol\":\"btc/usd\"}"}]}
  keepalive send the text "PING" every few seconds (server answers "PONG")
  messages  {"topic":"crypto_prices_chainlink","type":"update",
             "payload":{"symbol":"btc/usd","timestamp":<ms>,"value":<float>}}
            plus, right after subscribing, a snapshot whose payload carries
            "data":[{"timestamp":<ms>,"value":<float>}, ...].
The parser below accepts both shapes and ignores anything else, so a
format drift degrades to "no Chainlink data" (callers fall back) rather
than a crash.
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

from config import CHAINLINK_RTDS_URL, CHAINLINK_SYMBOL, PROXY_ENABLED

log = logging.getLogger(__name__)

_MAX_HISTORY_SECONDS = 900   # > one 5-min window + entry/settle slack
_PING_INTERVAL = 5.0

_price_history: deque[tuple[float, float]] = deque()
_latest_price: Optional[float] = None
_latest_ts: float = 0.0
_connected: bool = False


def _subscribe_msg() -> str:
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [{
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": json.dumps({"symbol": CHAINLINK_SYMBOL}),
        }],
    })


def _parse(msg) -> list[tuple[float, float]]:
    """RTDS message -> [(timestamp_s, price)], oldest first. Empty for acks/other topics."""
    if not isinstance(msg, dict):
        return []
    if msg.get("topic") not in (None, "crypto_prices_chainlink"):
        return []
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return []
    symbol = payload.get("symbol")
    if symbol and str(symbol).lower() != CHAINLINK_SYMBOL.lower():
        return []

    points = payload.get("data") if isinstance(payload.get("data"), list) else [payload]
    out = []
    for pt in points:
        try:
            ts = float(pt["timestamp"])
            price = float(pt["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts > 1e11:          # milliseconds -> seconds
            ts /= 1000.0
        if price > 0:
            out.append((ts, price))
    out.sort(key=lambda x: x[0])
    return out


def _ingest(points: list[tuple[float, float]]) -> None:
    global _latest_price, _latest_ts
    for ts, price in points:
        # Snapshot replays can overlap what we already hold — keep the
        # buffer strictly increasing in time.
        if _price_history and ts <= _price_history[-1][0]:
            continue
        _price_history.append((ts, price))
        if ts >= _latest_ts:
            _latest_ts, _latest_price = ts, price
    cutoff = time.time() - _MAX_HISTORY_SECONDS
    while _price_history and _price_history[0][0] < cutoff:
        _price_history.popleft()


# ── Public read API (same shape as feeds/btc_price.py) ───────────────────

def get_latest() -> Optional[float]:
    return _latest_price


def get_latest_age() -> float:
    """Seconds since the newest Chainlink sample (inf if none yet)."""
    return time.time() - _latest_ts if _latest_ts else float("inf")


def is_connected() -> bool:
    return _connected


def get_prices_since(since_timestamp: float) -> list[tuple[float, float]]:
    return [(t, p) for t, p in _price_history if t >= since_timestamp]


def get_price_at(target_ts: float, tolerance: float = 5.0) -> Optional[float]:
    """
    Price in force at `target_ts`: the last sample at or before it, provided
    it is no older than `tolerance` seconds. Using "last at-or-before" rather
    than "nearest" matches how an oracle snapshot at a boundary works — a
    sample published after the boundary must not decide the boundary price.
    """
    best: Optional[tuple[float, float]] = None
    for t, p in _price_history:
        if t > target_ts:
            break
        best = (t, p)
    if best is None or target_ts - best[0] > tolerance:
        return None
    return best[1]


# ── Feed task ─────────────────────────────────────────────────────────────

async def _keepalive(ws) -> None:
    while True:
        await asyncio.sleep(_PING_INTERVAL)
        await ws.send("PING")


async def _run_feed() -> None:
    global _connected
    backoff = 1.0
    proxy_url = None
    if PROXY_ENABLED:
        proxies = urllib.request.getproxies()
        proxy_url = proxies.get("https") or proxies.get("http")

    while True:
        ping_task = None
        try:
            log.info(f"正在连接 Chainlink {CHAINLINK_SYMBOL} 数据流 (Polymarket RTDS) …")
            if proxy_url:
                ws_ctx = proxy_connect(CHAINLINK_RTDS_URL, proxy=Proxy.from_url(proxy_url),
                                       ping_interval=20, ping_timeout=10)
            else:
                ws_ctx = websockets.connect(CHAINLINK_RTDS_URL, ping_interval=20, ping_timeout=10)

            async with ws_ctx as ws:
                await ws.send(_subscribe_msg())
                ping_task = asyncio.create_task(_keepalive(ws))
                _connected = True
                backoff = 1.0
                log.info("Chainlink RTDS 已连接。")
                first_price_logged = False

                async for raw in ws:
                    if not raw or raw in ("PONG", "PING"):
                        continue
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        log.debug(f"Chainlink RTDS 非JSON消息: {str(raw)[:120]!r}")
                        continue
                    points = _parse(msg)
                    if points:
                        _ingest(points)
                        if not first_price_logged:
                            log.info(f"已收到初始 Chainlink BTC/USD 价格: ${_latest_price:,.2f}")
                            first_price_logged = True
        except Exception as exc:
            _connected = False
            log.warning(f"Chainlink RTDS 错误: {exc}。将在 {backoff}s 后重连 …")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if ping_task:
                ping_task.cancel()
            _connected = False


async def start_feed() -> None:
    """Launch the Chainlink feed as a background task. Call once at startup."""
    asyncio.create_task(_run_feed(), name="chainlink_price_feed")
