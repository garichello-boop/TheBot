-- =============================================================================
-- Migration 005: bot_state_history — FSM transition audit log
-- =============================================================================
--
-- Records every FSM cycle_status transition in bot_state.
-- Insert strategy:
--   AFTER INSERT on bot_state   → always record (initial state creation).
--   AFTER UPDATE on bot_state   → record ONLY when cycle_status changes.
--
-- This gives ~10–20 rows per trading cycle instead of one row per tick,
-- which keeps the table compact while capturing the full FSM chronology.
--
-- Each row is a snapshot of the NEW state at the moment of the transition.
-- old_cycle_status is NULL for the first INSERT (no previous status).
-- trigger_op ('INSERT'/'UPDATE') distinguishes initialization from transitions.
--
-- No changed_by: bot_state is written exclusively by the bot process.
-- No rollback: state rollback is not a valid operation.
--
-- Apply:
--   psql -U postgres -d thebot -f 005_bot_state_history.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Table
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bot_state_history (
    id                      BIGSERIAL       PRIMARY KEY,

    -- Identity
    user_id                 TEXT            NOT NULL,
    bot_id                  TEXT            NOT NULL,

    -- FSM transition (the core reason this row exists)
    old_cycle_status        TEXT,                           -- NULL on INSERT
    new_cycle_status        TEXT            NOT NULL,

    -- Full state snapshot at the moment of transition
    version                 BIGINT          NOT NULL,
    cycle_id                TEXT,
    virtual_balance_free    NUMERIC(20, 8)  NOT NULL,
    virtual_balance_locked  NUMERIC(20, 8)  NOT NULL,
    position_qty            NUMERIC(20, 8)  NOT NULL,
    position_avg_price      NUMERIC(20, 8),
    dca_count               INTEGER         NOT NULL,
    quote_spent             NUMERIC(20, 8)  NOT NULL,
    quote_received          NUMERIC(20, 8)  NOT NULL,
    last_applied_trade_id   TEXT,
    active_entry_order_id   TEXT,
    active_tp_order_id      TEXT,
    closing_reason          TEXT,

    -- Metadata
    trigger_op              TEXT            NOT NULL,       -- 'INSERT' or 'UPDATE'
    recorded_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE bot_state_history IS
    'Append-only log of FSM cycle_status transitions. '
    'Each row is a full snapshot of bot_state at the moment of transition. '
    'Written by trigger _bot_state_fsm_audit on every INSERT '
    'and on every UPDATE where cycle_status changes.';

COMMENT ON COLUMN bot_state_history.old_cycle_status IS
    'Previous cycle_status before the transition. '
    'NULL for the initial INSERT (no prior status exists).';

COMMENT ON COLUMN bot_state_history.trigger_op IS
    'PostgreSQL TG_OP at the time of the trigger fire: ''INSERT'' or ''UPDATE''.';

-- Lookup index: all transitions for a bot, newest first
CREATE INDEX IF NOT EXISTS idx_bsh_bot_lookup
    ON bot_state_history (user_id, bot_id, id DESC);

-- Lookup by cycle_id (useful when investigating a specific cycle)
CREATE INDEX IF NOT EXISTS idx_bsh_cycle_lookup
    ON bot_state_history (user_id, bot_id, cycle_id)
    WHERE cycle_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- Trigger function
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION _bot_state_fsm_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- INSERT: always record — captures the initial IDLE state at bot startup.
    -- UPDATE: record only when cycle_status actually changed — skips tick-level
    --         saves that don't cross an FSM boundary.
    IF TG_OP = 'INSERT' OR NEW.cycle_status != OLD.cycle_status THEN
        INSERT INTO bot_state_history (
            user_id,
            bot_id,
            old_cycle_status,
            new_cycle_status,
            version,
            cycle_id,
            virtual_balance_free,
            virtual_balance_locked,
            position_qty,
            position_avg_price,
            dca_count,
            quote_spent,
            quote_received,
            last_applied_trade_id,
            active_entry_order_id,
            active_tp_order_id,
            closing_reason,
            trigger_op,
            recorded_at
        ) VALUES (
            NEW.user_id,
            NEW.bot_id,
            CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.cycle_status END,
            NEW.cycle_status,
            NEW.version,
            NEW.cycle_id,
            NEW.virtual_balance_free,
            NEW.virtual_balance_locked,
            NEW.position_qty,
            NEW.position_avg_price,
            NEW.dca_count,
            NEW.quote_spent,
            NEW.quote_received,
            NEW.last_applied_trade_id,
            NEW.active_entry_order_id,
            NEW.active_tp_order_id,
            NEW.closing_reason,
            TG_OP,
            NOW()
        );
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION _bot_state_fsm_audit() IS
    'Audit trigger for bot_state. Inserts a snapshot into bot_state_history '
    'on every INSERT and on every UPDATE where cycle_status changes.';


-- -----------------------------------------------------------------------------
-- Trigger  (DROP + CREATE for idempotent re-runs)
-- -----------------------------------------------------------------------------

DROP TRIGGER IF EXISTS bot_state_fsm_audit ON bot_state;

CREATE TRIGGER bot_state_fsm_audit
    AFTER INSERT OR UPDATE
    ON bot_state
    FOR EACH ROW
    EXECUTE FUNCTION _bot_state_fsm_audit();

COMMENT ON TRIGGER bot_state_fsm_audit ON bot_state IS
    'Fires after INSERT (always) and after UPDATE (only on cycle_status change). '
    'Delegates to _bot_state_fsm_audit().';


-- -----------------------------------------------------------------------------
-- Verification query
-- -----------------------------------------------------------------------------
--
-- SELECT trigger_name, event_manipulation, action_timing
--   FROM information_schema.triggers
--  WHERE event_object_table = 'bot_state'
--    AND trigger_name = 'bot_state_fsm_audit';
--
-- Expected: two rows — INSERT / UPDATE, AFTER.
-- -----------------------------------------------------------------------------
