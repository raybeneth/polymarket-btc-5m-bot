"""
Regression tests for the two causes of stranded on-chain positions, fixed
2026-08-15. Both were found by replaying position_book_snapshots against the
181 recorded live positions.

Fix 1 — NO_BID_EXIT (order_manager.check_early_exits).
  FORCE_EXIT only opens at t-30s, but every bid-empty position on record lost
  its last bid 41-116s before expiry (#140/#178/#179/#180/#181). The t-30s sell
  was offered into a book with no counterparty at any price, never filled, and
  the position rode into settlement — leaving its shares stranded on-chain for
  16-33h. Exiting after ABANDON_NO_BID_TICKS consecutive bid-empty polls costs
  no win rate: of all 181 positions exactly 7 ever reached a 3-poll streak, all
  7 were the SETTLE losers, and none recovered to a bid above 0.10.

Fix 2 — EXIT_ORDER_RETRY_SECONDS (order_manager._poll_pending_exits).
  Exits reused ORDER_FILL_TIMEOUT_SECONDS * 3 (24s) as their cancel timeout
  inside a 30s window, so there was room for exactly one attempt: #144 offered
  at the bid that existed at t-29s, the bid evaporated, and the one retry landed
  at t-3s. The exit timeout is now its own constant — sharing the entry one
  would have cut entry frequency to buy exit responsiveness.
"""
import pytest

from config import (
    ABANDON_NO_BID_TICKS,
    EXIT_ORDER_RETRY_SECONDS,
    EXIT_RETRY_CONCESSION,
    EXIT_RETRY_MAX_TICKS,
    FORCE_EXIT_BEFORE_EXPIRY,
    FORCE_EXIT_NO_BID_PRICE,
    FORCE_EXIT_NO_BID_MAX_ASK,
    ORDER_FILL_TIMEOUT_SECONDS,
)
from execution import order_manager as om


@pytest.fixture(autouse=True)
def _reset_exit_state():
    om._no_bid_streak.clear()
    om._exit_attempts.clear()
    om._nobid_hold_logged.clear()
    yield
    om._no_bid_streak.clear()
    om._exit_attempts.clear()
    om._nobid_hold_logged.clear()


# ── Fix 1: NO_BID_EXIT trigger ──────────────────────────────────────────────

def _decide(bids, force_exit=False, stop_price=0.50, tp_price=0.90,
            stop_on=False, tp_on=False, abandon_on=True):
    """Mirrors the trigger block in check_early_exits, fed a sequence of
    best_bid readings (None = empty book). Returns the reason on the LAST poll."""
    streak = 0
    reason = None
    for best_bid in bids:
        streak = streak + 1 if best_bid is None else 0
        reason = None
        if best_bid is not None:
            if stop_on and best_bid <= stop_price:
                reason = "STOP_LOSS"
            elif tp_on and best_bid >= tp_price:
                reason = "TAKE_PROFIT"
        if reason is None and abandon_on and streak >= ABANDON_NO_BID_TICKS:
            reason = "NO_BID_EXIT"
        if reason is None and force_exit:
            reason = "FORCE_EXIT"
    return reason


def test_sustained_empty_book_exits_without_waiting_for_the_window():
    """#181's shape: bid gone at t-85s, force-exit window still 55s away."""
    assert _decide([None] * ABANDON_NO_BID_TICKS) == "NO_BID_EXIT"


def test_a_single_empty_read_does_not_fire():
    """One empty book can be an API blip — that's why it takes several polls."""
    assert _decide([None]) is None
    for n in range(1, ABANDON_NO_BID_TICKS):
        assert _decide([None] * n) is None, n


def test_streak_resets_when_a_bid_reappears():
    """A book that flickers empty then quotes again is alive; #179 did exactly
    this at t-54s (bid 0.01 for two polls) before going empty for good."""
    assert _decide([None, None, 0.01, None, None]) is None
    assert _decide([None, None, 0.01] + [None] * ABANDON_NO_BID_TICKS) == "NO_BID_EXIT"


def test_a_cheap_but_present_bid_never_triggers():
    """The #101 case, and the reason the rule is "no bid at all" rather than
    "bid <= 0.02": #101 sat at 0.02 with 40s left and came back to exit at 0.77.
    A loose threshold would have booked -4.84 instead of +1.11."""
    assert _decide([0.02] * 20) is None
    assert _decide([0.01] * 20) is None


def test_price_triggers_still_take_precedence():
    assert _decide([0.40], stop_on=True) == "STOP_LOSS"
    assert _decide([0.95], tp_on=True) == "TAKE_PROFIT"


def test_no_bid_exit_takes_precedence_over_force_exit():
    """Both fire on an empty book inside the window; the more specific reason
    wins so the DB/snapshot trail says why we bailed."""
    assert _decide([None] * ABANDON_NO_BID_TICKS, force_exit=True) == "NO_BID_EXIT"


def test_force_exit_is_unchanged_when_a_bid_exists():
    assert _decide([0.42], force_exit=True) == "FORCE_EXIT"
    assert _decide([0.42], force_exit=False) is None


def test_kill_switch_restores_the_old_behaviour():
    assert _decide([None] * 50, abandon_on=False) is None
    assert _decide([None] * 50, abandon_on=False, force_exit=True) == "FORCE_EXIT"


def test_trigger_fires_well_before_the_force_exit_window():
    """The whole point: at a ~2s poll cadence the streak completes in seconds,
    not at t-30s. Every recorded case had 41s+ of runway left."""
    assert ABANDON_NO_BID_TICKS * 2 < FORCE_EXIT_BEFORE_EXPIRY


def test_the_ask_guard_still_gates_the_sell():
    """NO_BID_EXIT decides to leave; it does NOT bypass the guard that refuses to
    dump a position whose asks say it may still settle at 1.0."""
    def offers(best_ask):
        if best_ask is not None and best_ask > FORCE_EXIT_NO_BID_MAX_ASK:
            return False
        return True

    assert offers(0.01) is True                       # every recorded case
    assert offers(FORCE_EXIT_NO_BID_MAX_ASK) is True
    for ask in (0.06, 0.30, 0.90):
        assert offers(ask) is False, ask


# ── Fix 2: exit retry budget and re-quote price ─────────────────────────────

def test_exit_timeout_is_independent_of_the_entry_timeout():
    """The bug this guards: shortening ORDER_FILL_TIMEOUT_SECONDS to speed up
    exit retries would also shorten how long an ENTRY waits for its fill
    (_wait_for_fill), cancelling slow entries and cutting entry frequency."""
    assert EXIT_ORDER_RETRY_SECONDS != ORDER_FILL_TIMEOUT_SECONDS * 3
    assert EXIT_ORDER_RETRY_SECONDS < ORDER_FILL_TIMEOUT_SECONDS * 3


def test_the_window_now_fits_several_attempts():
    """#144 got one. The window must fit at least three re-quotes so a bid that
    evaporates between fetch and submit isn't the end of it."""
    assert FORCE_EXIT_BEFORE_EXPIRY / EXIT_ORDER_RETRY_SECONDS >= 3


def test_first_offer_is_the_untouched_best_bid():
    assert om._exit_offer_price("pos-1", 0.42) == 0.42


def test_each_retry_undercuts_by_one_tick():
    om._exit_attempts["pos-1"] = 1
    assert om._exit_offer_price("pos-1", 0.42) == pytest.approx(0.42 - EXIT_RETRY_CONCESSION)
    om._exit_attempts["pos-1"] = 2
    assert om._exit_offer_price("pos-1", 0.42) == pytest.approx(0.42 - 2 * EXIT_RETRY_CONCESSION)


def test_concession_is_capped():
    """Unbounded undercutting would eventually give the position away."""
    om._exit_attempts["pos-1"] = 99
    floor = 0.42 - EXIT_RETRY_MAX_TICKS * EXIT_RETRY_CONCESSION
    assert om._exit_offer_price("pos-1", 0.42) == pytest.approx(floor)


def test_concession_cannot_flip_a_historical_winner():
    """Of the 60 profitable FORCE_EXITs since 2026-07-25 the thinnest margin over
    entry was +0.03 (#99, 0.61 -> 0.64). The cap must stay under that."""
    assert EXIT_RETRY_MAX_TICKS * EXIT_RETRY_CONCESSION < 0.03


def test_concession_never_goes_below_the_minimum_tick():
    om._exit_attempts["pos-1"] = EXIT_RETRY_MAX_TICKS
    assert om._exit_offer_price("pos-1", FORCE_EXIT_NO_BID_PRICE) == FORCE_EXIT_NO_BID_PRICE
    assert om._exit_offer_price("pos-1", 0.015) >= FORCE_EXIT_NO_BID_PRICE


def test_empty_book_offers_the_minimum_tick_regardless_of_attempts():
    """With no bid there's nothing to undercut — 0.01 is already the floor."""
    for attempts in (0, 1, 5, 50):
        om._exit_attempts["pos-1"] = attempts
        assert om._exit_offer_price("pos-1", None) == FORCE_EXIT_NO_BID_PRICE


def test_attempts_are_tracked_per_position():
    om._exit_attempts["pos-1"] = 2
    assert om._exit_offer_price("pos-2", 0.42) == 0.42


# ── Bookkeeping lifecycle ───────────────────────────────────────────────────

def test_closed_positions_are_pruned():
    """These dicts live for the process lifetime; without pruning a long-running
    bot accumulates one entry per trade forever."""
    om._no_bid_streak.update({"open-1": 4, "closed-1": 7})
    om._exit_attempts.update({"open-1": 2, "closed-1": 3})
    om._nobid_hold_logged.update({"open-1", "closed-1"})

    om._prune_exit_state({"open-1"})

    assert om._no_bid_streak == {"open-1": 4}
    assert om._exit_attempts == {"open-1": 2}
    assert om._nobid_hold_logged == {"open-1"}


def test_pruning_with_nothing_open_clears_everything():
    om._no_bid_streak["gone"] = 3
    om._exit_attempts["gone"] = 1
    om._nobid_hold_logged.add("gone")

    om._prune_exit_state(set())

    assert not om._no_bid_streak
    assert not om._exit_attempts
    assert not om._nobid_hold_logged


def test_a_fresh_position_starts_at_the_plain_best_bid():
    """Reused order-id state from an earlier trade must not make the next
    position's first offer arrive pre-conceded."""
    om._exit_attempts["pos-1"] = EXIT_RETRY_MAX_TICKS
    om._prune_exit_state(set())
    assert om._exit_offer_price("pos-1", 0.42) == 0.42
