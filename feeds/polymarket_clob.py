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
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from config import (
    GAMMA_HOST,
    POLYMARKET_HOST,
    MARKET_KEYWORDS,
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
        +1 = all pressure on ask side (buyers dominant → bullish)
        -1 = all pressure on bid side (sellers dominant → bearish)
        """
        ask_vol = sum(lvl.size for lvl in self.asks[:levels])
        bid_vol = sum(lvl.size for lvl in self.bids[:levels])
        total = ask_vol + bid_vol
        if total == 0:
            return 0.0
        # More ask volume means buyers are aggressive → bullish signal
        return (ask_vol - bid_vol) / total


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
    Pull active BTC 5-min markets from Gamma API and refresh the cache.
    Gamma markets endpoint: GET /markets?active=true&closed=false&tag_slug=crypto
    """
    global _active_markets

    params = {
        "active": "true",
        "closed": "false",
        "tag_slug": "crypto",
        "limit": 100,
    }
    try:
        async with session.get(
            f"{GAMMA_HOST}/markets", params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:
        log.warning(f"Gamma market scan failed: {exc}")
        return

    found: dict[str, MarketInfo] = {}
    markets_list = data if isinstance(data, list) else data.get("markets", [])

    for m in markets_list:
        question = m.get("question", "") or ""
        # Must mention BTC and 5-minute
        q_lower = question.lower()
        if not any(k.lower() in q_lower for k in ["btc", "bitcoin"]):
            continue
        if not any(k.lower() in q_lower for k in ["5 min", "5min", "5-min"]):
            continue

        # Must have sufficient volume
        volume = float(m.get("volume", 0) or 0)
        if volume < MARKET_MIN_VOLUME_USD:
            continue

        # Extract token IDs (YES = index 0, NO = index 1 by convention)
        tokens = m.get("tokens", [])
        if len(tokens) < 2:
            continue

        yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), tokens[0])
        no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), tokens[1])

        end_ts_str = m.get("endDate") or m.get("end_date_iso") or ""
        if not end_ts_str:
            continue

        # Parse ISO timestamp
        from datetime import datetime, timezone
        try:
            end_dt = datetime.fromisoformat(end_ts_str.replace("Z", "+00:00"))
            end_ts = end_dt.timestamp()
        except Exception:
            continue

        # Skip expired or just-created markets (< 10s remaining or < 10s elapsed)
        remaining = end_ts - time.time()
        if remaining <= 10 or remaining >= 290:
            continue

        market_id = m.get("conditionId") or m.get("id", "")
        found[market_id] = MarketInfo(
            market_id=market_id,
            question=question,
            yes_token_id=yes_token.get("token_id", ""),
            no_token_id=no_token.get("token_id", ""),
            end_timestamp=end_ts,
            volume_usd=volume,
        )

    # Merge: keep existing entries that are still active, add new ones
    still_active = {k: v for k, v in _active_markets.items() if v.is_active}
    still_active.update(found)
    _active_markets = still_active
    log.debug(f"Market scan: {len(found)} new/updated, {len(_active_markets)} total active")


async def run_scanner(session: aiohttp.ClientSession) -> None:
    """Background task: refresh market list every SCANNER_POLL_INTERVAL seconds."""
    while True:
        await _fetch_active_markets(session)
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
    url = f"{POLYMARKET_HOST}/book"
    params = {"token_id": token_id}

    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as exc:
        log.debug(f"Order book fetch failed for {token_id[:12]}…: {exc}")
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
