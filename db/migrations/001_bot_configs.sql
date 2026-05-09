-- =============================================================================
-- Migration 001: bot_configs
-- Dynamic bot configuration: strategy params, budget, status.
-- Updated by WFO script independently of the running bot.
-- Static config (infra) lives in .env -> AppSettings (Point 0).
-- =============================================================================

CREATE TABLE IF NOT EXISTS bot_configs (
    user_id             TEXT            NOT NULL,
    bot_id              TEXT            NOT NULL,

    ticker              TEXT            NOT NULL,
    exchange            TEXT            NOT NULL,

    strategy_name       TEXT            NOT NULL,
    strategy_params     JSONB           NOT NULL DEFAULT '{}',

    -- NUMERIC, not REAL: exact monetary calculations, no rounding drift
    virtual_balance     NUMERIC(20, 8)  NOT NULL,

    -- ACTIVE      -> normal operation, new cycles open
    -- CLOSE_ONLY  -> finish current position, no new cycles
    -- STOPPED     -> finish current cycle and stop
    -- FORCE_CLOSE -> close position with market order immediately
    status              TEXT            NOT NULL DEFAULT 'ACTIVE',

    -- Version counter. Incremented on every change.
    -- ConfigWatcher compares this before each new cycle.
    -- More reliable than updated_at for change detection.
    config_version      INTEGER         NOT NULL DEFAULT 1,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (user_id, bot_id),

    CONSTRAINT bot_configs_status_valid
        CHECK (status IN ('ACTIVE', 'CLOSE_ONLY', 'STOPPED', 'FORCE_CLOSE')),

    CONSTRAINT bot_configs_virtual_balance_non_negative
        CHECK (virtual_balance >= 0),

    CONSTRAINT bot_configs_config_version_positive
        CHECK (config_version >= 1)
);

-- Index on status for monitoring queries: "show all active bots"
CREATE INDEX IF NOT EXISTS idx_bot_configs_status
    ON bot_configs (status);

-- ---------------------------------------------------------------------------
-- Auto-update updated_at on every UPDATE
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION _set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bot_configs_updated_at ON bot_configs;

CREATE TRIGGER trg_bot_configs_updated_at
    BEFORE UPDATE ON bot_configs
    FOR EACH ROW
    EXECUTE FUNCTION _set_updated_at();
