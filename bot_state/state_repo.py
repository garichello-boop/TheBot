from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from db.connection import get_connection, transaction
from bot_state.models import BotState, ClosingReason, CycleStatus, StateHistoryRow


class DuplicateBotError(Exception):
    """Raised when bot_state row is locked by another process."""
    pass


class StateRepository:
    """
    Read and write bot_state table.

    Stateless repository: db_pool accepted at construction; user_id/bot_id
    are passed to load() and initialize(). save() extracts both identifiers
    from the BotState object itself.

    Optimistic concurrency: save() checks WHERE version = state.version - 1.
    rowcount == 0 → RuntimeError (version conflict or missing row).

    get_connection() / transaction() use the globally configured pool
    (initialised by create_pool() in bot.py). self._pool is stored for
    future direct-pool usage.

    Audit trail:
        get_history() reads bot_state_history — the append-only FSM
        transition log written by the _bot_state_fsm_audit trigger.
    """

    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(
        self,
        user_id: str,
        bot_id: str,
        for_update: bool = False,
    ) -> Optional[BotState]:
        """
        Load bot_state row. Returns None if row does not exist yet.

        for_update=True: acquires row-level lock (SELECT FOR UPDATE NOWAIT).
        Used at startup to prevent duplicate processes.
        Raises DuplicateBotError if another process holds the lock.
        """
        sql = """
            SELECT
                user_id, bot_id, version, cycle_id, cycle_status,
                virtual_balance_free, virtual_balance_locked,
                position_qty, position_avg_price, dca_count,
                quote_spent, quote_received, last_applied_trade_id,
                active_entry_order_id, active_tp_order_id,
                active_dca_order_ids, pending_client_order_id,
                entered_at, last_order_at, updated_at,
                closing_reason
            FROM bot_state
            WHERE user_id = %s AND bot_id = %s
        """
        if for_update:
            sql += " FOR UPDATE NOWAIT"

        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(sql, (user_id, bot_id))
                except Exception as exc:
                    conn.rollback()
                    if "could not obtain lock" in str(exc).lower():
                        raise DuplicateBotError(
                            f"bot_state row is locked for ({user_id}, {bot_id}). "
                            "Another process is likely running this bot."
                        ) from exc
                    raise

                row = cur.fetchone()
                if row is None:
                    return None
                return BotState.from_row(dict(row))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def initialize(
        self,
        user_id: str,
        bot_id: str,
        virtual_balance: Decimal,
    ) -> BotState:
        """
        Insert a fresh bot_state row. Called once on first ever run.
        Returns the created BotState.
        """
        state = BotState.initial(user_id, bot_id, virtual_balance)
        sql = """
            INSERT INTO bot_state (
                user_id, bot_id, version, cycle_id, cycle_status,
                virtual_balance_free, virtual_balance_locked,
                position_qty, position_avg_price, dca_count,
                quote_spent, quote_received, last_applied_trade_id,
                active_entry_order_id, active_tp_order_id,
                active_dca_order_ids, pending_client_order_id,
                entered_at, last_order_at, updated_at,
                closing_reason
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s
            )
        """
        with transaction() as cur:
            cur.execute(sql, _to_params(state))
        return state

    def save(self, state: BotState) -> None:
        """
        Overwrite bot_state row with optimistic version check.

        WHERE version = state.version - 1 ensures no concurrent write
        slipped in between our load and this save.
        Raises RuntimeError on version conflict or missing row.
        """
        sql = _build_update_sql()
        params = _to_update_params(state)

        with transaction() as cur:
            cur.execute(sql, params)
            if cur.rowcount == 0:
                raise RuntimeError(
                    f"bot_state save failed for ({state.user_id}, {state.bot_id}): "
                    f"version conflict or row missing. "
                    f"Expected DB version {state.version - 1}, "
                    f"state version {state.version}."
                )

    def save_in_transaction(self, conn, state: BotState) -> None:
        """
        Write bot_state inside an already-open transaction.
        Caller owns commit/rollback. Same version check as save().
        rowcount validation is caller's responsibility.
        """
        sql = _build_update_sql()
        params = _to_update_params(state)
        with conn.cursor() as cur:
            cur.execute(sql, params)

    # ------------------------------------------------------------------
    # Audit trail: FSM history
    # ------------------------------------------------------------------

    def get_history(
        self,
        user_id: str,
        bot_id: str,
        limit: int = 50,
    ) -> list[StateHistoryRow]:
        """
        Return the N most recent FSM transitions for a bot, newest first.

        Each entry is a full snapshot of bot_state at the moment
        cycle_status changed, captured by the _bot_state_fsm_audit trigger.

        Args:
            limit: maximum rows to return (default 50).

        Returns:
            List of StateHistoryRow ordered by id DESC (newest first).
            Empty list if no history exists yet.

        Example:
            history = repo.get_history("igor", "btc_paper_01", limit=10)
            for h in history:
                print(h.transition_label, h.recorded_at)
            # IN_POSITION → CLOSING  2025-11-01T14:23:11+00:00
            # ENTERING → IN_POSITION 2025-11-01T14:20:05+00:00
            # IDLE → ENTERING        2025-11-01T14:19:58+00:00
        """
        query = """
            SELECT
                id, user_id, bot_id,
                old_cycle_status, new_cycle_status,
                version, cycle_id,
                virtual_balance_free, virtual_balance_locked,
                position_qty, position_avg_price,
                dca_count, quote_spent, quote_received,
                last_applied_trade_id,
                active_entry_order_id, active_tp_order_id,
                closing_reason, trigger_op, recorded_at
            FROM bot_state_history
            WHERE user_id = %s AND bot_id = %s
            ORDER BY id DESC
            LIMIT %s
        """
        with transaction() as cur:
            cur.execute(query, (user_id, bot_id, limit))
            rows = cur.fetchall()

        return [StateHistoryRow.from_row(dict(r)) for r in (rows or [])]


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _cs_value(cycle_status) -> str:
    """
    Serialize cycle_status to DB string.

    Handles both CycleStatus enum (normal path) and plain str
    (produced by dataclasses.replace() calls in P7 code that pass
    cycle_status as a raw string literal).
    """
    if isinstance(cycle_status, CycleStatus):
        return cycle_status.value
    return str(cycle_status)


def _cr_value(closing_reason) -> Optional[str]:
    """
    Serialize closing_reason to DB string or None.

    Handles ClosingReason enum, plain str, and None.
    """
    if closing_reason is None:
        return None
    if isinstance(closing_reason, ClosingReason):
        return closing_reason.value
    return str(closing_reason)


def _build_update_sql() -> str:
    return """
        UPDATE bot_state SET
            version                 = %s,
            cycle_id                = %s,
            cycle_status            = %s,
            virtual_balance_free    = %s,
            virtual_balance_locked  = %s,
            position_qty            = %s,
            position_avg_price      = %s,
            dca_count               = %s,
            quote_spent             = %s,
            quote_received          = %s,
            last_applied_trade_id   = %s,
            active_entry_order_id   = %s,
            active_tp_order_id      = %s,
            active_dca_order_ids    = %s,
            pending_client_order_id = %s,
            entered_at              = %s,
            last_order_at           = %s,
            updated_at              = %s,
            closing_reason          = %s
        WHERE user_id = %s
          AND bot_id  = %s
          AND version = %s
    """


def _to_update_params(state: BotState) -> tuple:
    now = datetime.now(timezone.utc)
    return (
        state.version,
        state.cycle_id,
        _cs_value(state.cycle_status),
        state.virtual_balance_free,
        state.virtual_balance_locked,
        state.position_qty,
        state.position_avg_price,
        state.dca_count,
        state.quote_spent,
        state.quote_received,
        state.last_applied_trade_id,
        state.active_entry_order_id,
        state.active_tp_order_id,
        list(state.active_dca_order_ids),
        state.pending_client_order_id,
        state.entered_at,
        state.last_order_at,
        now,
        _cr_value(state.closing_reason),
        # WHERE clause
        state.user_id,
        state.bot_id,
        state.version - 1,          # expected current DB version
    )


def _to_params(state: BotState) -> tuple:
    """Serialize BotState to INSERT params tuple."""
    now = datetime.now(timezone.utc)
    return (
        state.user_id,
        state.bot_id,
        state.version,
        state.cycle_id,
        _cs_value(state.cycle_status),
        state.virtual_balance_free,
        state.virtual_balance_locked,
        state.position_qty,
        state.position_avg_price,
        state.dca_count,
        state.quote_spent,
        state.quote_received,
        state.last_applied_trade_id,
        state.active_entry_order_id,
        state.active_tp_order_id,
        list(state.active_dca_order_ids),
        state.pending_client_order_id,
        state.entered_at,
        state.last_order_at,
        now,
        _cr_value(state.closing_reason),
    )
