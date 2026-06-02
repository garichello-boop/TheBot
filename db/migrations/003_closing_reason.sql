-- Migration 003: add closing_reason to bot_state
-- Apply: psql -U postgres -d thebot -f 003_closing_reason.sql

ALTER TABLE bot_state
    ADD COLUMN IF NOT EXISTS closing_reason TEXT DEFAULT NULL;

-- Constraint: only valid values or NULL
-- NULL is the normal state outside of CLOSING.
ALTER TABLE bot_state
    ADD CONSTRAINT bot_state_closing_reason_check CHECK (
        closing_reason IS NULL OR closing_reason IN (
            'TP', 'SL', 'FORCE_CLOSE', 'MANUAL_CANCEL'
        )
    );

-- Comment for documentation
COMMENT ON COLUMN bot_state.closing_reason IS
    'Reason for entering CLOSING state. '
    'TP=take-profit filled, SL=stop-loss triggered, '
    'FORCE_CLOSE=operator command, MANUAL_CANCEL=manual TP cancellation. '
    'NULL in all states except CLOSING. '
    'Auto-reset to NULL on CLOSING->IDLE transition by StateManager.';
