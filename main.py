"""
Polymarket BTC 5-min Bot — Main Loop
=====================================

Architecture:
  1. BTC price feed (Binance WS) starts in background
  2. Market scanner polls Gamma API for active BTC 5-min markets
  3. For each active market, every ~2 seconds:
       a. Fetch YES + NO order books from CLOB
       b. Run all 5 filters sequentially (short-circuit on first failure)
       c. If all pass and combined score >= threshold -> execute trade
  4. Expired positions are resolved periodically
  5. Risk state is checked before every trade
  6. Web dashboard served on port 8080

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
    MIN_TIMING_FILTER_SCORE,
    TRADE_SIZE_USD,
    DIRECTION_CONSISTENCY_HIGH,
    DIRECTION_CONSISTENCY_LOW,
    STOP_LOSS_RATIO,
    TradeDecision,
)
from feeds import btc_price
from feeds.polymarket_clob import run_scanner, get_active_markets, fetch_both_books, create_session
from filters import odds, timing, spread, chase, direction
from execution import order_manager
from execution.risk import get_state
from storage import db
from utils.logger import setup_logging, attach_web_logger
from web import server as web_server

log = logging.getLogger(__name__)

# Markets we've already traded in this run (prevents double-entry per market)
_traded_markets: set[str] = set()

# Historical stats (loaded once at startup)
_history: dict = {}
_start_time: float = 0.0

# Most recently closed positions, newest first (refreshed each loop cycle)
_recent_closed: list = []

# Cache of btc_target per 5-min window_start timestamp, so the dashboard
# target price doesn't jitter as ticks enter/leave the rolling buffer.
_target_cache: dict[int, float] = {}


async def evaluate_market(session: aiohttp.ClientSession, market) -> None:
    """Run the complete filter pipeline for one market."""
    market_id = market.market_id
    mid = market_id[:10]

    if market_id in _traded_markets:
        return
    state = get_state()
    if state.position_for_market(market_id):
        return

    yes_book, no_book = await fetch_both_books(session, market)
    if yes_book is None or no_book is None:
        log.debug(f"[{mid}] 跳过: 订单簿获取失败")
        return

    elapsed = market.elapsed_seconds
    remaining = market.remaining_seconds

    # Filter 1: Direction
    dir_result = direction.check(yes_book, no_book)
    if not dir_result.passed:
        log.debug(f"[{mid}] 跳过方向: 信号过弱 ({dir_result.details.get('score_raw', 0):.0f})")
        return

    side = dir_result.details["direction"]
    consistency = dir_result.details.get("consistency", 0.0)
    macro_trend = dir_result.details.get("macro_trend", "FLAT")

    # Gate A: Macro trend contradiction — never trade against the CEX kline trend
    macro_contradicts = (
        (macro_trend == "UP" and side == "NO") or
        (macro_trend == "DOWN" and side == "YES")
    )
    if macro_contradicts:
        log.info(f"[{mid}] 跳过宏观趋势矛盾: macro={macro_trend} vs side={side} (dir={dir_result.details.get('score_raw',0):.0f})")
        return

    # Gate B: Direction consistency — dynamic threshold replaces fixed hard floor
    if consistency >= DIRECTION_CONSISTENCY_HIGH:      # ≥80%: trending
        min_dir_score = 6.0     # raw ≥ ~25 is enough in a clear trend
    elif consistency >= DIRECTION_CONSISTENCY_LOW:     # ≥50%: moderate
        min_dir_score = 19.0    # raw ≥ ~35, need stronger signal
    else:
        log.info(f"[{mid}] 跳过方向不一致: consistency={consistency:.0%} < {DIRECTION_CONSISTENCY_LOW:.0%} (dir={dir_result.details.get('score_raw',0):.0f})")
        return                  # <50%: too choppy, skip entirely

    if dir_result.score < min_dir_score:
        log.info(
            f"[{mid}] 跳过方向: score={dir_result.score:.0f} < {min_dir_score:.0f} "
            f"(consistency={consistency:.0%})"
        )
        return

    book = yes_book if side == "YES" else no_book
    entry_price = book.best_ask
    if entry_price is None:
        log.debug(f"[{mid}] 跳过: {side} 无卖一价")
        return

    # Filter 2: Odds
    odds_result = odds.check(entry_price, direction_strength=dir_result.score)
    if not odds_result.passed:
        log.debug(f"[{mid}] 跳过赔率: 价格 {entry_price:.3f} 被拒绝")
        return

    # Filter 3: Timing
    timing_result = timing.check(elapsed, remaining)
    if not timing_result.passed:
        log.debug(f"[{mid}] 跳过时机: 已过{elapsed:.0f}s 剩余{remaining:.0f}s")
        return
    if timing_result.score < MIN_TIMING_FILTER_SCORE:
        log.debug(f"[{mid}] 跳过时机下限: {timing_result.score:.0f} < {MIN_TIMING_FILTER_SCORE:.0f}")
        return

    # Filter 4: Spread (pass/fail gate, no score contribution)
    spread_result = spread.check(book, side)
    if not spread_result.passed:
        log.debug(f"[{mid}] 跳过价差: {spread_result.reason}")
        return

    # Filter 5: Chase (pass/fail gate, no score contribution)
    chase_result = chase.check(book)
    if not chase_result.passed:
        log.debug(f"[{mid}] 跳过追单: {chase_result.reason}")
        return

    # Combined score — only strategy filters (direction + odds + timing) contribute;
    # spread/chase have weight 0 and serve as pass/fail gates above.
    filter_results = {
        "direction": dir_result, "odds": odds_result, "timing": timing_result,
        "spread": spread_result, "chase": chase_result,
    }
    combined_score = sum(
        SCORE_WEIGHTS[name] * result.score
        for name, result in filter_results.items()
        if SCORE_WEIGHTS[name] > 0
    )
    if combined_score < MIN_COMBINED_SCORE:
        log.debug(f"[{mid}] 跳过综合评分: {combined_score:.1f} < {MIN_COMBINED_SCORE}")
        return

    # Confidence tier → trade size
    macro_confirms = (
        (macro_trend == "UP" and side == "YES") or
        (macro_trend == "DOWN" and side == "NO")
    )
    if combined_score >= 60 and consistency >= DIRECTION_CONSISTENCY_HIGH and macro_confirms:
        trade_size = TRADE_SIZE_USD           # 100% — high confidence
        tier = "HIGH"
    else:
        trade_size = TRADE_SIZE_USD * 0.5     # 50% — medium confidence
        tier = "MED"

    # TRADE SIGNAL
    token_id = market.yes_token_id if side == "YES" else market.no_token_id
    decision = TradeDecision(
        should_trade=True, side=side, market_id=market_id, token_id=token_id,
        entry_price=entry_price, combined_score=combined_score,
        filter_results=filter_results, market_end_ts=market.end_timestamp,
        trade_size_usd=trade_size,
    )

    payout = 1.0 / entry_price if entry_price > 0 else 0
    scores_str = " | ".join(f"{k[:3]}={v.score:.0f}" for k, v in filter_results.items())
    log.info(
        f"SIGNAL {'^ ' if side == 'YES' else 'v '}{side} @ {entry_price:.3f} "
        f"x{payout:.2f} score={combined_score:.1f} [{scores_str}] "
        f"consist={consistency:.0%} macro={macro_trend} "
        f"tier={tier} ${trade_size:.2f} "
        f"t={elapsed:.0f}s/{remaining:.0f}s"
    )

    filled = await order_manager.execute(decision)
    if filled:
        _traded_markets.add(market_id)


def _build_web_state() -> dict:
    """Build state dict for web dashboard."""
    state = get_state()
    now = time.time()

    positions = []
    for pos in state.open_positions:
        stop_price = pos.entry_price * (1 - STOP_LOSS_RATIO)
        remaining = max(0, int(pos.market_end_ts - now)) if pos.market_end_ts > 0 else 0
        # Target = BTC price at the start of this market's own 5-min window —
        # the same reference point resolve_expired_positions() settles against,
        # i.e. the level BTC needs to stay above (YES) / below (NO) to win.
        # Cached so it doesn't jitter as ticks rotate through the buffer.
        if pos.market_end_ts > 0:
            tgt_key = int(pos.market_end_ts - 300)
            target = _target_cache.get(tgt_key)
            if target is None:
                target = btc_price.get_price_at(pos.market_end_ts - 300)
                if target is not None:
                    _target_cache[tgt_key] = target
        else:
            target = None
        positions.append({
            "side": pos.side,
            "entry": pos.entry_price,
            "qty": pos.shares,
            "hold": int(now - pos.opened_at),
            "stop": round(stop_price, 3),
            "expiry": remaining,
            "order_id": pos.order_id,
            "target": round(target, 2) if target is not None else None,
        })

    # Persistent chart reference: the open price of the CURRENT 5-min window
    # (the level BTC must stay above/below for the ongoing up/down market to
    # resolve). Always available even when we hold no position, so the dashboard
    # can always show a target line — not only while a trade is open.
    # Cached per window so the target doesn't jitter as ticks enter/leave the buffer.
    window_start = (int(now) // 300) * 300
    btc_target = _target_cache.get(window_start)
    if btc_target is None:
        btc_target = btc_price.get_price_at(window_start)
        if btc_target is not None:
            _target_cache[window_start] = btc_target

    return {
        "btc_price": btc_price.get_latest(),
        "btc_connected": btc_price.is_connected(),
        "btc_history": btc_price.get_chart_series(),
        "btc_target": round(btc_target, 2) if btc_target is not None else None,
        "markets_count": len(get_active_markets()),
        "today": {
            "trades": state.trades_today,
            "wins": state.wins_today,
            "losses": state.losses_today,
            "pnl": round(state.daily_pnl, 4),
        },
        "all_time": {
            "trades": _history.get("total_trades", 0),
            "wins": _history.get("wins", 0),
            "losses": _history.get("losses", 0),
            "pnl": round(_history.get("total_pnl", 0.0), 4),
        },
        "positions": positions,
        "history": [
            {
                "side": h["side"],
                "mode": h["mode"],
                "entry": h["entry_price"],
                "exit": h["exit_price"],
                "qty": h["shares"],
                "pnl": round(h["pnl"], 4),
                "result": h["result"],
                "reason": h["exit_reason"],
                "hold": int(h["closed_at"] - h["opened_at"]) if h["closed_at"] and h["opened_at"] else 0,
                "closed_ago": int(now - h["closed_at"]) if h["closed_at"] else 0,
                "order_id": h["order_id"],
            }
            for h in _recent_closed
        ],
        "uptime": int(now - _start_time),
    }


async def main_loop() -> None:
    global _history, _start_time, _recent_closed
    _start_time = time.time()

    loop = asyncio.get_event_loop()

    # Ensure the positions + logs tables exist, then load historical stats via SQL
    await loop.run_in_executor(None, db.init_schema)

    # Restore the dashboard's Live Log from previous runs so it isn't empty on
    # restart (core logs are sparse; persisting them keeps continuity).
    try:
        prior_logs = await loop.run_in_executor(None, db.get_recent_logs)
        if prior_logs:
            web_server.preload_logs(prior_logs)
            log.info(f"已从 MySQL 恢复 {len(prior_logs)} 条历史日志")
    except Exception as exc:
        log.debug(f"历史日志加载失败: {exc}")

    _history = await loop.run_in_executor(None, db.get_stats)
    log.info(
        f"历史记录已加载: 共{_history['total_trades']}笔, "
        f"W={_history['wins']} L={_history['losses']} "
        f"PnL={_history['total_pnl']:+.2f}"
    )

    # Sync RiskState with today's history
    state = get_state()
    state.trades_today = _history.get("today_trades", 0)
    state.wins_today = _history.get("today_wins", 0)
    state.losses_today = _history.get("today_losses", 0)
    state.daily_pnl = _history.get("today_pnl", 0.0)

    # Restore any positions still OPEN in the DB (e.g. after a restart)
    state.open_positions = await loop.run_in_executor(None, db.load_open_positions)
    if state.open_positions:
        log.info(f"已从 MySQL 恢复 {len(state.open_positions)} 个未平仓仓位")

    _recent_closed = await loop.run_in_executor(None, db.get_recent_closed, None)

    # Start web server
    await web_server.start_web_server()

    # Start BTC price feed
    await btc_price.start_feed()

    async with create_session() as session:
        asyncio.create_task(run_scanner(session), name="market_scanner")
        await asyncio.sleep(3.0)
        log.info("主循环已启动")

        loop_count = 0
        while True:
            loop_count += 1

            markets = get_active_markets()

            if not markets:
                web_server.update_state(_build_web_state())
                await web_server.broadcast()
                await asyncio.sleep(5.0)
                continue

            try:
                tasks = [evaluate_market(session, m) for m in markets.values()]
                await asyncio.gather(*tasks, return_exceptions=True)
                await order_manager.check_early_exits(session)
                await order_manager.resolve_expired_positions()
                # Full closed-history is only for the dashboard — refresh it
                # every ~5 loops instead of every loop to keep the hot path lean.
                if loop_count % 5 == 1:
                    _recent_closed = await loop.run_in_executor(None, db.get_recent_closed, None)
            except (ConnectionResetError, aiohttp.ClientError, OSError) as exc:
                log.warning(f"网络异常，跳过本轮循环: {exc}")

            # Update web dashboard
            # Re-compute all_time to include new trades from this session
            _history["total_trades"] = (
                _history.get("total_trades", 0)
                + max(0, state.trades_today - _history.get("today_trades", 0))
            )
            _history["wins"] = (
                _history.get("wins", 0)
                + max(0, state.wins_today - _history.get("today_wins", 0))
            )
            _history["losses"] = (
                _history.get("losses", 0)
                + max(0, state.losses_today - _history.get("today_losses", 0))
            )
            _history["total_pnl"] = (
                _history.get("total_pnl", 0.0)
                + (state.daily_pnl - _history.get("today_pnl", 0.0))
            )
            # Sync today snapshot
            _history["today_trades"] = state.trades_today
            _history["today_wins"] = state.wins_today
            _history["today_losses"] = state.losses_today
            _history["today_pnl"] = state.daily_pnl

            web_server.update_state(_build_web_state())
            await web_server.broadcast()

            # Clean up expired traded markets
            active_ids = set(markets.keys())
            _traded_markets.difference_update(_traded_markets - active_ids)

            # Evict stale target cache entries (keep only last 10 minutes)
            cache_cutoff = int(time.time()) - 600
            for k in [k for k in _target_cache if k < cache_cutoff]:
                del _target_cache[k]

            await asyncio.sleep(2.0)


def main() -> None:
    setup_logging()
    attach_web_logger()

    mode = "PAPER" if PAPER_TRADING else "LIVE"
    log.info(f"Polymarket BTC 5分钟机器人启动中 ({mode} 模式)")

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        state = get_state()
        w, l = state.wins_today, state.losses_today
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        log.info(
            f"已关闭 | 交易数={state.trades_today} "
            f"W={w} L={l} ({wr:.0f}%) | PnL={state.daily_pnl:+.2f} USD"
        )


if __name__ == "__main__":
    main()
