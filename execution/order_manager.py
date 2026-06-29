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
from utils.logger import log_trade

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
        log.warning(f"[PAPER] Risk block: {reason}")
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
        f"[PAPER] BUY {decision.side} | "
        f"market={decision.market_id[:12]}… | "
        f"price={simulated_fill_price:.4f} | "
        f"shares={shares:.2f} | "
        f"score={decision.combined_score:.1f}"
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
    return True


# ─────────────────────────────────────────────
# Live trading
# ─────────────────────────────────────────────

async def _live_execute(decision: TradeDecision) -> bool:
    """Place a real limit order on Polymarket CLOB."""
    state = get_state()
    ok, reason = state.can_trade()
    if not ok:
        log.warning(f"[LIVE] Risk block: {reason}")
        return False

    client = _get_clob_client()
    limit_price = round(decision.entry_price + ORDER_PRICE_SLIPPAGE, 4)

    # Calculate shares from USD budget
    shares = round(TRADE_SIZE_USD / limit_price, 2)

    log.info(
        f"[LIVE] Submitting BUY {decision.side} | "
        f"token={decision.token_id[:12]}… | "
        f"limit={limit_price:.4f} | shares={shares:.2f}"
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
        log.error(f"[LIVE] Order submission failed: {exc}")
        return False

    order_id = response.get("orderID") or response.get("id", "unknown")
    log.info(f"[LIVE] Order submitted: {order_id}")

    # Poll for fill or timeout
    filled_price = await _wait_for_fill(client, order_id, limit_price)
    if filled_price is None:
        log.warning(f"[LIVE] Order {order_id} not filled in {ORDER_FILL_TIMEOUT_SECONDS}s — cancelling")
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

    In paper mode we use a simple heuristic; in live mode Polymarket
    settles automatically and we just update our local state.
    """
    state = get_state()
    now = time.time()

    for pos in list(state.open_positions):
        # Heuristic: if position has been open for >6 minutes, market is done
        age = now - pos.opened_at
        if age < 360:
            continue

        # In paper mode, we don't know the outcome — log as unknown
        # In live mode, query the CLOB for the resolved price
        if PAPER_TRADING:
            log.info(
                f"[PAPER] Position {pos.order_id} expired (age={age:.0f}s) "
                f"— outcome unknown in paper mode"
            )
            state.record_close(pos.market_id, exit_price=0.5)  # assume breakeven
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
                    f"[LIVE] Closed expired position {pos.order_id} "
                    f"@ {exit_price:.3f} pnl={pnl:+.4f}"
                )
            except Exception as exc:
                log.warning(f"Could not resolve position {pos.order_id}: {exc}")
