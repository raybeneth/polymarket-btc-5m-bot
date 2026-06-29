"""
Polymarket BTC 5-min Bot — Main Loop
=====================================

Architecture:
  1. BTC price feed (Binance WS) starts in background
  2. Market scanner polls Gamma API for active BTC 5-min markets
  3. For each active market, every ~2 seconds:
       a. Fetch YES + NO order books from CLOB
       b. Run all 5 filters sequentially (short-circuit on first failure)
       c. If all pass and combined score ≥ threshold → execute trade
  4. Expired positions are resolved periodically
  5. Risk state is checked before every trade

One trade per market per market-lifetime (deduplication via `_traded_markets`).
"""
import asyncio
import logging
import time

import aiohttp

from config import (
    PAPER_TRADING,
    SCORE_WEIGHTS,
    MIN_COMBINED_SCORE,
    TradeDecision,
)
from feeds import btc_price
from feeds.polymarket_clob import run_scanner, get_active_markets, fetch_both_books
from filters import odds, timing, spread, chase, direction
from execution import order_manager
from execution.risk import get_state
from utils.logger import setup_logging

log = logging.getLogger(__name__)

# Markets we've already traded in this run (prevents double-entry per market)
_traded_markets: set[str] = set()

# Track last skip reason per market to suppress identical repeat lines
# format: market_id → (blocker_filter, reason_str)
_last_logged: dict[str, tuple[str, str]] = {}


def _fmt_scores(results: dict) -> str:
    """Compact score summary: 'dir=82 odds=71 timing=88 sprd=65 chs=70'"""
    short = {"direction": "dir", "odds": "odds", "timing": "time", "spread": "sprd", "chase": "chs"}
    return "  ".join(
        f"{short.get(k, k)}={'✓' + str(int(v.score)) if v.passed else '✗'}"
        for k, v in results.items()
    )


async def evaluate_market(session: aiohttp.ClientSession, market) -> None:
    """
    Run the complete filter pipeline for one market.
    Every evaluation is logged — passes, skips, and trades.
    Identical consecutive skip reasons are suppressed to avoid console spam.
    """
    market_id = market.market_id
    mid = market_id[:10]

    # ── Silent guards (no log — these are pure state dedup) ──────────
    if market_id in _traded_markets:
        return
    state = get_state()
    if state.position_for_market(market_id):
        return

    # ── Fetch order books ────────────────────────────────────────────
    yes_book, no_book = await fetch_both_books(session, market)
    if yes_book is None or no_book is None:
        _log_skip(mid, market_id, "book_fetch", "Could not fetch order books from CLOB", {})
        return

    elapsed = market.elapsed_seconds
    remaining = market.remaining_seconds

    # We'll collect results as we go for the final score line
    results: dict = {}

    # ── Filter 1: Direction ──────────────────────────────────────────
    dir_result = direction.check(yes_book, no_book)
    results["direction"] = dir_result
    if not dir_result.passed:
        _log_skip(mid, market_id, "direction", dir_result.reason, results,
                  extra=f"elapsed={elapsed:.0f}s remaining={remaining:.0f}s")
        return

    side = dir_result.details["direction"]
    book = yes_book if side == "YES" else no_book
    entry_price = book.best_ask

    if entry_price is None:
        _log_skip(mid, market_id, "direction", f"No ask price for {side} book", results)
        return

    # ── Filter 2: Odds ───────────────────────────────────────────────
    odds_result = odds.check(entry_price)
    results["odds"] = odds_result
    if not odds_result.passed:
        _log_skip(mid, market_id, "odds", odds_result.reason, results,
                  extra=f"side={side} ask={entry_price:.3f}")
        return

    # ── Filter 3: Timing ─────────────────────────────────────────────
    timing_result = timing.check(elapsed, remaining)
    results["timing"] = timing_result
    if not timing_result.passed:
        _log_skip(mid, market_id, "timing", timing_result.reason, results)
        return

    # ── Filter 4: Spread + depth ─────────────────────────────────────
    spread_result = spread.check(book, side)
    results["spread"] = spread_result
    if not spread_result.passed:
        _log_skip(mid, market_id, "spread", spread_result.reason, results,
                  extra=f"bid={book.best_bid:.3f} ask={book.best_ask:.3f}")
        return

    # ── Filter 5: Chase space ────────────────────────────────────────
    chase_result = chase.check(book)
    results["chase"] = chase_result
    if not chase_result.passed:
        _log_skip(mid, market_id, "chase", chase_result.reason, results)
        return

    # ── All 5 filters passed — compute combined score ────────────────
    filter_results = {
        "direction": dir_result,
        "odds":      odds_result,
        "timing":    timing_result,
        "spread":    spread_result,
        "chase":     chase_result,
    }
    combined_score = sum(
        SCORE_WEIGHTS[name] * result.score
        for name, result in filter_results.items()
    )

    if combined_score < MIN_COMBINED_SCORE:
        reason = f"Score too low: {combined_score:.1f} < {MIN_COMBINED_SCORE}"
        _log_skip(mid, market_id, "score_gate", reason, filter_results)
        return

    # ── TRADE SIGNAL ─────────────────────────────────────────────────
    # Always log trades unconditionally (clear _last_logged so it shows again if re-evaluated)
    _last_logged.pop(market_id, None)

    token_id = market.yes_token_id if side == "YES" else market.no_token_id
    decision = TradeDecision(
        should_trade=True,
        side=side,
        market_id=market_id,
        token_id=token_id,
        entry_price=entry_price,
        combined_score=combined_score,
        filter_results=filter_results,
    )

    log.info("─" * 70)
    log.info(
        f"[{mid}] ★ TRADE  {side:3s} @ {entry_price:.3f}  "
        f"score={combined_score:.1f}  elapsed={elapsed:.0f}s remaining={remaining:.0f}s"
    )
    log.info(f"  question : {market.question[:65]}")
    log.info(f"  direction: {dir_result.reason}")
    log.info(f"  odds     : {odds_result.reason}")
    log.info(f"  timing   : {timing_result.reason}")
    log.info(f"  spread   : {spread_result.reason}")
    log.info(f"  chase    : {chase_result.reason}")
    log.info("─" * 70)

    filled = await order_manager.execute(decision)
    if filled:
        _traded_markets.add(market_id)


def _log_skip(
    mid: str,
    market_id: str,
    blocker: str,
    reason: str,
    results: dict,
    extra: str = "",
) -> None:
    """
    Log a SKIP line at INFO level.
    Suppresses the line if the exact same (blocker, reason) was logged last cycle
    for this market — prevents identical spam every 2 seconds.
    """
    key = (blocker, reason)
    if _last_logged.get(market_id) == key:
        return  # same reason as last cycle — suppress

    _last_logged[market_id] = key
    scores = _fmt_scores(results) if results else ""
    extra_part = f"  {extra}" if extra else ""
    log.info(f"[{mid}] SKIP {blocker:<12}  {reason}{extra_part}  {scores}")


# ─────────────────────────────────────────────
# Main event loop
# ─────────────────────────────────────────────

async def main_loop() -> None:
    mode = "PAPER" if PAPER_TRADING else "LIVE"
    log.info(f"Bot starting in {mode} mode …")

    # Start BTC price feed
    await btc_price.start_feed()

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:

        # Start market scanner as background task
        asyncio.create_task(run_scanner(session), name="market_scanner")

        # Allow scanner first fetch to complete
        await asyncio.sleep(3.0)

        log.info("Entering main evaluation loop …")
        cycle = 0

        while True:
            cycle += 1
            markets = get_active_markets()

            if not markets:
                log.info("No active BTC 5-min markets found — waiting …")
                await asyncio.sleep(5.0)
                continue

            if cycle % 30 == 0:  # status every ~60s
                state = get_state()
                log.info(f"Status: {len(markets)} active markets | {state.summary()}")

            # Evaluate all markets concurrently
            tasks = [evaluate_market(session, m) for m in markets.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Resolve any expired positions
            await order_manager.resolve_expired_positions()

            # Clean up traded markets that are now expired
            active_ids = set(markets.keys())
            expired_traded = _traded_markets - active_ids
            _traded_markets.difference_update(expired_traded)

            await asyncio.sleep(2.0)  # evaluation cadence: 2 seconds


def main() -> None:
    setup_logging()
    log.info("=" * 60)
    log.info("  Polymarket BTC 5-min Trading Bot")
    log.info("=" * 60)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        state = get_state()
        log.info(f"Shutting down. Final stats: {state.summary()}")


if __name__ == "__main__":
    main()
