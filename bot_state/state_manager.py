from __future__ import annotations

from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from bot_state.models import BotState, CycleStatus
from bot_state.state_fsm import StateFSM, InvalidTransitionError
from bot_state.state_repo import StateRepository

if TYPE_CHECKING:
    from observability.emitter import EventEmitter


class StateInvariantError(Exception):
    """
    Raised when bot_state violates a physical invariant before persistence.
    Caller must emit STOP_CRANE_TRIGGERED with full diagnostic payload and
    halt trading. Never swallow this exception.
    """
    pass


class StateManager:
    """
    Orchestrates FSM transitions and atomic state persistence.

    Constructor
    -----------
    StateManager(db_pool, emitter=None)
      db_pool  — connection pool; passed through to StateRepository.
      emitter  — optional EventEmitter for observability.

    Strict ordering rule (TZ-6):
      1. Validate FSM transition (if cycle_status changed)
      2. Check local invariants
      3. Auto-bump version if caller used dataclasses.replace()
      4. Commit to DB
      5. Emit events AFTER commit — never inside transaction

    Write paths
    -----------
    commit()                    — canonical path for DecisionEngine.
                                  Caller builds new_state with replace() or
                                  with_updates(), calls commit(), then emits.
    transition()                — convenience: FSM + build + commit + emit.
    transition_in_transaction() — same but inside caller-owned transaction.
    update()                    — field-only update, no FSM change.
    update_in_transaction()     — same inside caller-owned transaction.
    """

    def __init__(
        self,
        db_pool,
        emitter: Optional["EventEmitter"] = None,
    ) -> None:
        self._repo = StateRepository(db_pool)
        self._emitter = emitter
        self._fsm = StateFSM()

    # ------------------------------------------------------------------
    # Canonical write path (DecisionEngine uses this)
    # ------------------------------------------------------------------

    def commit(
        self,
        old_state: BotState,
        new_state: BotState,
        trades: Optional[list] = None,
    ) -> BotState:
        """
        Validate FSM transition → check local invariants → persist → return.

        Emit events AFTER calling this method, never inside.

        Version handling
        ----------------
        Callers using dataclasses.replace() do NOT increment version.
        Callers using BotState.with_updates() DO increment version.
        commit() normalises both: if new_state.version <= old_state.version,
        version is bumped exactly once via with_updates() before saving.
        This makes replace() and with_updates() equally safe at call sites.

        P7 pattern (all bot_loop / order_manager / partial_fill code):
            new_state = self._state_manager.commit(
                state,
                replace(state, cycle_status="CLOSING", ...),
            )
            emitter.emit(...)   # always AFTER commit

        trades
        ------
        Atomic commit with trade inserts requires TradeRepository — deferred
        to Punkt 7 integration. Raises NotImplementedError if passed.

        Raises
        ------
        InvalidTransitionError  — FSM transition not allowed.
        StateInvariantError     — local invariant violated; caller → STOP_CRANE.
        RuntimeError            — DB version conflict or missing row.
        NotImplementedError     — trades passed before TradeRepository is wired.
        """
        # Step 1: FSM validation — only if cycle_status changed
        if old_state.cycle_status != new_state.cycle_status:
            self._fsm.transition(old_state.cycle_status, new_state.cycle_status)

        # Step 2: Invariant checks — before touching DB
        self._check_invariants(new_state)

        # Step 3: Version normalisation
        # replace() callers: new_state.version == old_state.version → bump.
        # with_updates() callers: new_state.version == old_state.version + 1 → skip.
        if new_state.version <= old_state.version:
            new_state = new_state.with_updates()   # bumps version, no other changes

        # Step 4: Atomic write
        if trades is not None:
            raise NotImplementedError(
                "Atomic commit with trades requires TradeRepository — "
                "implement in Punkt 7 when TradeRepository is wired in."
            )

        self._repo.save(new_state)
        return new_state

    # ------------------------------------------------------------------
    # FSM transitions (convenience wrappers)
    # ------------------------------------------------------------------

    def transition(
        self,
        state: BotState,
        to_status: CycleStatus,
        **updates,
    ) -> BotState:
        """
        Build new state, commit, emit CYCLE_STATUS_CHANGED.

        with_updates() already increments version, so commit() skips
        the auto-bump step. FSM is validated inside commit().
        """
        new_state = state.with_updates(cycle_status=to_status, **updates)
        saved = self.commit(state, new_state)
        self._emit_transition(state.cycle_status, to_status, saved)
        return saved

    def transition_in_transaction(
        self,
        conn,
        state: BotState,
        to_status: CycleStatus,
        **updates,
    ) -> BotState:
        """
        FSM transition inside an already-open transaction.
        Caller owns commit. Emit AFTER caller's commit via emit_post_commit().

        Pattern:
            with transaction() as conn:
                trade_repo.insert_in_transaction(conn, trade)
                new_state = manager.transition_in_transaction(conn, state, NEW)
            manager.emit_post_commit(old_status, new_state)
        """
        self._fsm.transition(state.cycle_status, to_status)
        new_state = state.with_updates(cycle_status=to_status, **updates)
        self._check_invariants(new_state)
        self._repo.save_in_transaction(conn, new_state)
        return new_state

    # ------------------------------------------------------------------
    # Field updates without FSM transition
    # ------------------------------------------------------------------

    def update(self, state: BotState, **updates) -> BotState:
        """
        Persist field updates without changing cycle_status.

        Use for: avg_price after DCA, pending_client_order_id before send,
        balance fields, dca_count, etc.

        Invariant checks still apply.
        Raises ValueError if cycle_status is in updates (use transition()).
        """
        if "cycle_status" in updates:
            raise ValueError(
                "Use transition() to change cycle_status. "
                "update() is for field-only changes within the same FSM state."
            )
        new_state = state.with_updates(**updates)
        self._check_invariants(new_state)
        self._repo.save(new_state)
        return new_state

    def update_in_transaction(self, conn, state: BotState, **updates) -> BotState:
        """update() variant for shared transactions."""
        if "cycle_status" in updates:
            raise ValueError(
                "Use transition_in_transaction() to change cycle_status."
            )
        new_state = state.with_updates(**updates)
        self._check_invariants(new_state)
        self._repo.save_in_transaction(conn, new_state)
        return new_state

    # ------------------------------------------------------------------
    # Initialization and loading
    # ------------------------------------------------------------------

    def initialize(
        self,
        user_id: str,
        bot_id: str,
        virtual_balance: Decimal,
    ) -> BotState:
        """
        Create fresh bot_state row (first ever start).
        Returns initial BotState(IDLE, version=0).
        """
        state = self._repo.initialize(user_id, bot_id, virtual_balance)
        if self._emitter:
            self._emitter.emit(
                event_type="STATE_LOADED",
                level="INFO",
                message=f"Bot state initialized for ({user_id}, {bot_id})",
                payload={"is_new": True, "version": state.version},
            )
        return state

    def load(
        self,
        user_id: str,
        bot_id: str,
        for_update: bool = False,
    ) -> Optional[BotState]:
        """
        Load state from DB. Returns None if row does not exist.
        for_update=True acquires row lock (used at startup).
        """
        state = self._repo.load(user_id, bot_id, for_update=for_update)
        if state is not None and self._emitter:
            self._emitter.emit(
                event_type="STATE_LOADED",
                level="INFO",
                message=f"Bot state loaded: {state.cycle_status} v{state.version}",
                payload={
                    "is_new": False,
                    "cycle_status": str(state.cycle_status),
                    "version": state.version,
                    "cycle_id": state.cycle_id,
                },
            )
        return state

    # ------------------------------------------------------------------
    # Invariant checks
    # ------------------------------------------------------------------

    def _check_invariants(self, state: BotState) -> None:
        """
        Assert physical invariants before persisting state.

        Local checks (no broker/exchange required):
          position_qty >= 0            → STOP_CRANE if violated
          virtual_balance_free >= 0    → STOP_CRANE if violated
          virtual_balance_locked >= 0  → STOP_CRANE if violated

        Exchange-dependent check (TP exists iff IN_POSITION) belongs in
        Punkt 7 where TickContext and broker access are defined.

        Raises StateInvariantError listing all violations. Caller is
        responsible for emitting STOP_CRANE_TRIGGERED with diagnostic payload.
        """
        violations: list[str] = []

        if state.position_qty < Decimal("0"):
            violations.append(
                f"position_qty={state.position_qty} < 0 "
                "(negative position is a critical bug)"
            )
        if state.virtual_balance_free < Decimal("0"):
            violations.append(
                f"virtual_balance_free={state.virtual_balance_free} < 0 "
                "(negative free balance indicates calculation error)"
            )
        if state.virtual_balance_locked < Decimal("0"):
            violations.append(
                f"virtual_balance_locked={state.virtual_balance_locked} < 0 "
                "(negative locked balance indicates calculation error)"
            )

        if violations:
            raise StateInvariantError(
                f"Invariant violation for ({state.user_id}/{state.bot_id}) "
                f"v{state.version}: " + "; ".join(violations)
            )

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
        from_status,
        to_status,
        state: BotState,
    ) -> None:
        if not self._emitter:
            return
        try:
            self._emitter.emit(
                event_type="CYCLE_STATUS_CHANGED",
                level="INFO",
                message=f"FSM: {from_status} -> {to_status}",
                payload={
                    "from_status": str(from_status),
                    "to_status":   str(to_status),
                    "cycle_id":    state.cycle_id,
                    "version":     state.version,
                },
            )
        except Exception:
            # Emitter errors are isolated — never stop trading logic
            pass
