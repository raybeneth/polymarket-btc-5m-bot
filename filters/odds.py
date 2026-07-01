"""
Filter 1 — Odds / Price Quality

Checks whether the current price (implied probability) is in a range
that offers acceptable risk/reward.

Logic:
  price < ODDS_MIN_PRICE      → skip (lottery, payout is huge but prob too low)
  price > ODDS_MAX_PRICE      → skip (hard ceiling, near-certain, payout tiny)
  price > dynamic_max(dir)    → skip (too expensive for this conviction level)
  Otherwise score based on distance from ideal centre (0.50):
    - Prices near 0.50 score highest (maximum uncertainty → maximum payout leverage)
    - The acceptable upper bound scales with direction strength: strong signals
      may pay up toward ODDS_MAX_PRICE, weak ones are capped at ODDS_DYN_MAX_WEAK.
"""
from config import (
    FilterResult,
    ODDS_MIN_PRICE,
    ODDS_MAX_PRICE,
    ODDS_PREFERRED_MIN,
    ODDS_PREFERRED_MAX,
    ODDS_DYN_MAX_WEAK,
)


def _dynamic_max_price(direction_strength: float) -> float:
    """
    Effective acceptable price ceiling as a function of direction strength.

    direction_strength is the direction filter's score [0,100].
      0   → ODDS_DYN_MAX_WEAK  (only cheap prices allowed)
      100 → ODDS_MAX_PRICE     (hard ceiling)
    Linearly interpolated between, clamped to the hard ceiling.
    """
    s = max(0.0, min(100.0, direction_strength))
    dyn = ODDS_DYN_MAX_WEAK + (ODDS_MAX_PRICE - ODDS_DYN_MAX_WEAK) * (s / 100.0)
    return min(dyn, ODDS_MAX_PRICE)


def check(price: float, direction_strength: float = 100.0) -> FilterResult:
    """
    Parameters
    ----------
    price : float
        Current best ask for the side we intend to buy (YES or NO token).
    direction_strength : float
        Direction filter score [0,100]. Higher conviction raises the price
        ceiling we're willing to pay. Defaults to 100 (no extra restriction)
        for backward compatibility.

    Returns
    -------
    FilterResult
        passed=False  →  hard skip, don't trade
        passed=True   →  score [0,100] reflecting how attractive the odds are
    """
    # ── Hard bounds ───────────────────────────────────────────────────
    if price < ODDS_MIN_PRICE:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"价格 {price:.3f} 低于最低值 {ODDS_MIN_PRICE} — 彩票赔率",
            details={"price": price},
        )

    if price > ODDS_MAX_PRICE:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"价格 {price:.3f} 高于硬上限 {ODDS_MAX_PRICE} — 无收益空间",
            details={"price": price},
        )

    # ── Direction-adaptive ceiling ────────────────────────────────────
    dyn_max = _dynamic_max_price(direction_strength)
    if price > dyn_max:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=(
                f"价格 {price:.3f} 高于动态上限 {dyn_max:.3f} "
                f"— 方向强度 {direction_strength:.0f} 不足以支撑此价格"
            ),
            details={"price": price, "dyn_max": dyn_max, "direction_strength": direction_strength},
        )

    # ── Score within acceptable range ────────────────────────────────
    # Perfect score at price = 0.50 (max uncertainty, best payout leverage).
    # Score degrades linearly as price moves toward either hard bound.
    #
    # In preferred range → 50–100
    # Outside preferred but within hard bounds → 0–50
    if ODDS_PREFERRED_MIN <= price <= ODDS_PREFERRED_MAX:
        # Map [preferred_min, preferred_max] → [50, 100]
        distance_from_center = abs(price - 0.50)
        max_distance_preferred = max(
            0.50 - ODDS_PREFERRED_MIN,
            ODDS_PREFERRED_MAX - 0.50,
        )
        score = 100.0 - 50.0 * (distance_from_center / max_distance_preferred)
    else:
        # Outside preferred range but inside hard bounds
        if price < ODDS_PREFERRED_MIN:
            # Map [ODDS_MIN_PRICE, ODDS_PREFERRED_MIN] → [0, 50]
            ratio = (price - ODDS_MIN_PRICE) / (ODDS_PREFERRED_MIN - ODDS_MIN_PRICE)
        else:
            # Map [ODDS_PREFERRED_MAX, ODDS_MAX_PRICE] → [50, 0]
            ratio = 1.0 - (price - ODDS_PREFERRED_MAX) / (ODDS_MAX_PRICE - ODDS_PREFERRED_MAX)
        score = 50.0 * max(0.0, ratio)

    # Expected value hint in the reason string
    payout = 1.0 / price if price > 0 else 0.0
    return FilterResult(
        passed=True,
        score=round(score, 1),
        reason=f"价格 {price:.3f} 合适 — 隐含概率={price:.1%}，赔付={payout:.2f}x",
        details={"price": price, "payout": payout},
    )
