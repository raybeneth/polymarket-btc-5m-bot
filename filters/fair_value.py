"""
Fair value / edge — what the market is actually betting on.

A BTC 5m Up/Down market pays $1 per share to Up if Chainlink BTC/USD at the
window end is >= its value at the window start, else to Down. So the quantity
that decides the trade is where price sits relative to that start price, and
how much time is left for it to cross back — not recent momentum by itself.

Model (driftless lognormal over the remaining time):
    z     = ln(price_now / price_start) / (σ · √remaining)
    P(Up) = Φ(z)
σ is the per-√second volatility realised over the last FAIR_VALUE_VOL_LOOKBACK
seconds, floored so a quiet tape can't produce absurd certainty.

Edge for the side we'd buy = fair probability − ask − FEE_PER_SHARE. A share
bought at the ask settles at 1 or 0, so this is the expected profit per share.

Both prices come from the SAME source so the exchange/Chainlink basis can't
leak into z: Chainlink when it has both the window start and a fresh sample,
otherwise the exchange feed (flagged in details["source"]).
"""
import math
import time
from typing import Optional

from config import (
    FilterResult,
    FAIR_VALUE_VOL_LOOKBACK,
    FAIR_VALUE_VOL_FLOOR_PER_S,
    FAIR_VALUE_EDGE_FULL,
    FEE_PER_SHARE,
    MARKET_DURATION_SECONDS,
)
from feeds import btc_price, chainlink_price

_CHAINLINK_MAX_AGE = 10.0   # seconds; older than this → treat Chainlink as stale

# Window start price per (source, window_start) — fixed once observed, so the
# reference can't jitter as samples roll out of the buffers.
_start_cache: dict[tuple[str, int], float] = {}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realised_vol_per_s(prices: list[tuple[float, float]]) -> Optional[float]:
    """σ per √second from (ts, price) samples: sqrt(Σ r² / Σ Δt)."""
    if len(prices) < 3:
        return None
    sq, span = 0.0, 0.0
    for (t0, p0), (t1, p1) in zip(prices, prices[1:]):
        dt = t1 - t0
        if dt <= 0 or p0 <= 0 or p1 <= 0:
            continue
        r = math.log(p1 / p0)
        sq += r * r
        span += dt
    if span <= 0:
        return None
    return math.sqrt(sq / span)


def prob_up(price_now: float, price_start: float, sigma_per_s: float, remaining_s: float) -> float:
    """P(end >= start) under a driftless lognormal. Ties resolve Up."""
    if remaining_s <= 0:
        return 1.0 if price_now >= price_start else 0.0
    sd = max(sigma_per_s, FAIR_VALUE_VOL_FLOOR_PER_S) * math.sqrt(remaining_s)
    return _norm_cdf(math.log(price_now / price_start) / sd)


def edge_score(edge: float) -> float:
    """Edge → [0,100] score for the combined-score pipeline (0 at ≤0 edge)."""
    return round(max(0.0, min(100.0, edge / FAIR_VALUE_EDGE_FULL * 100.0)), 1)


def _start_price(source: str, window_start: int) -> Optional[float]:
    key = (source, window_start)
    if key in _start_cache:
        return _start_cache[key]
    feed = chainlink_price if source == "chainlink" else btc_price
    px = feed.get_price_at(window_start, tolerance=5.0 if source == "chainlink" else 30.0)
    if px is not None:
        _start_cache[key] = px
        # Bound memory: drop windows older than ~1h.
        for k in [k for k in _start_cache if k[1] < window_start - 3600]:
            del _start_cache[k]
    return px


def _pick_source(window_start: int) -> tuple[str, Optional[float], Optional[float]]:
    """(source, start_price, now_price) — Chainlink if usable, else exchange."""
    if chainlink_price.get_latest_age() <= _CHAINLINK_MAX_AGE:
        start = _start_price("chainlink", window_start)
        if start is not None:
            return "chainlink", start, chainlink_price.get_latest()
    return "exchange", _start_price("exchange", window_start), btc_price.get_latest()


def check(market_end_ts: float, side: str, ask: float, now: Optional[float] = None) -> FilterResult:
    """
    Fair probability and edge for buying `side` ("YES"=Up / "NO"=Down) at `ask`.

    passed=False only when the inputs are unavailable (no start price yet,
    e.g. the bot started mid-window). Whether a small/negative edge blocks
    the trade is the caller's decision (FAIR_VALUE_MODE).
    `score` is the edge in cents (may be negative) so it is recorded verbatim
    in positions.filter_scores; use edge_score() for a 0-100 pipeline score.
    """
    now = time.time() if now is None else now
    window_start = int(market_end_ts - MARKET_DURATION_SECONDS)
    remaining = max(0.0, market_end_ts - now)

    source, start, px_now = _pick_source(window_start)
    if start is None or px_now is None or start <= 0 or px_now <= 0:
        return FilterResult(
            passed=False, score=0.0,
            reason="无窗口起始价，无法计算公允概率",
            details={"source": source, "window_start": window_start},
        )

    feed = chainlink_price if source == "chainlink" else btc_price
    sigma = realised_vol_per_s(feed.get_prices_since(now - FAIR_VALUE_VOL_LOOKBACK))
    sigma_used = max(sigma or 0.0, FAIR_VALUE_VOL_FLOOR_PER_S)

    p_up = prob_up(px_now, start, sigma_used, remaining)
    fair = p_up if side == "YES" else 1.0 - p_up
    edge = fair - ask - FEE_PER_SHARE

    return FilterResult(
        passed=True,
        score=round(edge * 100.0, 2),
        reason=(
            f"公允{side}={fair:.3f} 卖一={ask:.3f} edge={edge:+.3f} "
            f"(起始={start:,.2f} 现={px_now:,.2f} 剩{remaining:.0f}s σ={sigma_used:.2e}/√s {source})"
        ),
        details={
            "source": source, "start": start, "now": px_now, "remaining": remaining,
            "sigma_per_s": sigma_used, "p_up": p_up, "fair": fair, "ask": ask, "edge": edge,
        },
    )
