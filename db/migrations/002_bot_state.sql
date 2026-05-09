-- Migration 002: bot_state and bot_registry tables
-- Apply: psql -U postgres -d thebot -f 002_bot_state.sql

-- Operational status of the bot process (monitoring/heartbeat)
CREATE TABLE IF NOT EXISTS bot_registry (
    user_id             TEXT        NOT NULL,
    bot_id              TEXT        NOT NULL,
    operational_status  TEXT        NOT NULL DEFAULT 'STOPPED',
    last_heartbeat      TIMESTAMP,
    pid                 INTEGER,
    started_at          TIMESTAMP,
    stopped_at          TIMESTAMP,
    error_message       TEXT,

    PRIMARY KEY (user_id, bot_id),

    CONSTRAINT bot_registry_status_check CHECK (
        operational_status IN ('STARTING', 'RUNNING', 'STOPPING', 'STOPPED', 'ERROR')
    )
);

-- Trading state: one row per bot, updated atomically with trades
CREATE TABLE IF NOT EXISTS bot_state (
    user_id                 TEXT        NOT NULL,
    bot_id                  TEXT        NOT NULL,
    version                 INTEGER     NOT NULL DEFAULT 0,
    cycle_id                TEXT,
    cycle_status            TEXT        NOT NULL DEFAULT 'IDLE',
    virtual_balance_free    NUMERIC     NOT NULL DEFAULT 0,
    virtual_balance_locked  NUMERIC     NOT NULL DEFAULT 0,
    position_qty            NUMERIC     NOT NULL DEFAULT 0,
    position_avg_price      NUMERIC,
    dca_count               INTEGER     NOT NULL DEFAULT 0,
    quote_spent             NUMERIC     NOT NULL DEFAULT 0,
    quote_received          NUMERIC     NOT NULL DEFAULT 0,
    last_applied_trade_id   TEXT,
    active_entry_order_id   TEXT,
    active_tp_order_id      TEXT,
    active_dca_order_ids    TEXT[]      NOT NULL DEFAULT '{}',
    pending_client_order_id TEXT,
    entered_at              TIMESTAMP,
    last_order_at           TIMESTAMP,
    updated_at              TIMESTAMP   NOT NULL DEFAULT NOW(),

    PRIMARY KEY (user_id, bot_id),

    CONSTRAINT bot_state_cycle_status_check CHECK (
        cycle_status IN (
            'IDLE', 'ENTERING', 'IN_POSITION',
            'CLOSING', 'WAITING_FOR_LIQUIDITY', 'STOP_CRANE'
        )
    ),
    CONSTRAINT bot_state_version_non_negative CHECK (version >= 0),
    CONSTRAINT bot_state_balance_free_non_negative CHECK (virtual_balance_free >= 0),
    CONSTRAINT bot_state_balance_locked_non_negative CHECK (virtual_balance_locked >= 0),
    CONSTRAINT bot_state_position_qty_non_negative CHECK (position_qty >= 0)
);

-- Index for heartbeat monitoring queries
CREATE INDEX IF NOT EXISTS idx_bot_registry_heartbeat
    ON bot_registry (last_heartbeat)
    WHERE operational_status = 'RUNNING';
