"""Half-sizing inside the weak entry-timing band (180-200s to expiry at fill)."""
import pytest

from config import (
    TradeDecision,
    TRADE_SIZE_USD,
    TRADE_SIZE_WEAK_TIMING_BAND,
    TRADE_SIZE_WEAK_TIMING_RATIO,
)
from execution import order_manager

LO, HI = TRADE_SIZE_WEAK_TIMING_BAND


NOW = 1_784_292_000.0


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin the clock: seconds-to-expiry is compared against exact band edges, so
    even sub-millisecond drift between building the decision and evaluating it
    would flip a boundary case."""
    monkeypatch.setattr(order_manager.time, "time", lambda: NOW)


def _decision(secs_to_expiry, size=100.0, entry=0.6, min_order_size=0.0):
    return TradeDecision(
        should_trade=True, side="YES", market_id="m", token_id="t",
        entry_price=entry, combined_score=50.0, filter_results={},
        min_order_size=min_order_size,
        market_end_ts=NOW + secs_to_expiry, trade_size_usd=size,
    )


@pytest.mark.parametrize("secs", [LO, LO + 10, HI - 1])
def test_halves_inside_band(secs):
    d = _decision(secs)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(100.0 * TRADE_SIZE_WEAK_TIMING_RATIO)


@pytest.mark.parametrize("secs", [LO - 1, HI, HI + 30, 60])
def test_untouched_outside_band(secs):
    d = _decision(secs)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(100.0)


def test_falls_back_to_default_size_when_unset():
    d = _decision(LO + 5, size=0.0)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(TRADE_SIZE_USD * TRADE_SIZE_WEAK_TIMING_RATIO)


def test_does_not_compound_on_reexecution():
    d = _decision(LO + 5)
    order_manager._apply_weak_timing_ratio(d)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(50.0)


def test_noop_without_market_end_ts():
    d = _decision(LO + 5)
    d.market_end_ts = 0.0
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(100.0)


def test_disabled_by_config(monkeypatch):
    monkeypatch.setattr(order_manager, "TRADE_SIZE_WEAK_TIMING_ENABLED", False)
    d = _decision(LO + 5)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(100.0)


# ── clamping the halved size up to the market's minimum order size ──
# Live divides the budget by entry_price+ORDER_PRICE_SLIPPAGE rounded to tick,
# paper divides by entry_price, so the same dollars buy fewer shares live.

def _shares(d, paper):
    price = (d.entry_price if paper
             else order_manager._round_to_tick(
                 d.entry_price + order_manager.ORDER_PRICE_SLIPPAGE, d.tick_size))
    return round(d.trade_size_usd / price, 2)


@pytest.mark.parametrize("paper", [True, False])
def test_clamps_up_to_min_order_size(monkeypatch, paper):
    """LOW tier: $5 halved to $2.50 buys <5 shares at 0.60, so it must be raised
    back to exactly the minimum instead of being skipped downstream."""
    monkeypatch.setattr(order_manager, "PAPER_TRADING", paper)
    d = _decision(LO + 5, size=5.0, entry=0.60, min_order_size=5.0)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd > 2.50            # was clamped up
    assert d.trade_size_usd <= 5.0            # never above the un-halved size
    assert _shares(d, paper) >= 5.0           # would now pass the min-size gate


@pytest.mark.parametrize("paper", [True, False])
def test_no_clamp_when_half_already_clears_minimum(monkeypatch, paper):
    """HIGH tier: $20 halved to $10 buys well over 5 shares — leave it halved."""
    monkeypatch.setattr(order_manager, "PAPER_TRADING", paper)
    d = _decision(LO + 5, size=20.0, entry=0.60, min_order_size=5.0)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(10.0)


@pytest.mark.parametrize("paper", [True, False])
def test_never_clamps_above_unhalved_size(monkeypatch, paper):
    """If even the full size can't reach the minimum, don't inflate it — leave it
    for the existing min-size check to reject (pre-existing behaviour)."""
    monkeypatch.setattr(order_manager, "PAPER_TRADING", paper)
    d = _decision(LO + 5, size=2.0, entry=0.90, min_order_size=5.0)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(1.0)   # plain half, no clamp


def test_no_clamp_without_min_order_size():
    d = _decision(LO + 5, size=5.0, entry=0.60, min_order_size=0.0)
    order_manager._apply_weak_timing_ratio(d)
    assert d.trade_size_usd == pytest.approx(2.5)


@pytest.mark.parametrize("entry", [0.40, 0.49, 0.50, 0.51, 0.72])
def test_clamped_orders_always_clear_minimum_live(monkeypatch, entry):
    """Sweep the historical band's entry-price range: after the clamp, a LOW-tier
    order must clear the minimum at every price, not just above 0.50."""
    monkeypatch.setattr(order_manager, "PAPER_TRADING", False)
    d = _decision(LO + 5, size=5.0, entry=entry, min_order_size=5.0)
    order_manager._apply_weak_timing_ratio(d)
    assert _shares(d, paper=False) >= 5.0
    assert d.trade_size_usd <= 5.0
