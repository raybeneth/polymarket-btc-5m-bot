-- Position storage schema.
-- Applied automatically by storage.db.init_schema() on startup (idempotent),
-- kept here for reference / manual inspection.

CREATE TABLE IF NOT EXISTS positions (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    order_id        VARCHAR(64)  NOT NULL,
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
