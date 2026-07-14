-- Position storage schema.
-- Applied automatically by storage.db.init_schema() on startup (idempotent),
-- kept here for reference / manual inspection.

CREATE TABLE IF NOT EXISTS positions (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    -- 80 not 64: polymarket-client's order IDs are 66 chars ("0x" + 64 hex) —
    -- the old py-clob-client's were shorter and fit VARCHAR(64), but that
    -- silently truncated/rejected (MySQL error 1406) every real order insert
    -- after the SDK migration, so live trades never made it into this table.
    order_id        VARCHAR(80)  NOT NULL,
    market_id       VARCHAR(128) NOT NULL,
    token_id        VARCHAR(128) NOT NULL,
    side            ENUM('YES','NO') NOT NULL,
    mode            ENUM('paper','live') NOT NULL,
    status          ENUM('OPEN','CLOSED') NOT NULL DEFAULT 'OPEN',

    entry_price     DECIMAL(10,4) NOT NULL,
    shares          DECIMAL(18,4) NOT NULL,
    usd_size        DECIMAL(10,2) NOT NULL,
    combined_score  DECIMAL(6,2)  NULL,
    filter_scores   JSON NULL,

    opened_at       DATETIME(3) NOT NULL,
    market_end_ts   DATETIME(3) NULL,

    exit_price      DECIMAL(10,4) NULL,
    closed_at       DATETIME(3) NULL,
    exit_reason     VARCHAR(32) NULL,

    -- SQL computes P&L and result, not Python.
    pnl DECIMAL(14,6) AS (
        CASE WHEN exit_price IS NULL THEN NULL
             ELSE ROUND((exit_price - entry_price) * shares, 6) END
    ) STORED,
    result ENUM('WIN','LOSS') AS (
        CASE WHEN exit_price IS NULL THEN NULL
             WHEN (exit_price - entry_price) * shares > 0 THEN 'WIN'
             ELSE 'LOSS' END
    ) STORED,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_order_id (order_id),
    KEY idx_status (status),
    KEY idx_closed_at (closed_at),
    KEY idx_market_id (market_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Dashboard "Live Log" persistence. Each row is one already-formatted log line
-- as shown in the UI, plus a timestamp for ordering/retention. Reloaded into the
-- in-memory buffer on startup so the log survives restarts.
CREATE TABLE IF NOT EXISTS logs (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts         DATETIME(3) NOT NULL,
    line       TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Dashboard nav menu: links to other running strategy instances (different
-- ports/domains). Managed by hand (INSERT/UPDATE directly) — no admin UI.
CREATE TABLE IF NOT EXISTS strategy_links (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(64)  NOT NULL,
    url        VARCHAR(255) NOT NULL,
    enabled    TINYINT(1)   NOT NULL DEFAULT 1,
    sort_order INT          NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Per-poll order-book snapshot for OPEN positions — diagnostic trail behind
-- stop-loss fills. check_early_exits writes one row every loop (~2s) for each
-- open position, capturing the best_bid/best_ask it saw at that instant plus
-- the nominal stop level. Lets a stop that fills far below its stop price be
-- traced to either a real best_bid gap between two polls, or a missing/one-
-- sided book (triggered='NO_BOOK'). `triggered` names the exit reason this
-- snapshot fired (STOP_LOSS/TAKE_PROFIT/FORCE_EXIT), else NULL.
CREATE TABLE IF NOT EXISTS position_book_snapshots (
    id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    order_id       VARCHAR(80) NOT NULL,
    side           ENUM('YES','NO') NOT NULL,
    entry_price    DECIMAL(10,4) NOT NULL,
    stop_price     DECIMAL(10,4) NULL,
    tp_price       DECIMAL(10,4) NULL,
    best_bid       DECIMAL(10,4) NULL,
    best_ask       DECIMAL(10,4) NULL,
    spread         DECIMAL(10,4) NULL,
    secs_to_expiry INT NULL,
    triggered      VARCHAR(16) NULL,
    ts             DATETIME(3) NOT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_order_id (order_id),
    KEY idx_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Paper-trading account balance — a single row (id=1), seeded once from
-- ACCOUNT_BALANCE_USD on first-ever startup, then the DB is authoritative.
-- Debited on open (by the position's usd_size), credited back (usd_size +
-- pnl) on close. Not used in live mode — that reads the real wallet balance.
CREATE TABLE IF NOT EXISTS account_state (
    id         TINYINT UNSIGNED PRIMARY KEY DEFAULT 1,
    balance    DECIMAL(14,4) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
