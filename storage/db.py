"""
MySQL-backed position storage.

Single source of truth for trade records — replaces the old trades.jsonl /
positions.log files. P&L and win/loss are computed by generated columns in
SQL (see schema.sql), not hand-rolled in Python.

All functions here are synchronous (PyMySQL + connection pool). Async callers
must wrap them with `run_in_executor`, same pattern already used for the
blocking py-clob-client calls in execution/order_manager.py.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import pymysql
import pymysql.cursors
from dbutils.pooled_db import PooledDB

from config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
from execution.risk import OpenPosition

log = logging.getLogger(__name__)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

_pool: Optional[PooledDB] = None


def _get_pool() -> PooledDB:
    global _pool
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=8,
            mincached=1,
            blocking=True,
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    return _pool


def _to_dt(ts: float) -> Optional[datetime]:
    """Unix timestamp -> naive UTC datetime (MySQL DATETIME has no tz)."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _to_ts(dt: Optional[datetime]) -> float:
    """Naive UTC datetime (as read back from MySQL) -> unix timestamp."""
    if dt is None:
        return 0.0
    return dt.replace(tzinfo=timezone.utc).timestamp()


def init_schema() -> None:
    """Create the positions table if missing. Call once at startup."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        ddl = f.read()
    statement = "\n".join(
        line for line in ddl.splitlines() if not line.strip().startswith("--")
    )
    conn = _get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    finally:
        conn.close()
    log.info("MySQL positions 表已就绪")


def insert_open(pos: OpenPosition, mode: str, usd_size: float,
                 combined_score: float, filter_scores: dict) -> None:
    """Insert a new OPEN position row."""
    conn = _get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO positions
                    (order_id, market_id, token_id, side, mode, status,
                     entry_price, shares, usd_size, combined_score, filter_scores,
                     opened_at, market_end_ts)
                VALUES
                    (%s, %s, %s, %s, %s, 'OPEN',
                     %s, %s, %s, %s, %s,
                     %s, %s)
                """,
                (
                    pos.order_id, pos.market_id, pos.token_id, pos.side, mode,
                    pos.entry_price, pos.shares, usd_size, combined_score,
                    json.dumps(filter_scores),
                    _to_dt(pos.opened_at), _to_dt(pos.market_end_ts),
                ),
            )
    finally:
        conn.close()


def close_position(order_id: str, exit_price: float, exit_reason: str) -> Optional[dict]:
    """
    Mark a position CLOSED. MySQL's generated `pnl`/`result` columns compute
    the outcome — we just read them back.

    Returns {"pnl": float, "result": "WIN"|"LOSS"} or None if order_id unknown.
    """
    conn = _get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE positions
                SET exit_price = %s, closed_at = UTC_TIMESTAMP(3),
                    exit_reason = %s, status = 'CLOSED'
                WHERE order_id = %s
                """,
                (exit_price, exit_reason, order_id),
            )
            cur.execute(
                "SELECT pnl, result FROM positions WHERE order_id = %s",
                (order_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None or row["pnl"] is None:
        return None
    return {"pnl": float(row["pnl"]), "result": row["result"]}


def get_stats() -> dict:
    """
    Aggregate historical + today's (UTC) trade stats, entirely via SQL.
    Shape matches the old utils.logger.load_position_history() return value.
    """
    conn = _get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total_trades,
                       COALESCE(SUM(result='WIN'), 0)  AS wins,
                       COALESCE(SUM(result='LOSS'), 0) AS losses,
                       COALESCE(SUM(pnl), 0) AS total_pnl
                FROM positions WHERE status='CLOSED'
                """
            )
            overall = cur.fetchone()
            cur.execute(
                """
                SELECT COUNT(*) AS today_trades,
                       COALESCE(SUM(result='WIN'), 0)  AS today_wins,
                       COALESCE(SUM(result='LOSS'), 0) AS today_losses,
                       COALESCE(SUM(pnl), 0) AS today_pnl
                FROM positions
                WHERE status='CLOSED' AND DATE(closed_at) = UTC_DATE()
                """
            )
            today = cur.fetchone()
    finally:
        conn.close()
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
    conn = _get_pool().connection()
    try:
        with conn.cursor() as cur:
            if limit is None:
                cur.execute(base_sql)
            else:
                cur.execute(base_sql + " LIMIT %s", (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()

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
            "opened_at": _to_ts(row["opened_at"]),
            "closed_at": _to_ts(row["closed_at"]),
        }
        for row in rows
    ]


def load_open_positions() -> list[OpenPosition]:
    """Reload still-OPEN positions on startup (crash/restart recovery)."""
    conn = _get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_id, market_id, token_id, side, entry_price, shares,
                       opened_at, market_end_ts
                FROM positions WHERE status='OPEN'
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        OpenPosition(
            market_id=row["market_id"],
            token_id=row["token_id"],
            side=row["side"],
            entry_price=float(row["entry_price"]),
            shares=float(row["shares"]),
            opened_at=_to_ts(row["opened_at"]),
            order_id=row["order_id"],
            market_end_ts=_to_ts(row["market_end_ts"]),
        )
        for row in rows
    ]
