from __future__ import annotations

from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from bot_state.models import BotState, CycleStatus
from bot_state.state_fsm import StateFSM, InvalidTransitionError
from bot_state.state_repo import StateRepository

if TYPE_CHECKING:
    from observability.emitter import EventEmitter


class StateManager:
    """
    Orchestrates FSM transitions and atomic state persistence.

    Strict ordering rule (from TZ-6):
        1. Validate FSM transition
        2. Build new state via with_updates()
        3. Commit to DB (inside transaction)
        4. Emit events AFTER commit — never inside transaction

    emitter is optional to allow use without observability (tests, scripts).
    """

    def __init__(
        self,
        repo: StateRepository,
        emitter: Optional["EventEmitter"] = None,
    ) -> None:
        self._repo = repo
        self._emitter = emitter
        self._fsm = StateFSM()

    # ------------------------------------------------------------------
    # FSM transitions
    # ------------------------------------------------------------------

    def transition(
        self,
        state: BotState,
        to_status: CycleStatus,
        **updates,
    ) -> BotState:
        """
        Validate FSM transition, apply updates, persist, return new state.

        Additional field updates are passed as keyword arguments.
        version is auto-incremented by BotState.with_updates().

        Raises InvalidTransitionError if transition is not allowed.
        Raises RuntimeError if DB save fails (version conflict).
        """
        # Step 1: validate transition
        self._fsm.transition(state.cycle_status, to_status)

        # Step 2: build new state
        new_state = state.with_updates(cycle_status=to_status, **updates)

        # Step 3: commit
        self._repo.save(new_state)

        # Step 4: emit after commit
        self._emit_transition(state.cycle_status, to_status, new_state)

        return new_state

    def transition_in_transaction(
        self,
        conn,
        state: BotState,
        to_status: CycleStatus,
        **updates,
    ) -> BotState:
        """
        Same as transition() but writes into an already-open transaction.
        Used when state update must be atomic with a trade insert.
        Caller owns commit. Emit must happen AFTER caller's commit.

        Pattern:
            with transaction() as conn:
                trade_repo.insert_in_transaction(conn, trade)
                new_state = manager.transition_in_transaction(conn, state, ...)
            # commit happened — now emit
            manager.emit_post_commit(old_status, new_state)
        """
        self._fsm.transition(state.cycle_status, to_status)
        new_state = state.with_updates(cycle_status=to_status, **updates)
        self._repo.save_in_transaction(conn, new_state)
        return new_state

    # ------------------------------------------------------------------
    # Field updates without FSM transition
    # ------------------------------------------------------------------

    def update(self, state: BotState, **updates) -> BotState:
        """
        Persist field updates without changing cycle_status.
        Used for: updating avg_price after DCA, storing pending_client_order_id,
        updating balance fields, etc.

        Does NOT validate FSM (cycle_status unchanged).
        """
        if "cycle_status" in updates:
            raise ValueError(
                "Use transition() to change cycle_status. "
                "update() is for field-only changes within the same FSM state."
            )
        new_state = state.with_updates(**updates)
        self._repo.save(new_state)
        return new_state

    def update_in_transaction(self, conn, state: BotState, **updates) -> BotState:
        """update() variant for shared transactions."""
        if "cycle_status" in updates:
            raise ValueError(
                "Use transition_in_transaction() to change cycle_status."
            )
        new_state = state.with_updates(**updates)
        self._repo.save_in_transaction(conn, new_state)
        return new_state

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self, virtual_balance: Decimal) -> BotState:
        """
        Create fresh bot_state row (first ever start).
        Returns initial BotState(IDLE).
        """
        state = self._repo.initialize(virtual_balance)
        if self._emitter:
            self._emitter.emit(
                event_type="STATE_LOADED",
                level="INFO",
                message=f"Bot state initialized for ({self._repo.user_id}, {self._repo.bot_id})",
                payload={"is_new": True, "version": state.version},
            )
        return state

    def load(self, for_update: bool = False) -> Optional[BotState]:
        """
        Load state from DB. Returns None if row does not exist.
        for_update=True acquires row lock (used at startup).
        """
        state = self._repo.load(for_update=for_update)
        if state is not None and self._emitter:
            self._emitter.emit(
                event_type="STATE_LOADED",
                level="INFO",
                message=f"Bot state loaded: {state.cycle_status.value} v{state.version}",
                payload={
                    "is_new": False,
                    "cycle_status": state.cycle_status.value,
                    "version": state.version,
                    "cycle_id": state.cycle_id,
                },
            )
        return state

    # ------------------------------------------------------------------
    # Emit helpers
    # ------------------------------------------------------------------

    def emit_post_commit(
        self,
        from_status: CycleStatus,
        new_state: BotState,
    ) -> None:
        """
        Emit transition event after external commit.
        Called by caller when using transition_in_transaction().
        """
        self._emit_transition(from_status, new_state.cycle_status, new_state)

    def _emit_transition(
        self,
        from_status: CycleStatus,
        to_status: CycleStatus,
        state: BotState,
    ) -> None:
        if not self._emitter:
            return
        try:
            self._emitter.emit(
                event_type="CYCLE_STATUS_CHANGED",
                level="INFO",
                message=f"FSM: {from_status.value} -> {to_status.value}",
                payload={
                    "from_status": from_status.value,
                    "to_status": to_status.value,
                    "cycle_id": state.cycle_id,
                    "version": state.version,
                },
            )
        except Exception:
            # Emitter errors are isolated — never stop trading logic
            pass
