"""Chainlink RTDS parsing, fair-value math, and paper settlement source order."""
import asyncio
import math
import time

import pytest

from feeds import btc_price, chainlink_price
from filters import fair_value


@pytest.fixture(autouse=True)
def _clean_feeds(monkeypatch):
    monkeypatch.setattr(chainlink_price, "_price_history", chainlink_price.deque())
    monkeypatch.setattr(chainlink_price, "_latest_price", None)
    monkeypatch.setattr(chainlink_price, "_latest_ts", 0.0)
    monkeypatch.setattr(btc_price, "_price_history", btc_price.deque())
    monkeypatch.setattr(btc_price, "_latest_price", None)
    fair_value._start_cache.clear()


# ── RTDS parsing ─────────────────────────────────────────────────────────

def test_parse_update_ms_timestamp():
    msg = {"topic": "crypto_prices_chainlink", "type": "update",
           "payload": {"symbol": "btc/usd", "timestamp": 1_760_000_000_123, "value": 83050.5}}
    assert chainlink_price._parse(msg) == [(1_760_000_000.123, 83050.5)]


def test_parse_snapshot_sorted_and_other_symbols_ignored():
    snap = {"topic": "crypto_prices_chainlink",
            "payload": {"symbol": "btc/usd", "data": [
                {"timestamp": 2_000, "value": 2.0}, {"timestamp": 1_000, "value": 1.0}]}}
    assert chainlink_price._parse(snap) == [(1_000.0, 1.0), (2_000.0, 2.0)]
    eth = {"topic": "crypto_prices_chainlink", "payload": {"symbol": "eth/usd", "timestamp": 1, "value": 1}}
    assert chainlink_price._parse(eth) == []
    assert chainlink_price._parse({"topic": "activity", "payload": {}}) == []
    assert chainlink_price._parse("PONG") == []


def test_price_at_uses_last_sample_at_or_before_boundary():
    now = time.time()
    chainlink_price._ingest([(now - 10, 100.0), (now - 4.5, 101.0), (now - 3.5, 999.0)])
    # sample after the boundary must not decide it
    assert chainlink_price.get_price_at(now - 4.0) == 101.0
    # nothing within tolerance before the boundary
    assert chainlink_price.get_price_at(now - 20) is None


# ── Fair value ───────────────────────────────────────────────────────────

def test_prob_up_symmetry_and_direction():
    sigma = 0.0001
    assert fair_value.prob_up(100.0, 100.0, sigma, 120) == pytest.approx(0.5)
    k = math.exp(0.0015)   # log-symmetric up/down moves
    hi = fair_value.prob_up(100.0 * k, 100.0, sigma, 100)
    lo = fair_value.prob_up(100.0 / k, 100.0, sigma, 100)
    assert hi > 0.9 and lo < 0.1 and hi + lo == pytest.approx(1.0)
    # less time left → more certain
    assert fair_value.prob_up(100.05, 100.0, sigma, 30) > fair_value.prob_up(100.05, 100.0, sigma, 200)
    # expired
    assert fair_value.prob_up(100.0, 100.0, sigma, 0) == 1.0


def test_realised_vol_keeps_same_timestamp_moves():
    # 3 trades per second, price walks 0.1% between seconds — the intra-second
    # duplicates must not hide the per-second move.
    pts = []
    for sec in range(10):
        px = 100.0 * math.exp(0.001 * (-1) ** sec)
        pts += [(sec + 0.001, px), (sec + 0.001, px), (sec + 0.5, px)]
    assert fair_value.realised_vol_per_s(pts) == pytest.approx(0.002, rel=1e-6)


def test_realised_vol():
    pts = [(i, 100.0 * math.exp(0.001 * (-1) ** i)) for i in range(11)]
    assert fair_value.realised_vol_per_s(pts) == pytest.approx(0.002, rel=1e-6)
    assert fair_value.realised_vol_per_s(pts[:2]) is None


def test_check_prefers_chainlink_and_computes_edge():
    now = time.time()
    end = now + 100
    start_ts = end - 300
    chainlink_price._ingest([(start_ts - 1, 80_000.0), (now - 1, 80_200.0)])
    btc_price._price_history.extend([(start_ts, 70_000.0), (now - 1, 69_000.0)])  # would say DOWN
    btc_price._latest_price = 69_000.0

    yes = fair_value.check(end, "YES", ask=0.70, now=now)
    assert yes.passed and yes.details["source"] == "chainlink"
    assert yes.details["fair"] > 0.9
    assert yes.details["edge"] == pytest.approx(yes.details["fair"] - 0.70)
    no = fair_value.check(end, "NO", ask=0.20, now=now)
    assert no.details["edge"] < 0


def test_check_falls_back_to_exchange_when_chainlink_missing():
    now = time.time()
    end = now + 100
    btc_price._price_history.extend([(end - 300, 80_000.0), (now - 1, 80_000.0)])
    btc_price._latest_price = 80_000.0
    r = fair_value.check(end, "YES", ask=0.40, now=now)
    assert r.passed and r.details["source"] == "exchange"
    assert r.details["fair"] == pytest.approx(0.5)


def test_check_fails_without_start_price():
    r = fair_value.check(time.time() + 100, "YES", ask=0.5)
    assert not r.passed


def test_edge_score_bounds():
    assert fair_value.edge_score(-0.1) == 0.0
    assert fair_value.edge_score(1.0) == 100.0


# ── Paper settlement source order ────────────────────────────────────────

@pytest.fixture
def paper_settle(monkeypatch):
    from execution import order_manager as om
    from execution.risk import OpenPosition, get_state
    import feeds.polymarket_clob as pc

    monkeypatch.setattr(om, "PAPER_TRADING", True)
    monkeypatch.setattr(om.db, "close_position", lambda *a, **k: None)
    monkeypatch.setattr(om.db, "update_balance", lambda *a, **k: None)
    monkeypatch.setattr(om, "log_position_close", lambda **k: None)
    om._outcome_last_poll.clear()

    state = get_state()
    monkeypatch.setattr(state, "open_positions", [])
    closed = {}

    def record_close(market_id, exit_price):
        closed[market_id] = exit_price
        state.open_positions = [p for p in state.open_positions if p.market_id != market_id]
        return 0.0
    monkeypatch.setattr(state, "record_close", record_close)

    def add(end_ts, side="YES"):
        state.open_positions.append(OpenPosition(
            market_id="m", token_id="t", side=side, entry_price=0.5, shares=10,
            usd_size=5, opened_at=end_ts - 200, order_id="o", market_end_ts=end_ts))

    outcome = {"v": None}

    async def fake_outcome(session, window_start):
        return outcome["v"]
    monkeypatch.setattr(pc, "fetch_market_outcome", fake_outcome)
    return om, add, closed, outcome


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_settle_uses_polymarket_outcome_over_exchange(paper_settle):
    om, add, closed, outcome = paper_settle
    end = time.time() - 20
    btc_price._price_history.extend([(end - 300, 100.0), (end, 101.0)])  # exchange: UP
    add(end, side="YES")
    outcome["v"] = "DOWN"
    _run(om.resolve_expired_positions(session=object()))
    assert closed == {"m": 0.0}


def test_settle_waits_while_unresolved(paper_settle):
    om, add, closed, outcome = paper_settle
    add(time.time() - 20)
    _run(om.resolve_expired_positions(session=object()))
    assert closed == {}


def test_settle_falls_back_to_chainlink_after_wait(paper_settle):
    om, add, closed, outcome = paper_settle
    end = time.time() - 500
    chainlink_price._price_history.extend([(end - 301, 100.0), (end - 1, 99.0)])  # DOWN
    btc_price._price_history.extend([(end - 300, 100.0), (end, 101.0)])           # UP
    add(end, side="NO")
    _run(om.resolve_expired_positions(session=object()))
    assert closed == {"m": 1.0}


# ── Live settlement bookkeeping ──────────────────────────────────────────

@pytest.fixture
def live_settle(paper_settle, monkeypatch):
    om, add, closed, outcome = paper_settle
    monkeypatch.setattr(om, "PAPER_TRADING", False)

    async def no_client():
        raise AssertionError("must not fall back to last trade price")
    monkeypatch.setattr(om, "_get_clob_client", no_client)
    return om, add, closed, outcome


def test_live_settle_records_polymarket_outcome(live_settle):
    om, add, closed, outcome = live_settle
    add(time.time() - 20, side="NO")
    outcome["v"] = "DOWN"
    _run(om.resolve_expired_positions(session=object()))
    assert closed == {"m": 1.0}


def test_live_settle_waits_while_unresolved(live_settle):
    om, add, closed, outcome = live_settle
    add(time.time() - 20)
    _run(om.resolve_expired_positions(session=object()))
    assert closed == {}


def test_fair_value_gate_is_default():
    import config
    assert config.FAIR_VALUE_MODE == "gate"
