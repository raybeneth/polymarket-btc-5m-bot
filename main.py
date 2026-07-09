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
from typing import Optional

import aiohttp

from config import (
    PAPER_TRADING,
    SCORE_WEIGHTS,
    MIN_COMBINED_SCORE,
    MIN_TIMING_FILTER_SCORE,
    MIN_TIMING_FILTER_SCORE_ENABLED,
    MIN_SPREAD_FILTER_SCORE,
    TRADE_SIZE_USD,
    TRADE_SIZE_LOW_TIER_RATIO,
    DIRECTION_CONSISTENCY_HIGH,
    DIRECTION_CONSISTENCY_LOW,
    DIRECTION_CONSISTENCY_FLOOR,
    DIRECTION_MED_MIN_SCORE,
    DIRECTION_HIGH_MIN_SCORE,
    STOP_LOSS_RATIO,
    ACCOUNT_BALANCE_USD,
    ENTRY_BTC_MAX_VELOCITY,
    ENTRY_BTC_DECEL_RATIO,
    ENTRY_MAX_WAIT_SECONDS,
    ENTRY_CONFIRM_MAX_ADVERSE_PCT,
    TIMING_MIN_REMAINING,
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

# Signals pending the 45-second entry delay confirmation.
# market_id → (signal_unix_ts, TradeDecision, original_price_at_signal)
_pending_signals: dict[str, tuple[float, "TradeDecision", float]] = {}

# Historical stats (loaded once at startup)
_history: dict = {}
_start_time: float = 0.0

# Most recently closed positions, newest first (refreshed each loop cycle)
_recent_closed: list = []

# Cache of btc_target per 5-min window_start timestamp, so the dashboard
# target price doesn't jitter as ticks enter/leave the rolling buffer.
_target_cache: dict[int, float] = {}

# Last time the direction-filter skip/tier funnel summary was printed.
_last_stats_report: float = 0.0
STATS_REPORT_INTERVAL: float = 300.0  # 5 minutes

# Live mode's wallet USDC balance, polled periodically (see main_loop) — paper
# mode uses the DB-backed RiskState.balance instead, no polling needed.
_wallet_balance: Optional[float] = None


def _btc_entry_ready() -> tuple[bool, str]:
    """
    Check whether BTC momentum has decelerated enough for safe entry.

    Two conditions — either one passing allows entry:
      A) Flat:  |BTC change over last 20s| < ENTRY_BTC_MAX_VELOCITY (0.1%)
      B) Decel: per-second rate over 20s < ENTRY_BTC_DECEL_RATIO (40%) × rate over 60s

    Falls back to True (enter immediately) when price history is insufficient.
    """
    btc_now = btc_price.get_latest()
    if not btc_now:
        return True, "无BTC数据，直接入场"

    btc_20s = btc_price.get_price_n_seconds_ago(20)
    if not btc_20s:
        return True, "历史数据不足，直接入场"

    vel_20s = abs(btc_now - btc_20s) / btc_20s  # total % change over 20s (unsigned)

    # Condition A: price is flat
    if vel_20s < ENTRY_BTC_MAX_VELOCITY:
        return True, f"BTC近20s平稳 {vel_20s:.3%}"

    # Condition B: momentum rate is decelerating vs 60s average
    btc_60s = btc_price.get_price_n_seconds_ago(60)
    if btc_60s:
        vel_60s = abs(btc_now - btc_60s) / btc_60s
        rate_20s = vel_20s / 20   # % per second
        rate_60s = vel_60s / 60   # % per second
        if rate_60s > 0 and (rate_20s / rate_60s) < ENTRY_BTC_DECEL_RATIO:
            return True, (
                f"动量减速至 {rate_20s/rate_60s:.0%} "
                f"(20s:{vel_20s:.3%} / 60s:{vel_60s:.3%})"
            )

    return False, f"动量过强 {vel_20s:.3%}，继续等待"


async def evaluate_market(session: aiohttp.ClientSession, market) -> None:
    """Run the complete filter pipeline for one market."""
    market_id = market.market_id
    mid = market_id[:10]

    if market_id in _traded_markets:
        return
    state = get_state()
    if state.position_for_market(market_id):
        return

    # ── Strategy 1: BTC momentum deceleration confirmation ────────────────
    # Every ~2s the loop re-enters here for any market with a pending signal.
    # Each cycle:
    #   1. Re-fetch order book → check token price adverse move (cancel if >1.5% drop)
    #   2. Check BTC momentum deceleration (_btc_entry_ready)
    #      → enter if BTC is flat OR momentum rate has peaked (< 40% of 60s rate)
    #   3. Hard ceiling: enter regardless once ENTRY_MAX_WAIT_SECONDS (60s) elapsed
    #      (prevents the market window from expiring while we wait)
    if market_id in _pending_signals:
        signal_ts, pending_decision, original_price = _pending_signals[market_id]
        waited = time.time() - signal_ts
        side2 = pending_decision.side

        # Re-fetch book every cycle — replaces the normal per-market fetch for
        # pending signals, so network overhead is unchanged vs the old pipeline.
        yes_book2, no_book2 = await fetch_both_books(session, market)
        if yes_book2 is None or no_book2 is None:
            if waited >= ENTRY_MAX_WAIT_SECONDS:
                del _pending_signals[market_id]
                log.info(f"[{mid}] 确认超时: 订单簿不可用，放弃入场")
            return

        book2 = yes_book2 if side2 == "YES" else no_book2
        current_price = book2.best_ask
        if current_price is None:
            if waited >= ENTRY_MAX_WAIT_SECONDS:
                del _pending_signals[market_id]
                log.info(f"[{mid}] 确认超时: 无卖一价，放弃入场")
            return

        price_chg_pct = (current_price - original_price) / original_price

        # Token price adverse move → cancel immediately regardless of BTC state
        if price_chg_pct < -ENTRY_CONFIRM_MAX_ADVERSE_PCT:
            del _pending_signals[market_id]
            log.info(
                f"[{mid}] 逆向取消 ({waited:.0f}s): 代币价格 {price_chg_pct:+.1%} "
                f"(信号={original_price:.3f} 现={current_price:.3f})"
            )
            return

        # Momentum check + hard ceiling
        mom_ready, mom_reason = _btc_entry_ready()
        force_enter = waited >= ENTRY_MAX_WAIT_SECONDS
        if not mom_ready and not force_enter:
            log.debug(
                f"[{mid}] 等待动量 ({waited:.0f}s): {mom_reason} | "
                f"代币变动 {price_chg_pct:+.1%}"
            )
            return

        # Enter: momentum decelerating or max wait reached
        del _pending_signals[market_id]
        reversed_, rev_details = direction.check_reversal(side2, time.time())
        if reversed_:
            direction.record_skip("reversal_guard")
            log.debug(f"[{mid}] 动量就绪后反转哨兵拦截: {side2} move={rev_details.get('move_pct')}%")
            return

        pending_decision.entry_price = current_price
        pending_decision.tick_size = book2.tick_size
        pending_decision.min_order_size = book2.min_order_size or 0.0
        pending_decision.neg_risk = bool(book2.neg_risk)
        entry_reason = f"超时{waited:.0f}s" if force_enter else mom_reason
        log.info(
            f"[{mid}] 动量确认入场 ({waited:.0f}s | {entry_reason}): "
            f"代币变动 {price_chg_pct:+.1%} "
            f"(信号={original_price:.3f} 现={current_price:.3f})"
        )
        filled = await order_manager.execute(pending_decision)
        if filled:
            _traded_markets.add(market_id)
        return
    # ── End Strategy 1 pending handler ────────────────────────────────────

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
    raw_dir = abs(dir_result.details.get("score_raw", 0))
    strong_signal = dir_result.score >= 25.0  # raw ≥ ~40

    # Gate: two hard skips remain — true noise (consistency) and near-zero
    # magnitude (direction filter_score). Everything else proceeds and gets
    # sized by confidence tier below (LOW/MED/HIGH) instead of being rejected
    # outright — the old consistency<50% hard floor and the dir_score
    # sub-thresholds were redundant re-checks of the same signal, producing a
    # cliff effect without adding information (see docs/optimizations/008).
    if consistency < DIRECTION_CONSISTENCY_FLOOR:
        direction.record_skip("noise_skip")
        log.debug(f"[{mid}] 跳过噪声: consistency={consistency:.0%} < {DIRECTION_CONSISTENCY_FLOOR:.0%} (dir={raw_dir:.0f})")
        return

    # Near-zero direction magnitude must not fire regardless of how high
    # odds/timing are — odds(25%)+timing(30%) alone can reach 55 combined
    # score, above MIN_COMBINED_SCORE(45), so a hard floor here is the only
    # thing that actually stops a zero-edge trade (see docs/optimizations/010,
    # PAPER-93332164: dir=0.5, combined=50.48, still lost -$154 at LOW tier).
    if dir_result.score < DIRECTION_MED_MIN_SCORE:
        direction.record_skip("weak_skip")
        log.debug(
            f"[{mid}] 跳过方向弱信号: filter_score={dir_result.score:.1f} < {DIRECTION_MED_MIN_SCORE} "
            f"(raw={raw_dir:.0f}, consistency={consistency:.0%})"
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
    if MIN_TIMING_FILTER_SCORE_ENABLED and timing_result.score < MIN_TIMING_FILTER_SCORE:
        log.debug(f"[{mid}] 跳过时机下限: {timing_result.score:.0f} < {MIN_TIMING_FILTER_SCORE:.0f}")
        return

    # Filter 4: Spread (pass/fail gate + score floor)
    spread_result = spread.check(book, side)
    if not spread_result.passed:
        log.debug(f"[{mid}] 跳过价差: {spread_result.reason}")
        return
    if spread_result.score < MIN_SPREAD_FILTER_SCORE:
        log.debug(
            f"[{mid}] 跳过价差评分过低: {spread_result.score:.0f} < {MIN_SPREAD_FILTER_SCORE:.0f} "
            f"(流动性差/价差过宽)"
        )
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

    # Confidence tier → trade size. Only noise (checked above) is a hard skip;
    # every other passed signal fires, sized by how much it's trusted.
    # HIGH requires direction itself to be genuinely strong (filter≥DIRECTION_HIGH_MIN_SCORE=40)
    # AND persistent (consistency≥80%), not just odds+timing carrying the combined score.
    # The old floor of 20 allowed direction=23 (barely above minimum) to trigger full
    # $1000 sizing when timing+odds were high — 14 consecutive losses traced to this
    # exact pattern (see docs/optimizations/011). DIRECTION_HIGH_MIN_SCORE=40 means
    # the raw direction signal must reach ≥52/100 before a full position fires.
    #
    # MED also requires a small magnitude floor (DIRECTION_MED_MIN_SCORE) in
    # addition to consistency/strong_signal — consistency alone only measures
    # whether the SIGN has persisted, not whether the signal has any real
    # strength. Without this floor, a near-zero dir_result.score that happens
    # to read consistency≥50% gets the same 50% sizing as a genuinely moderate
    # signal (see docs/optimizations/009 — PAPER-25393563, dir_score=1.9,
    # consistency=64% → MED $5, lost -4.27). Falls through to LOW instead.
    if (combined_score >= 60 and dir_result.score >= DIRECTION_HIGH_MIN_SCORE
            and consistency >= DIRECTION_CONSISTENCY_HIGH):
        trade_size = TRADE_SIZE_USD                        # 100%
        tier = "HIGH"
    elif consistency >= DIRECTION_CONSISTENCY_LOW or strong_signal:
        # dir_result.score >= DIRECTION_MED_MIN_SCORE is guaranteed here — the
        # weak_skip hard floor above already rejected anything below it.
        trade_size = TRADE_SIZE_USD * 0.5                  # 50%
        tier = "MED"
    else:
        trade_size = TRADE_SIZE_USD * TRADE_SIZE_LOW_TIER_RATIO  # 25%
        tier = "LOW"
    direction.record_skip(tier.lower())

    # Reversal guard — last-look freshness check right before order placement.
    # Distinct from the 60s consistency window above: this only asks "has price
    # already moved against us in the last few seconds since we computed the
    # signal?" Matters more now that STOP_LOSS is disabled — a stale entry
    # rides to full settlement instead of exiting at a partial loss.
    reversed_, rev_details = direction.check_reversal(side, time.time())
    if reversed_:
        direction.record_skip("reversal_guard")
        log.debug(
            f"[{mid}] 反转哨兵拦截: side={side} move={rev_details.get('move_pct')}% "
            f"threshold={rev_details.get('threshold_pct')}%"
        )
        return

    # TRADE SIGNAL
    token_id = market.yes_token_id if side == "YES" else market.no_token_id
    decision = TradeDecision(
        should_trade=True, side=side, market_id=market_id, token_id=token_id,
        entry_price=entry_price, combined_score=combined_score,
        filter_results=filter_results, market_end_ts=market.end_timestamp,
        trade_size_usd=trade_size,
        tick_size=book.tick_size, min_order_size=book.min_order_size or 0.0,
        neg_risk=bool(book.neg_risk),
    )

    payout = 1.0 / entry_price if entry_price > 0 else 0
    scores_str = " | ".join(f"{k[:3]}={v.score:.0f}" for k, v in filter_results.items())
    log.info(
        f"SIGNAL {'^ ' if side == 'YES' else 'v '}{side} @ {entry_price:.3f} "
        f"x{payout:.2f} score={combined_score:.1f} [{scores_str}] "
        f"consist={consistency:.0%} "
        f"tier={tier} ${trade_size:.2f} "
        f"t={elapsed:.0f}s/{remaining:.0f}s"
    )

    # ── Strategy 1: BTC momentum confirmation ─────────────────────────────
    # Park the signal for momentum-based confirmation if there is enough time
    # budget. Minimum wait budget = 10s (5 evaluation cycles) to be meaningful;
    # below that, execute immediately rather than enter right before expiry.
    wait_budget = remaining - TIMING_MIN_REMAINING - 15
    if wait_budget >= 10:
        _pending_signals[market_id] = (time.time(), decision, entry_price)
        log.info(
            f"[{mid}] 信号挂起，等待BTC动量衰减 "
            f"(剩余={remaining:.0f}s, 最长等待={min(ENTRY_MAX_WAIT_SECONDS, wait_budget):.0f}s)"
        )
        return
    # Time budget too tight — skip momentum wait and enter immediately
    log.info(f"[{mid}] 剩余时间不足({remaining:.0f}s)，跳过动量确认直接入场")
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

    # Cumulative P&L over time for the dashboard's Profit/Loss chart.
    # _recent_closed is newest-first (closed_at DESC); walk it in chronological
    # order to build a running total, one point per closed trade. Two trades
    # closing in the same second collapse into one point (last value wins) —
    # the chart library requires strictly increasing, unique timestamps.
    pnl_series = []
    running = 0.0
    for h in reversed(_recent_closed):
        running += h["pnl"] or 0.0
        ts = h["closed_at"] or h["opened_at"]
        if not ts:
            continue
        ts = int(ts)
        if pnl_series and pnl_series[-1]["t"] == ts:
            pnl_series[-1]["v"] = round(running, 4)
        else:
            pnl_series.append({"t": ts, "v": round(running, 4)})

    return {
        "btc_price": btc_price.get_latest(),
        "btc_connected": btc_price.is_connected(),
        "btc_history": btc_price.get_chart_series(),
        "btc_target": round(btc_target, 2) if btc_target is not None else None,
        "paper_trading": PAPER_TRADING,
        "account_balance": state.balance if PAPER_TRADING else _wallet_balance,
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
        "pnl_series": pnl_series,
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
                "opened_at": h["opened_at"],
                "order_id": h["order_id"],
            }
            for h in _recent_closed
        ],
        "uptime": int(now - _start_time),
    }


async def main_loop() -> None:
    global _history, _start_time, _recent_closed, _last_stats_report, _wallet_balance
    _start_time = time.time()
    _last_stats_report = _start_time

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

    # Paper-mode virtual balance: seeded from ACCOUNT_BALANCE_USD only on the
    # very first-ever startup — the DB row is authoritative after that, so
    # restarts don't reset it back to the starting value.
    state.balance = await loop.run_in_executor(None, db.get_or_init_balance, ACCOUNT_BALANCE_USD)
    log.info(f"账户余额已加载: ${state.balance:.2f}")

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
        if not PAPER_TRADING:
            await order_manager.reconcile_positions(session)
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
                # Live mode's dashboard balance is the real wallet — a network
                # call, so poll it far less often than the hot path (~every 15
                # loops / 30s). Paper mode never touches this; its balance is
                # the DB-backed RiskState.balance, updated inline on open/close.
                if not PAPER_TRADING and loop_count % 15 == 1:
                    _wallet_balance = await order_manager.get_funder_onchain_balance_usdc(session)
                    if _wallet_balance is None:
                        _wallet_balance = await loop.run_in_executor(None, order_manager.get_wallet_balance_usdc)
                # Periodic on-chain reconciliation (live mode only) — catches desync
                # from partial fills, crashes mid-order, or unconfirmed sells that
                # this loop's own bookkeeping can't detect on its own.
                if not PAPER_TRADING and loop_count % 30 == 1:
                    await order_manager.reconcile_positions(session)
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

            # Clean up pending signals whose market has already expired
            stale_pending = [m for m in _pending_signals if m not in active_ids]
            for m in stale_pending:
                log.debug(f"清理过期挂起信号: {m[:10]}")
                del _pending_signals[m]

            # Evict stale target cache entries (keep only last 10 minutes)
            cache_cutoff = int(time.time()) - 600
            for k in [k for k in _target_cache if k < cache_cutoff]:
                del _target_cache[k]

            # Periodic funnel stats (NEUTRAL/noise/weak/reversal skips + tier
            # fills). Kept at log.debug — useful for trade-frequency tuning when
            # you raise LOG_LEVEL to DEBUG, but too noisy for normal INFO runs.
            now_ts = time.time()
            if now_ts - _last_stats_report >= STATS_REPORT_INTERVAL:
                stats = direction.get_and_reset_skip_stats()
                fired = stats["low"] + stats["med"] + stats["high"]
                log.debug(
                    f"方向漏斗(近{STATS_REPORT_INTERVAL/60:.0f}分钟): "
                    f"NEUTRAL={stats['neutral']} 噪声跳过={stats['noise_skip']} "
                    f"弱信号跳过={stats['weak_skip']} 反转拦截={stats['reversal_guard']} | "
                    f"成交 LOW={stats['low']} MED={stats['med']} HIGH={stats['high']} (共{fired}笔)"
                )
                _last_stats_report = now_ts

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
