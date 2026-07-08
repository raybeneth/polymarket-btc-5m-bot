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

import aiohttp

from config import (
    PAPER_TRADING,
    PRIVATE_KEY,
    API_KEY,
    API_SECRET,
    API_PASSPHRASE,
    POLYMARKET_HOST,
    CHAIN_ID,
    FUNDER_ADDRESS,
    SIGNATURE_TYPE,
    POLYGON_RPC_URL,
    USDC_CONTRACT_ADDRESS,
    TRADE_SIZE_USD,
    ORDER_PRICE_SLIPPAGE,
    ORDER_FILL_TIMEOUT_SECONDS,
    STOP_LOSS_ENABLED,
    TAKE_PROFIT_ENABLED,
    STOP_LOSS_RATIO,
    TAKE_PROFIT_DELTA,
    FORCE_EXIT_BEFORE_EXPIRY,
    TradeDecision,
)
from execution.risk import OpenPosition, get_state
from storage import db
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
            signature_type=SIGNATURE_TYPE,
            funder=FUNDER_ADDRESS or None,
        )
    return _clob_client


def _round_to_tick(price: float, tick_size) -> float:
    """
    Round a price to the nearest valid increment for this market's tick size
    (e.g. "0.01" or "0.001"), as read from the live order book — Polymarket
    rejects any order whose price doesn't sit on that grid, and different
    markets use different tick sizes, so a blind round(x, 4) can silently
    produce an invalid price.
    """
    tick_str = str(tick_size) if tick_size else "0.01"
    try:
        tick = float(tick_str)
    except (TypeError, ValueError):
        tick = 0.01
        tick_str = "0.01"
    if tick <= 0:
        tick, tick_str = 0.01, "0.01"
    decimals = len(tick_str.split(".")[-1]) if "." in tick_str else 0
    rounded = round(round(price / tick) * tick, decimals)
    return min(max(rounded, tick), 1 - tick)


def get_wallet_balance_usdc() -> Optional[float]:
    """
    Fallback balance source via CLOB's /balance-allowance endpoint. Only used
    when get_funder_onchain_balance_usdc() can't run (no FUNDER_ADDRESS / RPC
    failure) — that endpoint resolves the checked address from the L1 signer +
    SIGNATURE_TYPE via Polymarket's own proxy-derivation logic, not from
    FUNDER_ADDRESS, so it reports the wrong (often empty) balance whenever
    PRIVATE_KEY is a delegated trading key rather than the funder's own key.
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = _get_clob_client()
        resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw = resp.get("balance") if isinstance(resp, dict) else None
        return float(raw) / 1_000_000 if raw is not None else None  # USDC = 6 decimals
    except Exception as exc:
        log.warning(f"[实盘] 查询钱包USDC余额失败: {exc}")
        return None


def _encode_balance_of_call(address: str) -> str:
    """ABI-encodes an ERC20 balanceOf(address) call for eth_call."""
    selector = "70a08231"
    padded = address.lower().removeprefix("0x").rjust(64, "0")
    return "0x" + selector + padded


async def _fetch_funder_onchain_balance(session: aiohttp.ClientSession) -> Optional[float]:
    if not FUNDER_ADDRESS:
        return None
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": USDC_CONTRACT_ADDRESS, "data": _encode_balance_of_call(FUNDER_ADDRESS)},
            "latest",
        ],
    }
    try:
        async with session.post(
            POLYGON_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
        result = body.get("result")
        if not result or result == "0x":
            return None
        return int(result, 16) / 1_000_000  # USDC = 6 decimals
    except Exception as exc:
        log.warning(f"[实盘] 链上查询钱包USDC余额失败: {exc}")
        return None


async def get_funder_onchain_balance_usdc(session: Optional[aiohttp.ClientSession] = None) -> Optional[float]:
    """
    Live mode's real balance: FUNDER_ADDRESS's actual USDC.e balance, read
    directly on-chain via a Polygon RPC eth_call — this is what
    polymarket.com's UI shows, and unlike get_wallet_balance_usdc() it doesn't
    depend on the CLOB backend correctly deriving the funder from the signer.
    Used both for the dashboard (main loop passes its long-lived `session`)
    and as the pre-trade sufficient-funds gate in _live_execute() (no session
    to hand in there, so one is created just for this call).
    Paper mode has its own DB-backed balance instead (execution.risk.RiskState.balance).
    """
    if session is not None:
        return await _fetch_funder_onchain_balance(session)
    from feeds.polymarket_clob import create_session
    async with create_session() as owned_session:
        return await _fetch_funder_onchain_balance(owned_session)


# ─────────────────────────────────────────────
# Paper trading
# ─────────────────────────────────────────────

async def _paper_execute(decision: TradeDecision) -> bool:
    """Simulate a fill and record a paper position."""
    state = get_state()
    actual_size = decision.trade_size_usd or TRADE_SIZE_USD
    ok, reason = state.can_trade(actual_size)
    if not ok:
        log.warning(f"[模拟] 风控拦截: {reason}")
        return False

    # Paper mode: fill at best_ask (decision.entry_price) without artificial
    # slippage. Adding ORDER_PRICE_SLIPPAGE in paper mode inflated entry cost
    # and therefore the stop-loss trigger price, causing earlier-than-realistic
    # stop-outs and masking the true signal quality (see docs/optimizations/011).
    # Real-mode still uses best_ask + slippage via limit order placement.
    simulated_fill_price = decision.entry_price
    shares = actual_size / simulated_fill_price

    order_id = f"PAPER-{uuid.uuid4().hex[:8]}"
    pos = OpenPosition(
        market_id=decision.market_id,
        token_id=decision.token_id,
        side=decision.side,
        entry_price=simulated_fill_price,
        shares=shares,
        opened_at=time.time(),
        order_id=order_id,
        market_end_ts=decision.market_end_ts,
        usd_size=actual_size,
    )
    state.record_open(pos)

    side_icon = "^" if decision.side == "YES" else "v"
    stop_target = simulated_fill_price - simulated_fill_price * STOP_LOSS_RATIO
    tp_target = simulated_fill_price + TAKE_PROFIT_DELTA
    log.info(
        f"OPEN {side_icon} {decision.side} @ {simulated_fill_price:.3f} "
        f"qty={shares:.2f} ${actual_size:.2f} "
        f"score={decision.combined_score:.1f} "
        f"止损Target={stop_target:.3f} 止盈Target={tp_target:.3f} {order_id}"
    )

    await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: db.insert_open(
            pos, "paper", actual_size, decision.combined_score,
            {k: v.score for k, v in decision.filter_results.items()},
        ),
    )
    await asyncio.get_event_loop().run_in_executor(None, lambda: db.update_balance(state.balance))
    log_trade({
        "mode": "paper",
        "order_id": order_id,
        "market_id": decision.market_id,
        "side": decision.side,
        "token_id": decision.token_id,
        "entry_price": simulated_fill_price,
        "shares": shares,
        "usd_size": actual_size,
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
    # negRisk markets use a different exchange contract; this bot's live path
    # has never been exercised against one, so refuse rather than assume it
    # works. BTC 5-min Up/Down markets are plain binary markets and should
    # always read neg_risk=False — this is a safety net, not expected to fire.
    if decision.neg_risk:
        log.warning(
            f"[实盘] 跳过: 市场为 negRisk 合约(neg_risk=True)，实盘下单尚未针对此类型验证 "
            f"token={decision.token_id[:12]}…"
        )
        return False

    state = get_state()
    # trade_size_usd=0 here deliberately skips can_trade()'s internal balance
    # check — that check compares against state.balance, which is the paper-
    # mode simulated ledger and has no meaning in live mode. Concurrent-
    # position / daily-loss / circuit-breaker gates still apply below.
    ok, reason = state.can_trade(0)
    if not ok:
        log.warning(f"[实盘] 风控拦截: {reason}")
        return False

    actual_size = decision.trade_size_usd or TRADE_SIZE_USD
    real_balance = await get_funder_onchain_balance_usdc()
    if real_balance is None:
        real_balance = await asyncio.get_event_loop().run_in_executor(None, get_wallet_balance_usdc)
    if real_balance is None:
        log.warning("[实盘] 无法查询钱包USDC余额，为安全起见跳过本次下单")
        return False
    if real_balance < actual_size:
        log.warning(f"[实盘] 钱包USDC余额不足: 现有{real_balance:.2f} < 需要{actual_size:.2f}")
        return False

    client = _get_clob_client()
    # Round to the market's real tick size (from the order book response) instead
    # of a blind 4-decimal round — Polymarket rejects prices off the tick grid,
    # and tick size varies by market (see feeds/polymarket_clob.py OrderBook).
    limit_price = _round_to_tick(decision.entry_price + ORDER_PRICE_SLIPPAGE, decision.tick_size)

    # Calculate shares from USD budget (dynamic size from confidence tier)
    shares = round(actual_size / limit_price, 2)

    min_size = decision.min_order_size or 0.0
    if min_size > 0 and shares < min_size:
        log.warning(
            f"[实盘] 跳过: 下单量 {shares:.2f} 低于该市场最小下单量 {min_size:.2f}"
        )
        return False

    log.info(
        f"[实盘] 提交买单 {decision.side} | "
        f"代币={decision.token_id[:12]}… | "
        f"限价={limit_price:.4f}(tick={decision.tick_size or '0.01'}) | "
        f"数量={shares:.2f} | ${actual_size:.2f}"
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

    # Poll for fill/partial-fill or timeout
    fill_price, filled_shares = await _wait_for_fill(client, order_id, limit_price, shares)
    if filled_shares <= 0:
        log.warning(f"[实盘] 订单 {order_id} 在 {ORDER_FILL_TIMEOUT_SECONDS}s 内未成交 — 正在撤单")
        await asyncio.get_event_loop().run_in_executor(None, lambda: client.cancel(order_id))
        return False

    if filled_shares < shares - 1e-6:
        log.warning(
            f"[实盘] 订单 {order_id} 仅部分成交: {filled_shares:.2f}/{shares:.2f} 份 — "
            f"撤销剩余未成交部分，按实际成交量建仓"
        )
        await asyncio.get_event_loop().run_in_executor(None, lambda: client.cancel(order_id))

    # Cost basis reflects what actually filled, not the originally requested
    # size — a partial fill should not be booked/risk-tracked as a full position.
    actual_cost = filled_shares * fill_price
    pos = OpenPosition(
        market_id=decision.market_id,
        token_id=decision.token_id,
        side=decision.side,
        entry_price=fill_price,
        shares=filled_shares,
        opened_at=time.time(),
        order_id=order_id,
        market_end_ts=decision.market_end_ts,
        usd_size=actual_cost,
    )
    state.record_open(pos)

    side_icon = "^" if decision.side == "YES" else "v"
    stop_target = fill_price - fill_price * STOP_LOSS_RATIO
    tp_target = fill_price + TAKE_PROFIT_DELTA
    log.info(
        f"OPEN {side_icon} {decision.side} @ {fill_price:.3f} "
        f"qty={filled_shares:.2f} ${actual_cost:.2f} "
        f"score={decision.combined_score:.1f} "
        f"止损Target={stop_target:.3f} 止盈Target={tp_target:.3f} {order_id}"
    )

    await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: db.insert_open(
            pos, "live", actual_cost, decision.combined_score,
            {k: v.score for k, v in decision.filter_results.items()},
        ),
    )
    log_trade({
        "mode": "live",
        "order_id": order_id,
        "market_id": decision.market_id,
        "side": decision.side,
        "token_id": decision.token_id,
        "entry_price": fill_price,
        "shares": filled_shares,
        "usd_size": actual_cost,
        "combined_score": decision.combined_score,
        "filter_scores": {k: v.score for k, v in decision.filter_results.items()},
        "timestamp": time.time(),
    })
    log_position_open(
        order_id=order_id,
        market_id=decision.market_id,
        side=decision.side,
        entry_price=fill_price,
        shares=filled_shares,
        score=decision.combined_score,
        mode="实盘",
    )
    return True


async def _wait_for_fill(
    client, order_id: str, limit_price: float, requested_size: float
) -> tuple[Optional[float], float]:
    """
    Poll order status until filled, partially filled, dead, or timeout.

    Returns (fill_price, filled_size):
      - filled_size == 0            → nothing matched (fill_price is None)
      - 0 < filled_size < requested  → partial fill; caller cancels the rest
      - filled_size >= requested     → fully matched

    The CLOB's own status strings are lowercase ("matched"/"live"/"delayed"/
    "unmatched"/"cancelled") — this used to compare against "MATCHED" (upper-
    case), which never matched, so every live fill was silently treated as a
    timeout and the (possibly already-filled) order was cancelled afterwards.
    """
    deadline = time.time() + ORDER_FILL_TIMEOUT_SECONDS
    size_matched = 0.0
    fill_price = limit_price
    while time.time() < deadline:
        try:
            status = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_order(order_id)
            )
            status_val = str(status.get("status", "")).lower()
            size_matched = float(status.get("size_matched", 0) or 0)
            fill_price = float(status.get("price", limit_price) or limit_price)
            if status_val == "matched" or size_matched >= requested_size - 1e-6:
                return fill_price, min(size_matched, requested_size)
            if status_val in ("unmatched", "cancelled", "canceled"):
                return (fill_price, size_matched) if size_matched > 0 else (None, 0.0)
        except Exception:
            pass
        await asyncio.sleep(1.0)
    # Timed out still "live" on the book — report whatever matched so far;
    # caller cancels the unfilled remainder.
    return (fill_price, size_matched) if size_matched > 0 else (None, 0.0)


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

async def execute(decision: TradeDecision) -> bool:
    """Route to paper or live execution based on config."""
    if PAPER_TRADING:
        return await _paper_execute(decision)
    return await _live_execute(decision)


# Live-mode exits in flight: pos.order_id -> {pos, sell_order_id, exit_reason,
# submitted_at}. A position stays in state.open_positions (i.e. still counted
# as real risk) until its SELL order is confirmed matched — closing it out
# the instant we submit the order would desync our book from the exchange
# the moment a sell fails to fill (illiquid market, price moved, etc.),
# leaving us thinking we're flat while still holding real shares.
_pending_exits: dict = {}


async def _submit_exit(state, pos, exit_reason: str, best_bid: float) -> None:
    """Submit a SELL order for a live position that hit an exit trigger and
    track it in _pending_exits until the fill is confirmed by _poll_pending_exits."""
    try:
        client = _get_clob_client()
        from py_clob_client.clob_types import OrderArgs
        sell_args = OrderArgs(
            token_id=pos.token_id,
            price=round(best_bid, 4),
            size=round(pos.shares, 2),
            side="SELL",
        )
        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.create_and_post_order(sell_args),
        )
        sell_order_id = response.get("orderID") or response.get("id", "unknown")
        _pending_exits[pos.order_id] = {
            "pos": pos,
            "sell_order_id": sell_order_id,
            "exit_reason": exit_reason,
            "submitted_at": time.time(),
        }
        log.info(f"[实盘] {exit_reason} 卖单已提交 {pos.order_id} -> {sell_order_id} @ {best_bid:.3f}")
    except Exception as exc:
        log.warning(f"[实盘] {exit_reason} 卖出提交失败 {pos.order_id}: {exc}")


async def _poll_pending_exits(state) -> None:
    """Check in-flight exit sell orders; only record_close/db.close_position
    once the exchange confirms the sell actually matched."""
    if not _pending_exits:
        return
    client = _get_clob_client()
    for key, info in list(_pending_exits.items()):
        pos = info["pos"]
        sell_order_id = info["sell_order_id"]
        exit_reason = info["exit_reason"]
        try:
            status = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_order(sell_order_id)
            )
            status_val = str(status.get("status", "")).lower()
            size_matched = float(status.get("size_matched", 0) or 0)
            fill_price = float(status.get("price", 0) or 0)
        except Exception as exc:
            log.warning(f"[实盘] 查询卖单状态失败 {sell_order_id}: {exc}")
            continue

        if status_val == "matched" or size_matched >= pos.shares - 1e-6:
            exit_price = fill_price if fill_price > 0 else pos.entry_price
            pnl = state.record_close(pos.market_id, exit_price)
            hold_time = int(time.time() - pos.opened_at)
            log.info(
                f"EXIT {exit_reason} {pos.side} "
                f"{pos.entry_price:.3f}->{exit_price:.3f} "
                f"pnl={pnl:+.2f} hold={hold_time}s {pos.order_id}"
            )
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: db.close_position(pos.order_id, exit_price, exit_reason)
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
                result=f"{result_str}({exit_reason})",
                mode="实盘",
            )
            del _pending_exits[key]
        elif status_val in ("unmatched", "cancelled", "canceled"):
            log.warning(
                f"[实盘] {exit_reason} 卖单未成交/被取消 {sell_order_id} — "
                f"仓位 {pos.order_id} 仍持有，下轮重新尝试平仓"
            )
            del _pending_exits[key]
        elif time.time() - info["submitted_at"] > ORDER_FILL_TIMEOUT_SECONDS * 3:
            log.warning(f"[实盘] 卖单 {sell_order_id} 长时间未成交，撤单后下轮重试")
            await asyncio.get_event_loop().run_in_executor(None, lambda: client.cancel(sell_order_id))
            del _pending_exits[key]


async def check_early_exits(session) -> None:
    """
    Check open positions for stop-loss / take-profit / force-exit triggers.
    Fetches current orderbook for each held token. The trigger judgment uses
    the book's mid-price (not raw best_bid) so a single-sided/stale best_bid
    quote can't fire the stop on its own; the actual exit fill still uses
    best_bid since that's the price we could realistically sell at. Also
    forces exit when remaining time before market settlement <= FORCE_EXIT_BEFORE_EXPIRY.
    """
    from feeds.polymarket_clob import fetch_order_book

    state = get_state()

    if not PAPER_TRADING:
        await _poll_pending_exits(state)

    if not state.open_positions:
        return

    for pos in list(state.open_positions):
        if not PAPER_TRADING and pos.order_id in _pending_exits:
            continue  # sell already submitted for this position, awaiting fill confirmation
        book = await fetch_order_book(session, pos.token_id)
        if book is None or book.best_bid is None:
            continue

        best_bid = book.best_bid
        # Use best_bid for BOTH trigger judgment and exit fill — previously
        # trigger used book.mid while exit used best_bid, which created a gap
        # of up to 10+ cents between when the stop fired and what we actually
        # received. In illiquid/fast-moving markets mid can be far above best_bid,
        # so mid-triggered stops routinely exited at much worse prices than intended
        # (see docs/optimizations/011, e.g. Trade 119: mid≈0.45 triggered but
        # best_bid=0.34 on exit → extra $107 loss vs expected stop).
        trigger_price = best_bid
        stop_delta = pos.entry_price * STOP_LOSS_RATIO
        stop_price = pos.entry_price - stop_delta
        tp_price = pos.entry_price + TAKE_PROFIT_DELTA

        # Check force exit before expiry
        now = time.time()
        force_exit = (
            pos.market_end_ts > 0
            and (pos.market_end_ts - now) <= FORCE_EXIT_BEFORE_EXPIRY
        )

        exit_reason = None
        if STOP_LOSS_ENABLED and trigger_price <= stop_price:
            exit_reason = "STOP_LOSS"
        elif TAKE_PROFIT_ENABLED and trigger_price >= tp_price:
            exit_reason = "TAKE_PROFIT"
        elif force_exit:
            exit_reason = "FORCE_EXIT"

        if exit_reason is None:
            continue

        exit_price = best_bid

        if PAPER_TRADING:
            pnl = state.record_close(pos.market_id, exit_price)
            hold_time = int(time.time() - pos.opened_at)
            log.info(
                f"EXIT {exit_reason} {pos.side} "
                f"{pos.entry_price:.3f}->{exit_price:.3f} "
                f"pnl={pnl:+.2f} hold={hold_time}s {pos.order_id}"
            )
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: db.close_position(pos.order_id, exit_price, exit_reason)
            )
            await asyncio.get_event_loop().run_in_executor(None, lambda: db.update_balance(state.balance))
            result_str = "WIN" if (pnl and pnl > 0) else "LOSS"
            log_position_close(
                order_id=pos.order_id,
                market_id=pos.market_id,
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                shares=pos.shares,
                pnl=pnl if pnl is not None else 0.0,
                result=f"{result_str}({exit_reason})",
                mode="模拟",
            )
        else:
            await _submit_exit(state, pos, exit_reason, best_bid)


async def reconcile_positions(session: aiohttp.ClientSession) -> None:
    """
    Compare our internal open-position book (DB + RiskState) against actual
    on-chain holdings via Polymarket's public Data API. Nothing else in this
    bot catches desync: a partial fill, a sell that _pending_exits thinks is
    still live but actually got cancelled by Polymarket, or a crash between
    order submission and DB write would otherwise go unnoticed indefinitely.
    Live mode only — paper mode has no on-chain positions to compare against.
    """
    if PAPER_TRADING:
        return

    try:
        client = _get_clob_client()
        wallet = await asyncio.get_event_loop().run_in_executor(None, client.get_address)
    except Exception as exc:
        log.warning(f"[对账] 无法获取钱包地址: {exc}")
        return

    try:
        async with session.get(
            "https://data-api.polymarket.com/positions",
            params={"user": wallet},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            onchain = await resp.json()
    except Exception as exc:
        log.warning(f"[对账] 查询链上持仓失败: {exc}")
        return

    onchain_by_token: dict[str, float] = {}
    for p in onchain if isinstance(onchain, list) else []:
        token = p.get("asset")
        if not token:
            continue
        try:
            size = float(p.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size > 1e-6:
            onchain_by_token[token] = onchain_by_token.get(token, 0.0) + size

    state = get_state()
    tracked_by_token = {pos.token_id: pos for pos in state.open_positions}

    mismatches = 0
    for token, onchain_size in onchain_by_token.items():
        tracked = tracked_by_token.get(token)
        if tracked is None:
            mismatches += 1
            log.warning(
                f"[对账] 发现未跟踪的链上持仓: token={token[:12]}… "
                f"size={onchain_size:.2f} — 本地DB/state中无对应记录"
            )
        elif abs(tracked.shares - onchain_size) > 0.5:
            mismatches += 1
            log.warning(
                f"[对账] 持仓数量不一致 {tracked.order_id}: "
                f"本地记录={tracked.shares:.2f} 链上实际={onchain_size:.2f}"
            )

    for token, pos in tracked_by_token.items():
        if token not in onchain_by_token:
            mismatches += 1
            log.warning(
                f"[对账] 本地记录持仓 {pos.order_id} (token={token[:12]}…) "
                f"但链上未查到对应余额 — 可能已平仓但本地未同步，或订单从未真正成交"
            )

    if mismatches == 0 and (onchain_by_token or tracked_by_token):
        log.info(f"[对账] 本地持仓与链上一致 ({len(tracked_by_token)} 个仓位)")


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
        # Settle once the market's own resolution time has passed (plus a small
        # buffer so the close-price tick has arrived). Fall back to the age
        # heuristic only for legacy positions with no recorded end timestamp.
        if pos.market_end_ts > 0:
            if now < pos.market_end_ts + 5:
                continue
        else:
            age = now - pos.opened_at
            if age < 360:
                continue

        if PAPER_TRADING:
            # Determine outcome from BTC movement over the MARKET's own window,
            # not the position's age: [market_end_ts - 300, market_end_ts].
            # This is the same window Polymarket settles on, regardless of when
            # within the window we actually entered.
            if pos.market_end_ts > 0:
                btc_open = btc_price.get_price_at(pos.market_end_ts - 300)
                btc_close = btc_price.get_price_at(pos.market_end_ts)
            else:
                # Legacy fallback: approximate via position age
                age = now - pos.opened_at
                btc_open = btc_price.get_price_n_seconds_ago(int(age))
                btc_close = btc_price.get_price_n_seconds_ago(int(max(0, age - 300)))

            if btc_open is not None and btc_close is not None:
                btc_went_up = btc_close >= btc_open
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
                f"SETTLE {result_str} {pos.side} "
                f"{pos.entry_price:.3f}->{exit_price:.1f} "
                f"BTC={outcome_str} pnl={pnl:+.2f} {pos.order_id}"
            )
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: db.close_position(pos.order_id, exit_price, "SETTLE")
            )
            await asyncio.get_event_loop().run_in_executor(None, lambda: db.update_balance(state.balance))
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
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: db.close_position(pos.order_id, exit_price, "SETTLE")
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
