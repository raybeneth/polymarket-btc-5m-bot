"""
Filter 3 — Order Book Spread & Depth

Checks:
  1. Bid-ask spread is not too wide (wide spread = market maker extracting too much)
  2. Sufficient shares available at the best ask for our intended side
  3. Both bid and ask are non-trivial (not a dead book)
"""
from feeds.polymarket_clob import OrderBook
from config import (
    FilterResult,
    SPREAD_MAX_ALLOWED,
    SPREAD_WARN_THRESHOLD,
    DEPTH_MIN_SHARES,
    MIN_BID,
    MIN_ASK,
)


def check(book: OrderBook, intended_side: str) -> FilterResult:
    """
    Parameters
    ----------
    book          : OrderBook for the token we intend to buy
    intended_side : "YES" or "NO" (determines which book side we check depth on)

    Returns
    -------
    FilterResult
    """
    # ── Guard: book must have at least one level each side ───────────
    if book.best_bid is None or book.best_ask is None:
        return FilterResult(
            passed=False,
            score=0.0,
            reason="订单簿为空 — 无买单或卖单",
            details={},
        )

    # ── Guard: minimum activity thresholds ───────────────────────────
    if book.best_bid < MIN_BID:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"最佳买单 {book.best_bid:.3f} 低于最低要求 {MIN_BID} — 死盘",
            details={"best_bid": book.best_bid},
        )
    if book.best_ask < MIN_ASK:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"最佳卖单 {book.best_ask:.3f} 低于最低要求 {MIN_ASK} — 死盘",
            details={"best_ask": book.best_ask},
        )

    spread = book.spread  # guaranteed non-None at this point

    # ── Hard limit on spread ─────────────────────────────────────────
    if spread > SPREAD_MAX_ALLOWED:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"价差 {spread:.4f} > 最大允许值 {SPREAD_MAX_ALLOWED} — 过宽",
            details={"spread": spread, "bid": book.best_bid, "ask": book.best_ask},
        )

    # ── Depth check on the ask side (we are buying, so we consume asks) ─
    depth_at_ask = book.asks[0].size if book.asks else 0.0
    if depth_at_ask < DEPTH_MIN_SHARES:
        return FilterResult(
            passed=False,
            score=0.0,
            reason=f"最佳卖单深度仅 {depth_at_ask:.0f} 股 < 最低要求 {DEPTH_MIN_SHARES}",
            details={"depth_at_ask": depth_at_ask, "spread": spread},
        )

    # ── Score ────────────────────────────────────────────────────────
    # Spread component: 100 when spread=0, 0 when spread=SPREAD_MAX_ALLOWED
    spread_score = 100.0 * (1.0 - spread / SPREAD_MAX_ALLOWED)

    # Depth component: 100 when depth >= 3x minimum, 0 at minimum
    depth_score = min(100.0, 100.0 * (depth_at_ask - DEPTH_MIN_SHARES) / (3 * DEPTH_MIN_SHARES))
    depth_score = max(0.0, depth_score)

    score = 0.6 * spread_score + 0.4 * depth_score

    spread_warn = " [偏宽]" if spread > SPREAD_WARN_THRESHOLD else ""
    return FilterResult(
        passed=True,
        score=round(score, 1),
        reason=(
            f"价差 {spread:.4f}{spread_warn}，"
            f"卖单深度={depth_at_ask:.0f} 股"
        ),
        details={
            "spread": spread,
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "depth_at_ask": depth_at_ask,
            "spread_score": round(spread_score, 1),
            "depth_score": round(depth_score, 1),
        },
    )
