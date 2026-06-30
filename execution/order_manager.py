"""
Order manager — places and tracks limit orders on Polymarket CLOB.

Two modes:
  PAPER_TRADING=true  → logs trade intent, simulates fill at entry price
  PAPER_TRADING=false → submits real order via py-clob-client

Order strategy:
  - Limit order at best_ask + ORDER_PRICE_SLIPPAGE (small buffer to improve fill chance)
  - GTC with manual cancel after ORDER_FILL_TIMEOUT_SECONDS
  - On rejection / timeout → cancel and skip
"""
import asyncio
import logging
import time
import uuid
from typing import Optional

from config import (
    PAPER_TRADING,
    PRIVATE_KEY,
    API_KEY,
    API_SECRET,
    API_PASSPHRASE,
    POLYMARKET_HOST,
    CHAIN_ID,
    TRADE_SIZE_USD,
    ORDER_PRICE_SLIPPAGE,
    ORDER_FILL_TIMEOUT_SECONDS,
    TradeDecision,
)
from execution.risk import OpenPosition, get_state
from utils.logger import log_trade, log_position_open, log_position_close

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CLOB client (lazy init, only when live)
# ─────────────────────────────────────────────
_clob_client = None


def _get_clob_client():
    global _clob_client
    if _clob_client is None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=API_KEY,
            api_secret=API_SECRET,
            api_passphrase=API_PASSPHRASE,
        )
        _clob_client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
            creds=creds,
        )
    return _clob_client


# ─────────────────────────────────────────────
# Paper trading
# ─────────────────────────────────────────────

async def _paper_execute(decision: TradeDecision) -> bool:
    """Simulate a fill and record a paper position."""
    state = get_state()
    ok, reason = state.can_trade()
    if not ok:
        log.warning(f"[模拟] 风控拦截: {reason}")
        return False

    simulated_fill_price = decision.entry_price + ORDER_PRICE_SLIPPAGE
    shares = TRADE_SIZE_USD / simulated_fill_price

    order_id = f"PAPER-{uuid.uuid4().hex[:8]}"
    pos = OpenPosition(
        market_id=decision.market_id,
        token_id=decision.token_id,
        side=decision.side,
        entry_price=simulated_fill_price,
        shares=shares,
        opened_at=time.time(),
        order_id=order_id,
    )
    state.record_open(pos)

    log.info(
        f"[模拟] 买入 {decision.side} | "
        f"市场={decision.market_id[:12]}… | "
        f"价格={simulated_fill_price:.4f} | "
        f"数量={shares:.2f} | "
        f"评分={decision.combined_score:.1f}"
    )

    log_trade({
        "mode": "paper",
        "order_id": order_id,
        "market_id": decision.market_id,
        "side": decision.side,
        "token_id": decision.token_id,
        "entry_price": simulated_fill_price,
        "shares": shares,
        "usd_size": TRADE_SIZE_USD,
        "combined_score": decision.combined_score,
        "filter_scores": {k: v.score for k, v in decision.filter_results.items()},
        "timestamp": time.time(),
    })
    log_position_open(
        order_id=order_id,
        market_id=decision.market_id,
        side=decision.side,
        entry_price=simulated_fill_price,
        shares=shares,
        score=decision.combined_score,
        mode="模拟",
    )
    return True


# ─────────────────────────────────────────────
# Live trading
# ─────────────────────────────────────────────

async def _live_execute(decision: TradeDecision) -> bool:
    """Place a real limit order on Polymarket CLOB."""
    state = get_state()
    ok, reason = state.can_trade()
    if not ok:
        log.warning(f"[实盘] 风控拦截: {reason}")
        return False

    client = _get_clob_client()
    limit_price = round(decision.entry_price + ORDER_PRICE_SLIPPAGE, 4)

    # Calculate shares from USD budget
    shares = round(TRADE_SIZE_USD / limit_price, 2)

    log.info(
        f"[实盘] 提交买单 {decision.side} | "
        f"代币={decision.token_id[:12]}… | "
        f"限价={limit_price:.4f} | 数量={shares:.2f}"
    )

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        order_args = OrderArgs(
            token_id=decision.token_id,
            price=limit_price,
            size=shares,
            side="BUY",
        )
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.create_and_post_order(order_args),
        )
    except Exception as exc:
        log.error(f"[实盘] 订单提交失败: {exc}")
        return False

    order_id = response.get("orderID") or response.get("id", "unknown")
    log.info(f"[实盘] 订单已提交: {order_id}")

    # Poll for fill or timeout
    filled_price = await _wait_for_fill(client, order_id, limit_price)
    if filled_price is None:
        log.warning(f"[实盘] 订单 {order_id} 在 {ORDER_FILL_TIMEOUT_SECONDS}s 内未成交 — 正在撤单")
        await asyncio.get_event_loop().run_in_executor(None, lambda: client.cancel(order_id))
        return False

    pos = OpenPosition(
        market_id=decision.market_id,
        token_id=decision.token_id,
        side=decision.side,
        entry_price=filled_price,
        shares=shares,
        opened_at=time.time(),
        order_id=order_id,
    )
    state.record_open(pos)

    log_trade({
        "mode": "live",
        "order_id": order_id,
        "market_id": decision.market_id,
        "side": decision.side,
        "token_id": decision.token_id,
        "entry_price": filled_price,
        "shares": shares,
        "usd_size": TRADE_SIZE_USD,
        "combined_score": decision.combined_score,
        "filter_scores": {k: v.score for k, v in decision.filter_results.items()},
        "timestamp": time.time(),
    })
    log_position_open(
        order_id=order_id,
        market_id=decision.market_id,
        side=decision.side,
        entry_price=filled_price,
        shares=shares,
        score=decision.combined_score,
        mode="实盘",
    )
    return True


async def _wait_for_fill(client, order_id: str, limit_price: float) -> Optional[float]:
    """Poll order status until filled or timeout. Returns fill price or None."""
    deadline = time.time() + ORDER_FILL_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            status = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_order(order_id)
            )
            if status.get("status") == "MATCHED":
                return float(status.get("price", limit_price))
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

async def execute(decision: TradeDecision) -> bool:
    """Route to paper or live execution based on config."""
    if PAPER_TRADING:
        return await _paper_execute(decision)
    return await _live_execute(decision)


async def resolve_expired_positions() -> None:
    """
    Called periodically. Checks open positions whose market has expired
    and closes them at the settlement price (1.0 if won, 0.0 if lost).

    In paper mode we use BTC price change during the market window to determine
    the outcome (Up = BTC price went up, Down = BTC price went down).
    In live mode Polymarket settles automatically and we just update our local state.
    """
    from feeds import btc_price

    state = get_state()
    now = time.time()

    for pos in list(state.open_positions):
        # Heuristic: if position has been open for >6 minutes, market is done
        age = now - pos.opened_at
        if age < 360:
            continue

        if PAPER_TRADING:
            # Determine outcome from BTC price movement during the 5-min window
            # The market window started ~5min before the position's market expired
            # Use the BTC prices we have: price at entry vs price 5 min later
            price_at_entry = btc_price.get_price_n_seconds_ago(int(age))
            price_at_end = btc_price.get_price_n_seconds_ago(int(age - 300))

            if price_at_entry is not None and price_at_end is not None:
                btc_went_up = price_at_end >= price_at_entry
                # side "YES" = bought Up token, side "NO" = bought Down token
                won = (pos.side == "YES" and btc_went_up) or (pos.side == "NO" and not btc_went_up)
                exit_price = 1.0 if won else 0.0
                outcome_str = "UP" if btc_went_up else "DOWN"
                result_str = "WIN" if won else "LOSS"
            else:
                # Not enough price history — assume breakeven
                exit_price = 0.5
                outcome_str = "unknown"
                result_str = "unknown"

            pnl = state.record_close(pos.market_id, exit_price)
            log.info(
                f"[模拟] 仓位 {pos.order_id} 已结算 — "
                f"BTC={outcome_str} 方向={pos.side} 结果={result_str} 盈亏={pnl:+.4f}"
            )
            log_position_close(
                order_id=pos.order_id,
                market_id=pos.market_id,
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                shares=pos.shares,
                pnl=pnl if pnl is not None else 0.0,
                result=result_str,
                mode="模拟",
            )
        else:
            try:
                client = _get_clob_client()
                # Query last trade price as proxy for settlement
                trades = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.get_last_trade_price(pos.token_id),
                )
                exit_price = float(trades.get("price", 0.5))
                pnl = state.record_close(pos.market_id, exit_price)
                log.info(
                    f"[实盘] 已平仓过期仓位 {pos.order_id} "
                    f"@ {exit_price:.3f} 盈亏={pnl:+.4f}"
                )
                result_str = "WIN" if (pnl and pnl > 0) else "LOSS"
                log_position_close(
                    order_id=pos.order_id,
                    market_id=pos.market_id,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    shares=pos.shares,
                    pnl=pnl if pnl is not None else 0.0,
                    result=result_str,
                    mode="实盘",
                )
            except Exception as exc:
                log.warning(f"无法结算仓位 {pos.order_id}: {exc}")
