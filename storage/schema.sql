-- Position storage schema (SQLite).
-- Applied automatically by storage.db.init_schema() on startup (idempotent),
-- kept here for reference / manual inspection (sqlite3 polymarket_bot.db).
--
-- All *_at / *_ts / ts columns are unix timestamps (REAL, UTC seconds) —
-- inspect with e.g. datetime(opened_at, 'unixepoch').

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL UNIQUE,
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('YES','NO')),
    mode            TEXT NOT NULL CHECK (mode IN ('paper','live')),
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),

    entry_price     REAL NOT NULL,
    shares          REAL NOT NULL,
    usd_size        REAL NOT NULL,
    combined_score  REAL NULL,
    filter_scores   TEXT NULL,          -- JSON

    opened_at       REAL NOT NULL,
    market_end_ts   REAL NULL,

    exit_price      REAL NULL,
    closed_at       REAL NULL,
    exit_reason     TEXT NULL,

    -- SQL computes P&L and result, not Python.
    pnl REAL GENERATED ALWAYS AS (
        CASE WHEN exit_price IS NULL THEN NULL
             ELSE ROUND((exit_price - entry_price) * shares, 6) END
    ) STORED,
    result TEXT GENERATED ALWAYS AS (
        CASE WHEN exit_price IS NULL THEN NULL
             WHEN (exit_price - entry_price) * shares > 0 THEN 'WIN'
             ELSE 'LOSS' END
    ) STORED,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_positions_status    ON positions (status);
CREATE INDEX IF NOT EXISTS idx_positions_closed_at ON positions (closed_at);
CREATE INDEX IF NOT EXISTS idx_positions_market_id ON positions (market_id);

-- Dashboard "Live Log" persistence. Each row is one already-formatted log line
-- as shown in the UI, plus a timestamp for ordering/retention. Reloaded into the
-- in-memory buffer on startup so the log survives restarts.
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    line       TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Dashboard nav menu: links to other running strategy instances (different
-- ports/domains). Managed by hand (INSERT/UPDATE directly) — no admin UI.
CREATE TABLE IF NOT EXISTS strategy_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    url        TEXT    NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Per-poll order-book snapshot for OPEN positions — diagnostic trail behind
-- stop-loss fills. check_early_exits writes one row every loop (~2s) for each
-- open position, capturing the best_bid/best_ask it saw at that instant plus
-- the nominal stop level. Lets a stop that fills far below its stop price be
-- traced to either a real best_bid gap between two polls, or a missing/one-
-- sided book (triggered='NO_BOOK'). `triggered` names the exit reason this
-- snapshot fired (STOP_LOSS/TAKE_PROFIT/FORCE_EXIT), else NULL.
CREATE TABLE IF NOT EXISTS position_book_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('YES','NO')),
    entry_price    REAL NOT NULL,
    stop_price     REAL NULL,
    tp_price       REAL NULL,
    best_bid       REAL NULL,
    best_ask       REAL NULL,
    spread         REAL NULL,
    secs_to_expiry INTEGER NULL,
    triggered      TEXT NULL,
    ts             REAL NOT NULL,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_snapshots_order_id ON position_book_snapshots (order_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts       ON position_book_snapshots (ts);

-- Paper-trading account balance — a single row (id=1), seeded once from
-- ACCOUNT_BALANCE_USD on first-ever startup, then the DB is authoritative.
-- Debited on open (by the position's usd_size), credited back (usd_size +
-- pnl) on close. Not used in live mode — that reads the real wallet balance.
CREATE TABLE IF NOT EXISTS account_state (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    balance    REAL NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
