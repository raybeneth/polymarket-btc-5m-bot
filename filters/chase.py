"""
Filter 4 — Chase Space (slippage tolerance)

Answers: "If we need to cross the spread and even chase a little,
is there enough liquidity within our chase budget?"

We submit a limit order at best_ask + ORDER_PRICE_SLIPPAGE (from config).
This filter checks whether the aggregated ask volume between best_ask and
best_ask + CHASE_BUDGET is enough to fill at least CHASE_MIN_FILL_SHARES.

A good chase score means we can get filled near the quoted price;
a bad chase score means we'd have to pay a lot more or get a partial fill.
"""
from feeds.polymarket_clob import OrderBook
from config import (
    FilterResult,
    CHASE_BUDGET,
    CHASE_MIN_FILL_SHARES,
    TRADE_SIZE_USD,
)


def check(book: OrderBook) -> FilterResult:
    """
    Parameters
    ----------
    book : OrderBook for the token we intend to buy (already filtered by spread)

    Returns
    -------
    FilterResult
    """
    if not book.asks:
        return FilterResult(
            passed=False,
            score=0.0,
            reason="订单簿无卖单 — 无法评估追单空间",
        )

    best_ask = book.best_ask
    chase_ceiling = best_ask + CHASE_BUDGET

    # Aggregate ask volume within the chase window
    available_in_window = book.ask_depth(within_price=chase_ceiling)

    # Translate our USD trade size into shares at best_ask
    shares_needed = TRADE_SIZE_USD / best_ask if best_ask > 0 else 999

    # ── Hard check: minimum shares ───────────────────────────────────
    if available_in_window < CHASE_MIN_FILL_SHARES:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=(
                f"追单窗口 [{best_ask:.3f}, {chase_ceiling:.3f}] 内仅 {available_in_window:.0f} 股"
                f" — 需要 ≥{CHASE_MIN_FILL_SHARES}"
            ),
            details={
                "best_ask": best_ask,
                "chase_ceiling": chase_ceiling,
                "available": available_in_window,
                "needed": shares_needed,
            },
        )

    # ── Score: based on fill coverage ───────────────────────────────
    # How much of our order can we fill within chase budget?
    fill_ratio = min(1.0, available_in_window / max(shares_needed, 1.0))
    # Also reward having more headroom above the minimum
    headroom_ratio = min(1.0, available_in_window / (2 * CHASE_MIN_FILL_SHARES))

    score = 100.0 * (0.6 * fill_ratio + 0.4 * headroom_ratio)
    score = max(0.0, min(100.0, score))

    return FilterResult(
        passed=True,
        score=round(score, 1),
        reason=(
            f"追单空间充足 — {available_in_window:.0f} 股可用，上限 {chase_ceiling:.3f} "
            f"(成交覆盖率 {fill_ratio:.0%})"
        ),
        details={
            "best_ask": best_ask,
            "chase_ceiling": chase_ceiling,
            "available_in_window": available_in_window,
            "shares_needed": shares_needed,
            "fill_ratio": fill_ratio,
        },
    )
