-- =============================================================================
-- Migration 006: bot_state.active_tp_price
-- =============================================================================
--
-- Stores the price of the active TP LIMIT order alongside its order_id.
--
-- Why a separate column:
--   PaperBroker keeps pending orders in-memory (_pending dict).
--   On restart that dict is empty — the TP order ID is in bot_state but
--   the price is not recoverable without either re-querying the exchange
--   (impossible for PaperBroker) or deriving from strategy_params
--   (inaccurate after DCA because avg_price changed).
--
--   Storing active_tp_price in bot_state makes it available during
--   StateRecovery._reconcile_in_position() which runs at every startup.
--   If the TP order vanished (paper restart) and the price was hit during
--   downtime, reconciliation fetches historical klines and simulates the fill.
--
-- Lifecycle:
--   SET:   OrderManager.place_tp_order() writes the price.
--          DCAScheduler.recreate_tp_after_dca() calls place_tp_order()
--          which overwrites with the new TP price.
--   CLEAR: Implicitly stale after IDLE transition; overwritten at next entry.
--          Stale values in IDLE state are ignored by reconciliation
--          (playback only runs for IN_POSITION).
--
-- Note on bot_state_history:
--   The _bot_state_fsm_audit trigger uses explicit column lists and will
--   NOT capture active_tp_price in history. This is acceptable for the
--   current audit trail scope; add it to the trigger in a later migration
--   if needed.
--
-- Apply:
--   psql -U postgres -d thebot -f 006_bot_state_tp_price.sql
-- =============================================================================

ALTER TABLE bot_state
    ADD COLUMN IF NOT EXISTS active_tp_price NUMERIC(20, 8);

COMMENT ON COLUMN bot_state.active_tp_price IS
    'Price of the active TP LIMIT (SELL) order. '
    'Set by OrderManager.place_tp_order(). '
    'Used by StateRecovery for OHLCV playback on PaperBroker restart.';

-- Verification:
-- SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--  WHERE table_name = 'bot_state'
--    AND column_name = 'active_tp_price';
