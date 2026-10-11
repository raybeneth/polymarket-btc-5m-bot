"""storage.db against a throwaway SQLite file: schema, P&L generated columns,
restart recovery, balance upsert, log pruning."""
import time

import pytest

from execution.risk import OpenPosition
from storage import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SQLITE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "_conn", None)
    db.init_schema()
    db.init_schema()  # idempotent
    yield
    db._conn.close()


def _pos(order_id, entry=0.6, shares=10.0):
    now = time.time()
    return OpenPosition(
        market_id="m", token_id="t", side="YES", entry_price=entry,
        shares=shares, usd_size=entry * shares, opened_at=now,
        order_id=order_id, market_end_ts=now + 300,
    )


def test_open_close_pnl_and_stats():
    db.insert_open(_pos("0x" + "a" * 64), "paper", 6.0, 55.0, {"f": 1})
    db.insert_open(_pos("b"), "paper", 6.0, 55.0, {})
    db.insert_open(_pos("c"), "paper", 6.0, 55.0, {})

    assert db.close_position("0x" + "a" * 64, 1.0, "SETTLE") == {"pnl": 4.0, "result": "WIN"}
    assert db.close_position("b", 0.0, "SETTLE") == {"pnl": -6.0, "result": "LOSS"}
    assert db.close_position("missing", 1.0, "SETTLE") is None

    stats = db.get_stats()
    assert stats["total_trades"] == 3 and stats["today_trades"] == 3
    assert stats["wins"] == 1 and stats["losses"] == 1
    assert stats["total_pnl"] == pytest.approx(-2.0)

    closed = db.get_recent_closed(None)
    assert [r["order_id"] for r in closed] == ["b", "0x" + "a" * 64]
    assert closed[0]["closed_at"] > 0

    open_rows = db.load_open_positions()
    assert [p.order_id for p in open_rows] == ["c"]
    assert open_rows[0].market_end_ts > open_rows[0].opened_at


def test_balance_seed_then_update():
    assert db.get_or_init_balance(100.0) == 100.0
    db.update_balance(92.5)
    assert db.get_or_init_balance(100.0) == 92.5


def test_logs_prune_and_order():
    for i in range(10):
        db.insert_log(time.time(), f"line{i}")
    db.prune_logs(keep=3)
    assert db.get_recent_logs(200) == ["line7", "line8", "line9"]


def test_book_snapshots_and_links():
    db.insert_book_snapshot("c", "YES", 0.6, 0.4, None, 0.59, 0.61, 0.02, 120, None, time.time())
    db.prune_book_snapshots(keep=1)
    assert db.get_strategy_links() == []
