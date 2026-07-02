"""
Polymarket data layer.

Two responsibilities:
  1. Market scanner — periodically finds active BTC 5-min binary markets
     via the Gamma REST API and returns their token IDs.
  2. Order book fetcher — pulls real-time bid/ask depth from the CLOB REST API
     for a specific token.

No WebSocket subscriptions here (Polymarket CLOB has limited WS support for
order books); we poll the REST endpoint at ~2s intervals which is sufficient.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector

def _get_proxy_url() -> Optional[str]:
    """Read system proxy from environment (set by Windows IE/registry settings)."""
    import urllib.request
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("http")


_DEFAULT_HEADERS = {
    # Polymarket's Gamma/CLOB endpoints sit behind Cloudflare and return 403
    # to requests with no (or non-browser) User-Agent.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    # Don't advertise brotli — aiohttp can't decode "br" without the optional
    # brotli package installed, which otherwise raises ClientPayloadError.
    "Accept-Encoding": "gzip, deflate",
}


def create_session() -> aiohttp.ClientSession:
    """Create an aiohttp session that respects system proxy settings.

    Connection hardening for long-running bots on Windows:
      - enable_cleanup_closed=True: silently discard reset connections instead
        of raising ConnectionResetError inside ProactorEventLoop callbacks.
      - keepalive_timeout=15: close idle connections before the server does.
      - force_close=True on proxy connector: prevents stale tunnel reuse.
    """
    proxy_url = _get_proxy_url()
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    if proxy_url:
        connector = ProxyConnector.from_url(
            proxy_url,
            limit=20,
            enable_cleanup_closed=True,
            force_close=True,
        )
        return aiohttp.ClientSession(
            connector=connector, headers=_DEFAULT_HEADERS, timeout=timeout,
        )
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            limit=20,
            enable_cleanup_closed=True,
            keepalive_timeout=15,
        ),
        headers=_DEFAULT_HEADERS,
        timeout=timeout,
    )

from config import (
    GAMMA_HOST,
    POLYMARKET_HOST,
    MARKET_MIN_VOLUME_USD,
    SCANNER_POLL_INTERVAL,
    DEPTH_LEVELS_TO_CHECK,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class BookLevel:
    price: float
    size: float   # shares available at this price


@dataclass
class OrderBook:
    token_id: str
    fetched_at: float         # unix timestamp
    bids: list[BookLevel]     # sorted descending (best bid first)
    asks: list[BookLevel]     # sorted ascending  (best ask first)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    def ask_depth(self, within_price: float) -> float:
        """Total shares available on the ask side up to `within_price`."""
        return sum(lvl.size for lvl in self.asks if lvl.price <= within_price)

    def bid_depth(self, within_price: float) -> float:
        """Total shares available on the bid side down to `within_price`."""
        return sum(lvl.size for lvl in self.bids if lvl.price >= within_price)

    def imbalance(self, levels: int = 5) -> float:
        """
        Orderbook imbalance score in [-1, +1].

        Standard microstructure convention:
          bids = resting demand (support) → bid-heavy pushes price up → bullish
          asks = resting supply (overhang) → ask-heavy pushes price down → bearish

        +1 = all volume on bid side (demand dominant → bullish)
        -1 = all volume on ask side (supply dominant → bearish)
        """
        ask_vol = sum(lvl.size for lvl in self.asks[:levels])
        bid_vol = sum(lvl.size for lvl in self.bids[:levels])
        total = ask_vol + bid_vol
        if total == 0:
            return 0.0
        # More bid volume means demand outweighs supply → bullish signal
        return (bid_vol - ask_vol) / total


@dataclass
class MarketInfo:
    market_id: str            # Polymarket condition ID
    question: str             # e.g. "Will BTC be higher in 5 minutes?"
    yes_token_id: str
    no_token_id: str
    end_timestamp: float      # unix seconds when market resolves
    volume_usd: float

    @property
    def elapsed_seconds(self) -> float:
        remaining = self.end_timestamp - time.time()
        return max(0.0, 300.0 - remaining)  # assumes 300s total

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.end_timestamp - time.time())

    @property
    def is_active(self) -> bool:
        return self.remaining_seconds > 0


# ─────────────────────────────────────────────
# Market Scanner
# ─────────────────────────────────────────────

# Cache: condition_id → MarketInfo
_active_markets: dict[str, MarketInfo] = {}


def get_active_markets() -> dict[str, MarketInfo]:
    """Return a snapshot of currently cached active markets."""
    return dict(_active_markets)


async def _fetch_active_markets(session: aiohttp.ClientSession) -> None:
    """
    Find active BTC 5-min Up/Down markets from Gamma API.

    Markets use the slug pattern: btc-updown-5m-{window_start_unix}
    where window_start_unix is the Unix timestamp of the 5-min window start,
    always a multiple of 300.  endDate = window_start + 300s.

    We check the current window and a few surrounding ones so we catch markets
    that are inside the entry window (45–210 s elapsed).
    """
    global _active_markets
    from datetime import datetime, timezone
    import json as _json

    now = time.time()
    # Snap to the current 5-min boundary and check ±2 windows
    base = int(now // 300) * 300
    candidates = [base + offset * 300 for offset in (-1, 0, 1, 2)]

    found: dict[str, MarketInfo] = {}

    for window_ts in candidates:
        slug = f"btc-updown-5m-{window_ts}"
        try:
            async with session.get(
                f"{GAMMA_HOST}/markets",
                params={"slug": slug},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:
            log.debug(f"Gamma接口请求失败 {slug}: {exc}")
            continue

        markets_list = data if isinstance(data, list) else data.get("markets", [])
        if not markets_list:
            continue

        m = markets_list[0]

        if m.get("closed") or not m.get("active", True):
            continue

        volume = float(m.get("volume") or 0)
        if volume < MARKET_MIN_VOLUME_USD:
            pass  # low volume markets still usable for paper trading

        # Token IDs are in the JSON-encoded clobTokenIds field
        # outcomes = ["Up", "Down"]  →  index 0 = Up (≡ YES), index 1 = Down (≡ NO)
        try:
            token_ids = _json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            continue
        if len(token_ids) < 2:
            continue

        end_ts = window_ts + 300  # resolution time

        # Skip if not yet in a tradeable window or already expired
        remaining = end_ts - now
        elapsed   = now - window_ts
        if remaining <= 5 or elapsed < 0:
            continue

        market_id = m.get("conditionId") or m.get("id", "")
        question  = m.get("question", slug)

        found[market_id] = MarketInfo(
            market_id=market_id,
            question=question,
            yes_token_id=token_ids[0],   # "Up"   token
            no_token_id=token_ids[1],    # "Down" token
            end_timestamp=end_ts,
            volume_usd=volume,
        )

    # Merge: keep existing entries that are still active, add new ones
    still_active = {k: v for k, v in _active_markets.items() if v.is_active}
    still_active.update(found)
    _active_markets = still_active
    log.debug(f"市场扫描: {len(found)} 个新增/更新, {len(_active_markets)} 个活跃中")


async def run_scanner(session: aiohttp.ClientSession) -> None:
    """Background task: refresh market list every SCANNER_POLL_INTERVAL seconds."""
    while True:
        try:
            await _fetch_active_markets(session)
        except (ConnectionResetError, aiohttp.ClientError, OSError) as exc:
            log.warning(f"市场扫描网络异常，将在下轮重试: {exc}")
        await asyncio.sleep(SCANNER_POLL_INTERVAL)


# ─────────────────────────────────────────────
# Order Book Fetcher
# ─────────────────────────────────────────────

async def fetch_order_book(
    session: aiohttp.ClientSession,
    token_id: str,
) -> Optional[OrderBook]:
    """
    Fetch the current order book for a single token from Polymarket CLOB.
    Returns None on network error.

    CLOB endpoint: GET /book?token_id={token_id}
    Response shape:
      { "bids": [{"price": "0.55", "size": "120"}, ...],
        "asks": [{"price": "0.57", "size": "80"},  ...] }
    """
    if not token_id:
        log.warning("订单簿获取失败: token_id 为空(市场数据缺失)")
        return None

    url = f"{POLYMARKET_HOST}/book"
    params = {"token_id": token_id}

    # 1 initial attempt + 1 retry. Transient timeouts / 5xx are common when the
    # CLOB (behind Cloudflare) is slow or the proxy hiccups; a quick retry
    # usually succeeds. 4xx (bad/expired token) won't improve, so we stop early.
    data = None
    last_err = None
    for attempt in range(2):
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    break
                last_err = f"HTTP {resp.status}"
                if resp.status < 500:
                    break  # client error — retry won't help
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt == 0:
            await asyncio.sleep(0.4)  # brief backoff before the single retry

    if data is None:
        log.warning(f"订单簿获取失败 {token_id[:12]}… — {last_err}")
        return None

    def parse_levels(raw: list) -> list[BookLevel]:
        levels = []
        for entry in raw:
            try:
                levels.append(BookLevel(
                    price=float(entry["price"]),
                    size=float(entry["size"]),
                ))
            except (KeyError, ValueError):
                continue
        return levels

    raw_bids = data.get("bids", [])
    raw_asks = data.get("asks", [])

    bids = sorted(parse_levels(raw_bids), key=lambda x: x.price, reverse=True)
    asks = sorted(parse_levels(raw_asks), key=lambda x: x.price)

    # Trim to configured depth
    bids = bids[:DEPTH_LEVELS_TO_CHECK]
    asks = asks[:DEPTH_LEVELS_TO_CHECK]

    return OrderBook(
        token_id=token_id,
        fetched_at=time.time(),
        bids=bids,
        asks=asks,
    )


async def fetch_both_books(
    session: aiohttp.ClientSession,
    market: MarketInfo,
) -> tuple[Optional[OrderBook], Optional[OrderBook]]:
    """Fetch YES and NO order books concurrently."""
    yes_book, no_book = await asyncio.gather(
        fetch_order_book(session, market.yes_token_id),
        fetch_order_book(session, market.no_token_id),
        return_exceptions=False,
    )
    return yes_book, no_book
