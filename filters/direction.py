"""
Filter 5 — Direction Strength

Combines two independent signals into one direction score [-100, +100]:

  Signal A — BTC Spot Momentum (65% weight)
    Measures how strongly BTC spot price is moving over the last
    DIRECTION_LOOKBACK_SECONDS window using a fast/slow MA crossover.
    Fast MA window = 30s, Slow MA window = 90s.

  Signal B — Polymarket Order Book Imbalance (35% weight)
    Bid vs ask volume on the binary market's own book.
    Net buying pressure on the ask side → bullish signal.

Direction Smoothing:
    Instead of acting on a single snapshot, we average the combined score
    over DIRECTION_SMOOTH_WINDOW seconds. This filters out single-tick noise
    while still allowing trades when direction is persistently biased.

Final direction:
    smoothed_score ≥  MIN_DIRECTION_SCORE → BULLISH  → buy YES token
    smoothed_score ≤ -MIN_DIRECTION_SCORE → BEARISH  → buy NO  token
    |smoothed_score| <  MIN_DIRECTION_SCORE → NEUTRAL → skip
"""
import time
import math
from collections import deque
from feeds import btc_price
from feeds.polymarket_clob import OrderBook
from config import (
    FilterResult,
    DIRECTION_MIN_SCORE,
    DIRECTION_FAST_WINDOW,
    DIRECTION_SLOW_WINDOW,
    DIRECTION_LOOKBACK_SECONDS,
    DIRECTION_OB_WEIGHT,
    DIRECTION_MOMENTUM_WEIGHT,
    DIRECTION_SMOOTH_WINDOW,
    DIRECTION_VOL_SIGMA_FULL,
    DIRECTION_VOL_FLOOR_PCT,
)


# ─────────────────────────────────────────────
# Direction score history for smoothing
# ─────────────────────────────────────────────
# Stores (timestamp, combined_score)
_score_history: deque = deque(maxlen=100)


def _get_smoothed_score(current_score: float, now: float) -> float:
    """
    Average combined scores over the smoothing window.
    If not enough history, returns current_score as-is.
    """
    _score_history.append((now, current_score))

    cutoff = now - DIRECTION_SMOOTH_WINDOW
    recent_scores = [s for t, s in _score_history if t >= cutoff]

    if len(recent_scores) < 2:
        return current_score

    return sum(recent_scores) / len(recent_scores)


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _realised_vol_pct(prices_raw: list[tuple[float, float]]) -> float:
    """
    Estimate realised volatility (in %) of the BTC tape over the sample window,
    expressed at the same horizon as the momentum move (the full lookback span).

    We take tick-to-tick log returns, compute their standard deviation, then
    scale by sqrt(N) to turn per-tick sigma into the volatility of the whole
    lookback move. This is the natural denominator for the move: a move of
    1 horizon-sigma is "ordinary", several sigma is a real directional push.
    """
    prices = [p for _, p in prices_raw if p > 0]
    if len(prices) < 3:
        return DIRECTION_VOL_FLOOR_PCT

    rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    per_tick_sigma = math.sqrt(var)

    # Aggregate per-tick sigma to the full-window horizon, convert to %.
    horizon_sigma_pct = per_tick_sigma * math.sqrt(n) * 100.0
    return max(horizon_sigma_pct, DIRECTION_VOL_FLOOR_PCT)


def _compute_momentum_score() -> tuple[float, dict]:
    """
    Fast/Slow MA crossover on BTC spot history.
    Returns normalised score [-100, +100] and debug details.

    Normalisation is volatility-adaptive: the raw momentum (a % move) is
    divided by the recent realised volatility rather than a fixed % threshold,
    so the signal stays calibrated across calm and violent regimes.
    """
    now = time.time()
    prices_raw = btc_price.get_prices_since(now - DIRECTION_LOOKBACK_SECONDS)

    if len(prices_raw) < 5:
        return 0.0, {"error": "not enough BTC price samples", "samples": len(prices_raw)}

    # Fast MA: average over last FAST_WINDOW seconds
    fast_cutoff = now - DIRECTION_FAST_WINDOW
    slow_cutoff = now - DIRECTION_SLOW_WINDOW

    fast_prices = [p for t, p in prices_raw if t >= fast_cutoff]
    slow_prices = [p for t, p in prices_raw if t >= slow_cutoff]

    if not fast_prices or not slow_prices:
        return 0.0, {"error": "insufficient samples for MA windows"}

    fast_ma = sum(fast_prices) / len(fast_prices)
    slow_ma = sum(slow_prices) / len(slow_prices)

    # Crossover as percentage of slow MA
    # Positive = fast above slow = uptrend
    crossover_pct = (fast_ma - slow_ma) / slow_ma * 100.0 if slow_ma > 0 else 0.0

    # Also compute raw directional momentum (close-to-close over lookback)
    oldest_price = prices_raw[0][1]
    latest_price = prices_raw[-1][1]
    total_move_pct = (latest_price - oldest_price) / oldest_price * 100.0

    # Combine crossover and total move into a single % move signal
    combined_move_pct = 0.6 * crossover_pct + 0.4 * total_move_pct

    # Volatility-adaptive scaling: full scale = SIGMA_FULL horizon-sigmas.
    vol_pct = _realised_vol_pct(prices_raw)
    full_scale_pct = DIRECTION_VOL_SIGMA_FULL * vol_pct
    score = max(-100.0, min(100.0, combined_move_pct / full_scale_pct * 100.0))

    return score, {
        "fast_ma": round(fast_ma, 2),
        "slow_ma": round(slow_ma, 2),
        "crossover_pct": round(crossover_pct, 4),
        "total_move_pct": round(total_move_pct, 4),
        "combined_move_pct": round(combined_move_pct, 4),
        "vol_pct": round(vol_pct, 4),
        "full_scale_pct": round(full_scale_pct, 4),
        "momentum_score": round(score, 1),
        "samples": len(prices_raw),
    }


def _compute_ob_imbalance_score(yes_book: OrderBook, no_book: OrderBook) -> tuple[float, dict]:
    """
    Compare demand pressure between the YES and NO books.

    Each book's imbalance is +1 when bid-heavy (demand/support → that token
    is being bid up) and -1 when ask-heavy (supply/overhang → that token is
    being pressured down). See OrderBook.imbalance.

    Returns score [-100, +100]:
      +100 = strong demand for YES relative to NO → bullish (buy YES)
      -100 = strong demand for NO  relative to YES → bearish (buy NO)
    """
    yes_imb = yes_book.imbalance(levels=5)  # [-1, +1], + = YES bid-heavy (bullish)
    no_imb = no_book.imbalance(levels=5)    # [-1, +1], + = NO  bid-heavy (bearish)

    # net = YES demand minus NO demand. YES support is bullish, NO support is
    # bearish, so subtracting no_imb correctly pulls the score toward NO.
    net = yes_imb - no_imb  # range [-2, +2]
    score = max(-100.0, min(100.0, net * 50.0))

    return score, {
        "yes_imbalance": round(yes_imb, 3),
        "no_imbalance": round(no_imb, 3),
        "net": round(net, 3),
        "ob_score": round(score, 1),
    }


# ─────────────────────────────────────────────
# Public filter entry point
# ─────────────────────────────────────────────

def check(yes_book: OrderBook, no_book: OrderBook) -> FilterResult:
    """
    Parameters
    ----------
    yes_book : OrderBook for the YES token of this market
    no_book  : OrderBook for the NO  token of this market

    Returns
    -------
    FilterResult
        details["direction"] = "YES" | "NO" | "NEUTRAL"
        details["score_raw"] = combined direction score [-100, +100]
    """
    momentum_score, momentum_details = _compute_momentum_score()
    ob_score, ob_details = _compute_ob_imbalance_score(yes_book, no_book)

    # Weighted combination (instantaneous)
    combined = (
        DIRECTION_MOMENTUM_WEIGHT * momentum_score
        + DIRECTION_OB_WEIGHT * ob_score
    )

    # Smooth over recent history to reduce noise
    now = time.time()
    smoothed = _get_smoothed_score(combined, now)

    abs_score = abs(smoothed)
    direction = "NEUTRAL"

    if smoothed >= DIRECTION_MIN_SCORE:
        direction = "YES"
    elif smoothed <= -DIRECTION_MIN_SCORE:
        direction = "NO"

    if direction == "NEUTRAL":
        return FilterResult(
            passed=False,
            score=abs_score,
            reason=(
                f"方向信号过弱 — 综合={smoothed:.1f}，"
                f"需要 |score| ≥ {DIRECTION_MIN_SCORE}"
            ),
            details={
                "direction": "NEUTRAL",
                "score_raw": round(smoothed, 1),
                "score_instant": round(combined, 1),
                "momentum": momentum_details,
                "orderbook": ob_details,
            },
        )

    # Normalise abs_score to 0-100 filter score (MIN_DIRECTION_SCORE maps to 0)
    # 100 maps to 100
    filter_score = min(100.0, 100.0 * (abs_score - DIRECTION_MIN_SCORE) / (100.0 - DIRECTION_MIN_SCORE))

    return FilterResult(
        passed=True,
        score=round(filter_score, 1),
        reason=f"方向 {direction} — 综合评分={smoothed:.1f}",
        details={
            "direction": direction,
            "score_raw": round(smoothed, 1),
            "score_instant": round(combined, 1),
            "momentum": momentum_details,
            "orderbook": ob_details,
        },
    )
