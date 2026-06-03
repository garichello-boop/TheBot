-- =============================================================================
-- Migration 004: bot_configs_history — audit trail for bot_configs
-- =============================================================================
--
-- Creates an append-only history table that records every change to
-- bot_configs, plus a trigger that fires automatically on INSERT and UPDATE.
--
-- changed_by resolution (priority order):
--   1. SET LOCAL app.changed_by = '...' before the DML statement
--      (used by ConfigRepository.rollback() and any code that wants attribution)
--   2. Falls back to 'unknown' if the GUC is not set
--      (covers direct SQL edits, WFO without GUC, legacy callers)
--
-- Usage from application code (Python):
--   with transaction() as cur:
--       cur.execute("SET LOCAL app.changed_by = %s", ("wfo",))
--       cur.execute("UPDATE bot_configs SET strategy_params = %s ...", ...)
--
-- Usage from psql / pgAdmin (operator):
--   BEGIN;
--   SET LOCAL app.changed_by = 'operator';
--   UPDATE bot_configs SET ... WHERE user_id = 'igor' AND bot_id = 'btc_paper_01';
--   COMMIT;
--
-- Apply:
--   psql -U postgres -d thebot -f 004_bot_configs_history.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Table
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bot_configs_history (
    id              BIGSERIAL       PRIMARY KEY,
    user_id         TEXT            NOT NULL,
    bot_id          TEXT            NOT NULL,
    config_version  INTEGER         NOT NULL,
    ticker          TEXT            NOT NULL,
    strategy_name   TEXT            NOT NULL,
    strategy_params JSONB           NOT NULL,
    virtual_balance NUMERIC(20, 8)  NOT NULL,
    status          TEXT            NOT NULL,
    changed_by      TEXT            NOT NULL  DEFAULT 'unknown',
    changed_at      TIMESTAMPTZ     NOT NULL  DEFAULT NOW()
);

COMMENT ON TABLE bot_configs_history IS
    'Append-only audit log of every INSERT/UPDATE on bot_configs. '
    'Each row is a full snapshot of the row after the change. '
    'changed_by is captured via SET LOCAL app.changed_by before the DML; '
    'defaults to ''unknown'' if not set.';

COMMENT ON COLUMN bot_configs_history.changed_by IS
    'Who triggered this change: ''operator'', ''wfo'', ''bot'', '
    '''rollback_to_vN:operator'', or ''unknown'' (GUC not set).';

-- Lookup index: all history for a bot, newest first
CREATE INDEX IF NOT EXISTS idx_bch_bot_lookup
    ON bot_configs_history (user_id, bot_id, id DESC);

-- Point lookup by config_version (used by rollback)
CREATE INDEX IF NOT EXISTS idx_bch_version_lookup
    ON bot_configs_history (user_id, bot_id, config_version);


-- -----------------------------------------------------------------------------
-- Trigger function
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION _bot_configs_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO bot_configs_history (
        user_id,
        bot_id,
        config_version,
        ticker,
        strategy_name,
        strategy_params,
        virtual_balance,
        status,
        changed_by,
        changed_at
    ) VALUES (
        NEW.user_id,
        NEW.bot_id,
        NEW.config_version,
        NEW.ticker,
        NEW.strategy_name,
        NEW.strategy_params,
        NEW.virtual_balance,
        NEW.status,
        -- NULLIF guards against an empty string from SET LOCAL app.changed_by = ''
        COALESCE(NULLIF(current_setting('app.changed_by', TRUE), ''), 'unknown'),
        NOW()
    );
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION _bot_configs_audit() IS
    'Audit trigger for bot_configs. Writes a full NEW-row snapshot to '
    'bot_configs_history on every INSERT and UPDATE.';


-- -----------------------------------------------------------------------------
-- Trigger  (DROP + CREATE for idempotent re-runs)
-- -----------------------------------------------------------------------------

DROP TRIGGER IF EXISTS bot_configs_audit ON bot_configs;

CREATE TRIGGER bot_configs_audit
    AFTER INSERT OR UPDATE
    ON bot_configs
    FOR EACH ROW
    EXECUTE FUNCTION _bot_configs_audit();

COMMENT ON TRIGGER bot_configs_audit ON bot_configs IS
    'Fires after every INSERT/UPDATE on bot_configs. '
    'Delegates to _bot_configs_audit() to write audit history.';


-- -----------------------------------------------------------------------------
-- Verification query (run manually to confirm trigger is active)
-- -----------------------------------------------------------------------------
--
-- SELECT trigger_name, event_manipulation, action_timing
--   FROM information_schema.triggers
--  WHERE event_object_table = 'bot_configs'
--    AND trigger_name = 'bot_configs_audit';
--
-- Expected: two rows — INSERT / UPDATE, AFTER.
-- -----------------------------------------------------------------------------
