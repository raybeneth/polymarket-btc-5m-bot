"""
Filter 1 — Odds / Price Quality

Checks whether the current price (implied probability) is in a range
that offers acceptable risk/reward.

Logic:
  price < ODDS_MIN_PRICE → skip (lottery, payout is huge but prob too low)
  price > ODDS_MAX_PRICE → skip (near-certain, payout tiny)
  Otherwise score based on distance from ideal centre (0.50):
    - Prices near 0.50 score highest (maximum uncertainty → maximum payout leverage)
    - But we also reward conviction: very strong direction signals can still trade
      at 0.35 or 0.65 if they clear the hard bounds
"""
from config import (
    FilterResult,
    ODDS_MIN_PRICE,
    ODDS_MAX_PRICE,
    ODDS_PREFERRED_MIN,
    ODDS_PREFERRED_MAX,
)


def check(price: float) -> FilterResult:
    """
    Parameters
    ----------
    price : float
        Current best ask for the side we intend to buy (YES or NO token).

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
            reason=f"Price {price:.3f} below minimum {ODDS_MIN_PRICE} — lottery odds",
            details={"price": price},
        )

    if price > ODDS_MAX_PRICE:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"Price {price:.3f} above maximum {ODDS_MAX_PRICE} — no return",
            details={"price": price},
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
        reason=f"Price {price:.3f} OK — implied_prob={price:.1%}, payout={payout:.2f}x",
        details={"price": price, "payout": payout},
    )
