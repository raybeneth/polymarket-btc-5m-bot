"""
Filter 2 — Entry Timing

Checks how much time has elapsed and how much remains in the 5-min market.
We want to enter in a specific window:
  - Not too early: direction hasn't formed yet
  - Not too late:  price nearly converged → no edge left

Window defined in config:
  TIMING_ENTRY_OPEN_AT  = min elapsed seconds (default 45s)
  TIMING_ENTRY_CLOSE_AT = max elapsed seconds (default 210s = 3m30s)
  => at that point remaining ≥ 90s

Score is highest in the middle of the window (around 2 min elapsed)
and tapers off toward the edges.
"""
from config import (
    FilterResult,
    TIMING_MIN_ELAPSED,
    TIMING_MAX_ELAPSED,
    TIMING_MIN_REMAINING,
    MARKET_DURATION_SECONDS,
)


def check(elapsed_seconds: float, remaining_seconds: float) -> FilterResult:
    """
    Parameters
    ----------
    elapsed_seconds  : seconds since market opened
    remaining_seconds: seconds until market resolves

    Returns
    -------
    FilterResult
    """
    elapsed = max(0.0, elapsed_seconds)
    remaining = max(0.0, remaining_seconds)

    # ── Too early: direction not readable yet ────────────────────────
    if elapsed < TIMING_MIN_ELAPSED:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"Too early — only {elapsed:.0f}s elapsed, need ≥{TIMING_MIN_ELAPSED}s",
            details={"elapsed": elapsed, "remaining": remaining},
        )

    # ── Too late: not enough time to benefit from being right ────────
    if remaining < TIMING_MIN_REMAINING:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"Too late — only {remaining:.0f}s remaining, need ≥{TIMING_MIN_REMAINING}s",
            details={"elapsed": elapsed, "remaining": remaining},
        )

    if elapsed > TIMING_MAX_ELAPSED:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"Too late — {elapsed:.0f}s elapsed exceeds max {TIMING_MAX_ELAPSED}s",
            details={"elapsed": elapsed, "remaining": remaining},
        )

    # ── Score: peaks at the midpoint of the entry window ─────────────
    # Window: [TIMING_MIN_ELAPSED, TIMING_MAX_ELAPSED]
    window_width = TIMING_MAX_ELAPSED - TIMING_MIN_ELAPSED
    window_mid = TIMING_MIN_ELAPSED + window_width / 2  # ~127.5s elapsed
    distance_from_mid = abs(elapsed - window_mid)
    max_distance = window_width / 2
    score = 100.0 * (1.0 - distance_from_mid / max_distance)
    score = max(0.0, min(100.0, score))

    return FilterResult(
        passed=True,
        score=round(score, 1),
        reason=f"Timing OK — {elapsed:.0f}s elapsed, {remaining:.0f}s remaining",
        details={"elapsed": elapsed, "remaining": remaining, "window_mid": window_mid},
    )
