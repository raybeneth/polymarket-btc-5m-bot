"""
SQLite-backed position storage.

Single source of truth for trade records — replaces the old trades.jsonl /
positions.log files. P&L and win/loss are computed by generated columns in
SQL (see schema.sql), not hand-rolled in Python.

All functions here are synchronous (stdlib sqlite3). Async callers must wrap
them with `run_in_executor`, same pattern already used for the blocking
py-clob-client calls in execution/order_manager.py. Executor threads share
one connection guarded by a lock; WAL mode lets a separate web_only.py
process read the same file while the bot writes.

Timestamps are stored as unix seconds (REAL, UTC).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from config import SQLITE_PATH
from execution.risk import OpenPosition

log = logging.getLogger(__name__)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        parent = os.path.dirname(os.path.abspath(SQLITE_PATH))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(
            SQLITE_PATH,
            timeout=10,                 # wait on another process's write lock
            check_same_thread=False,    # shared across executor threads (guarded by _lock)
            isolation_level=None,       # autocommit, same as the old MySQL pool
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _conn = conn
    return _conn


def _execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        return _get_conn().execute(sql, params)


def _fetchone(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with _lock:
        return _get_conn().execute(sql, params).fetchone()


def _fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _get_conn().execute(sql, params).fetchall()


def init_schema() -> None:
    """Create the positions + logs tables if missing. Call once at startup."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        ddl = f.read()
    with _lock:
        _get_conn().executescript(ddl)
    log.info(f"SQLite 表已就绪 ({SQLITE_PATH})")


def insert_open(pos: OpenPosition, mode: str, usd_size: float,
                 combined_score: float, filter_scores: dict) -> None:
    """Insert a new OPEN position row."""
    _execute(
        """
        INSERT INTO positions
            (order_id, market_id, token_id, side, mode, status,
             entry_price, shares, usd_size, combined_score, filter_scores,
             opened_at, market_end_ts)
        VALUES
            (?, ?, ?, ?, ?, 'OPEN',
             ?, ?, ?, ?, ?,
             ?, ?)
        """,
        (
            pos.order_id, pos.market_id, pos.token_id, pos.side, mode,
            pos.entry_price, pos.shares, usd_size, combined_score,
            json.dumps(filter_scores),
            pos.opened_at, pos.market_end_ts or None,
        ),
    )


def close_position(order_id: str, exit_price: float, exit_reason: str) -> Optional[dict]:
    """
    Mark a position CLOSED. SQLite's generated `pnl`/`result` columns compute
    the outcome — we just read them back.

    Returns {"pnl": float, "result": "WIN"|"LOSS"} or None if order_id unknown.
    """
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            UPDATE positions
            SET exit_price = ?, closed_at = ?,
                exit_reason = ?, status = 'CLOSED'
            WHERE order_id = ?
            """,
            (exit_price, time.time(), exit_reason, order_id),
        )
        row = conn.execute(
            "SELECT pnl, result FROM positions WHERE order_id = ?", (order_id,)
        ).fetchone()
    if row is None or row["pnl"] is None:
        return None
    return {"pnl": float(row["pnl"]), "result": row["result"]}


def get_stats() -> dict:
    """
    Aggregate historical + today's (UTC) trade stats, entirely via SQL.
    Shape matches the old utils.logger.load_position_history() return value.
    """
    # "trades" counts every position ever opened (OPEN or CLOSED) — not just
    # resolved ones — so a still-open position (e.g. restored after a restart)
    # is reflected immediately instead of appearing as 0 until it settles.
    # wins/losses/pnl are naturally NULL on open rows (see generated columns
    # in schema.sql) so they don't need an extra status filter.
    overall = _fetchone(
        """
        SELECT COUNT(*) AS total_trades,
               COALESCE(SUM(result='WIN'), 0)  AS wins,
               COALESCE(SUM(result='LOSS'), 0) AS losses,
               COALESCE(SUM(pnl), 0) AS total_pnl
        FROM positions
        """
    )
    today = _fetchone(
        """
        SELECT COUNT(*) AS today_trades,
               COALESCE(SUM(result='WIN'), 0)  AS today_wins,
               COALESCE(SUM(result='LOSS'), 0) AS today_losses,
               COALESCE(SUM(pnl), 0) AS today_pnl
        FROM positions
        WHERE date(opened_at, 'unixepoch') = date('now')
        """
    )
    return {
        "total_trades": int(overall["total_trades"]),
        "wins": int(overall["wins"]),
        "losses": int(overall["losses"]),
        "total_pnl": float(overall["total_pnl"]),
        "today_trades": int(today["today_trades"]),
        "today_wins": int(today["today_wins"]),
        "today_losses": int(today["today_losses"]),
        "today_pnl": float(today["today_pnl"]),
    }


def get_recent_closed(limit: Optional[int] = 50) -> list[dict]:
    """
    Closed positions, newest first, for the dashboard.
    limit=None returns the full history (used by the cumulative P&L chart).
    """
    base_sql = """
        SELECT order_id, mode, side, entry_price, exit_price, shares,
               pnl, result, exit_reason, opened_at, closed_at
        FROM positions
        WHERE status='CLOSED'
        ORDER BY closed_at DESC
    """
    if limit is None:
        rows = _fetchall(base_sql)
    else:
        rows = _fetchall(base_sql + " LIMIT ?", (limit,))

    return [
        {
            "order_id": row["order_id"],
            "mode": row["mode"],
            "side": row["side"],
            "entry_price": float(row["entry_price"]),
            "exit_price": float(row["exit_price"]) if row["exit_price"] is not None else None,
            "shares": float(row["shares"]),
            "pnl": float(row["pnl"]) if row["pnl"] is not None else 0.0,
            "result": row["result"],
            "exit_reason": row["exit_reason"],
            "opened_at": float(row["opened_at"] or 0.0),
            "closed_at": float(row["closed_at"] or 0.0),
        }
        for row in rows
    ]


def insert_log(ts: float, line: str) -> None:
    """Persist one dashboard log line (called from the web log-writer thread)."""
    _execute("INSERT INTO logs (ts, line) VALUES (?, ?)", (ts, line))


def get_recent_logs(limit: int = 200) -> list[str]:
    """Newest `limit` log lines, returned oldest-first for chronological display."""
    rows = _fetchall("SELECT line FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    return [row["line"] for row in reversed(rows)]


def prune_logs(keep: int = 5000) -> None:
    """Delete all but the newest `keep` log rows to bound table growth."""
    _execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (keep,))


def insert_book_snapshot(order_id: str, side: str, entry_price: float,
                          stop_price: Optional[float], tp_price: Optional[float],
                          best_bid: Optional[float], best_ask: Optional[float],
                          spread: Optional[float], secs_to_expiry: Optional[int],
                          triggered: Optional[str], ts: float) -> None:
    """Persist one per-poll order-book snapshot for an OPEN position (see
    position_book_snapshots in schema.sql). Called every loop from
    check_early_exits — keep it a single cheap INSERT."""
    _execute(
        """
        INSERT INTO position_book_snapshots
            (order_id, side, entry_price, stop_price, tp_price,
             best_bid, best_ask, spread, secs_to_expiry, triggered, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, side, entry_price, stop_price, tp_price,
         best_bid, best_ask, spread, secs_to_expiry, triggered, ts),
    )


def prune_book_snapshots(keep: int = 50000) -> None:
    """Delete all but the newest `keep` snapshot rows to bound table growth."""
    _execute(
        "DELETE FROM position_book_snapshots "
        "WHERE id <= (SELECT MAX(id) FROM position_book_snapshots) - ?",
        (keep,),
    )


def load_open_positions() -> list[OpenPosition]:
    """Reload still-OPEN positions on startup (crash/restart recovery)."""
    rows = _fetchall(
        """
        SELECT order_id, market_id, token_id, side, entry_price, shares,
               usd_size, opened_at, market_end_ts
        FROM positions WHERE status='OPEN'
        """
    )
    return [
        OpenPosition(
            market_id=row["market_id"],
            token_id=row["token_id"],
            side=row["side"],
            entry_price=float(row["entry_price"]),
            shares=float(row["shares"]),
            usd_size=float(row["usd_size"]),
            opened_at=float(row["opened_at"] or 0.0),
            order_id=row["order_id"],
            market_end_ts=float(row["market_end_ts"] or 0.0),
        )
        for row in rows
    ]


def get_or_init_balance(seed: float) -> float:
    """First-ever startup: seed the account balance from `seed`. After that,
    the DB row is authoritative — restarts don't reset it."""
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT balance FROM account_state WHERE id = 1").fetchone()
        if row is not None:
            return float(row["balance"])
        conn.execute("INSERT INTO account_state (id, balance) VALUES (1, ?)", (seed,))
        return seed


def update_balance(balance: float) -> None:
    _execute(
        "INSERT INTO account_state (id, balance) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET balance = excluded.balance, "
        "updated_at = CURRENT_TIMESTAMP",
        (balance,),
    )


def get_strategy_links() -> list[dict]:
    """Other running strategy instances for the dashboard nav menu."""
    rows = _fetchall(
        "SELECT name, url FROM strategy_links "
        "WHERE enabled = 1 ORDER BY sort_order ASC, id ASC"
    )
    return [dict(row) for row in rows]
