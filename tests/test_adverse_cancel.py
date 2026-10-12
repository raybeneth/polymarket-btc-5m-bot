"""Adverse-move cancel for pending signals (main._adverse_cancel).

Regression cover for 2026-10-12 08:24-09:34: all 3 signals were cancelled
within 2s by the flat 1.5% rule. Cases below are the literal log values:
(market, side, signal price, price 2s later, edge at the later price).
"""
from types import SimpleNamespace

import pytest

import main
from config import FilterResult

LOG_SIGNALS = [
    # 08:26:16 YES @0.610 → 0.510; 08:26:20 fv at ask 0.500 edge=+0.444
    ("0xa6f9def4", "YES", 0.610, 0.510, 0.444),
    # 08:26:22 YES @0.470 → 0.420; 08:26:27 fv at ask 0.370 edge=+0.528
    ("0xa6f9def4", "YES", 0.470, 0.420, 0.528),
    # 09:11:13 NO @0.640 → 0.620; 09:11:18 fv at ask 0.550 edge=+0.346
    ("0x860e0b6e", "NO", 0.640, 0.620, 0.346),
]


def _fv(edge):
    return lambda end_ts, side, ask: FilterResult(
        passed=True, score=edge * 100, reason="test", details={"edge": edge})


def _run(monkeypatch, side, sig, now, edge, mode="gate"):
    monkeypatch.setattr(main, "FAIR_VALUE_MODE", mode)
    monkeypatch.setattr(main.fair_value, "check", _fv(edge))
    market = SimpleNamespace(end_timestamp=0.0)
    return main._adverse_cancel(market, side, sig, now, "0.01", "test", 2.0)


def test_threshold_is_at_least_two_ticks():
    assert main.adverse_threshold(0.30, "0.01") == pytest.approx(0.0667, abs=1e-3)
    assert main.adverse_threshold(0.70, "0.01") == pytest.approx(0.0286, abs=1e-3)
    # Fine tick grid falls back to the percentage floor.
    assert main.adverse_threshold(0.70, "0.001") == main.ENTRY_CONFIRM_MAX_ADVERSE_PCT


def test_one_tick_flicker_does_not_cancel(monkeypatch):
    # 0.70 → 0.69 is -1.43%; 0.68 → 0.67 is -1.47% — both just a tick.
    assert not _run(monkeypatch, "YES", 0.68, 0.67, edge=-1.0)


@pytest.mark.parametrize("mid,side,sig,now,edge", LOG_SIGNALS)
def test_log_signals_survive_with_positive_edge(monkeypatch, mid, side, sig, now, edge):
    assert not _run(monkeypatch, side, sig, now, edge)


def test_large_drop_with_edge_gone_cancels(monkeypatch):
    assert _run(monkeypatch, "YES", 0.610, 0.510, edge=0.01)


def test_shadow_mode_keeps_hard_cancel(monkeypatch):
    # Without gate mode the edge isn't trusted, so a real drop still cancels.
    assert _run(monkeypatch, "YES", 0.610, 0.510, edge=0.444, mode="shadow")
