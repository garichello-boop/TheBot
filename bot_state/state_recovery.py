from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from bot_state.models import BotState, BotRegistry, CycleStatus, OperationalStatus
from bot_state.state_repo import StateRepository, DuplicateBotError
from bot_state.registry_repo import RegistryRepository
from bot_state.state_manager import StateManager

if TYPE_CHECKING:
    from broker.broker import IBroker
    from observability.emitter import EventEmitter


class BotAlreadyRunningError(Exception):
    """Raised when another live process is running this bot."""
    pass


class ReconciliationError(Exception):
    """Raised when bot_state conflicts with exchange and cannot be resolved."""
    pass


class StateRecovery:
    """
    Startup sequence and reconciliation for bot_state.

    Responsibilities:
    1. Duplicate process detection (bot_registry heartbeat + SELECT FOR UPDATE).
    2. Load or initialize bot_state.
    3. Reconcile persisted state with exchange (broker).
    4. Return a verified BotState ready for trading.

    Reconciliation with broker (steps 4-8 from TZ-6 Startup/Restart Recovery)
    is a skeleton in this iteration — full implementation belongs to Punkt 7
    where TickContext, DecisionEngine and broker interaction patterns are defined.
    """

    # How old a heartbeat must be to consider the previous process dead
    HEARTBEAT_TIMEOUT_SEC: int = 300

    def __init__(
        self,
        user_id: str,
        bot_id: str,
        emitter: Optional["EventEmitter"] = None,
    ) -> None:
        self.user_id = user_id
        self.bot_id = bot_id
        self._emitter = emitter
        self._state_repo = StateRepository(user_id, bot_id)
        self._registry_repo = RegistryRepository(user_id, bot_id)
        self._manager = StateManager(self._state_repo, emitter)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def startup(
        self,
        virtual_balance: Decimal,
        broker: Optional["IBroker"] = None,
    ) -> BotState:
        """
        Full startup sequence. Returns verified BotState ready for trading.

        Steps:
        1. Check bot_registry for running process (heartbeat guard).
        2. Mark registry as STARTING.
        3. Load bot_state (SELECT FOR UPDATE — second lock layer).
        4. Initialize if first run.
        5. Reconcile with exchange.
        6. Mark registry as RUNNING.
        7. Return state.

        Raises BotAlreadyRunningError if another live process detected.
        Raises DuplicateBotError if bot_state row is locked by another process.
        """
        # Step 1: heartbeat guard
        self._check_registry()

        # Step 2: mark STARTING in registry
        self._registry_repo.upsert(
            status=OperationalStatus.STARTING,
            started_at=datetime.now(timezone.utc),
        )

        # Step 3 & 4: load or initialize state
        state = self._load_or_initialize(virtual_balance)

        # Step 5: reconcile with exchange
        state = self._reconcile(state, broker)

        # Step 6: mark RUNNING
        self._registry_repo.upsert(
            status=OperationalStatus.RUNNING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        self._emit(
            "BOT_STARTED",
            "INFO",
            f"Bot ({self.user_id}/{self.bot_id}) ready. "
            f"Status: {state.cycle_status.value}, version: {state.version}",
            {
                "cycle_status": state.cycle_status.value,
                "version": state.version,
                "cycle_id": state.cycle_id,
                "virtual_balance_free": str(state.virtual_balance_free),
            },
        )

        return state

    def shutdown(self, state: Optional[BotState] = None) -> None:
        """Mark registry as STOPPED. Called on clean shutdown."""
        self._registry_repo.mark_stopped()
        self._emit("BOT_STOPPED", "INFO", "Bot stopped cleanly.", {})

    def crash(self, error_message: str) -> None:
        """Mark registry as ERROR. Called on unhandled exception."""
        self._registry_repo.mark_error(error_message)
        self._emit(
            "BOT_CRASHED",
            "CRITICAL",
            f"Bot crashed: {error_message}",
            {"error_message": error_message},
        )

    # ------------------------------------------------------------------
    # Step 1: duplicate process guard
    # ------------------------------------------------------------------

    def _check_registry(self) -> None:
        registry = self._registry_repo.load()
        if registry is None:
            return  # first ever start — no conflict possible

        if registry.operational_status != OperationalStatus.RUNNING:
            return  # stopped, error, starting — safe to proceed

        # Status is RUNNING — check heartbeat age
        if registry.last_heartbeat is None:
            return  # RUNNING but no heartbeat — treat as stale, allow start

        now = datetime.now(timezone.utc)
        heartbeat = registry.last_heartbeat
        # Make heartbeat timezone-aware if DB returned naive datetime
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)

        age_sec = (now - heartbeat).total_seconds()
        if age_sec < self.HEARTBEAT_TIMEOUT_SEC:
            raise BotAlreadyRunningError(
                f"Bot ({self.user_id}/{self.bot_id}) appears to be running. "
                f"PID {registry.pid}, heartbeat {age_sec:.0f}s ago "
                f"(timeout={self.HEARTBEAT_TIMEOUT_SEC}s). "
                "Stop the running process before starting a new one."
            )
        # Heartbeat is stale — previous process is considered dead
        self._emit(
            "BOT_STARTED",
            "WARNING",
            f"Stale heartbeat detected ({age_sec:.0f}s). "
            "Previous process considered dead. Proceeding with recovery.",
            {"stale_heartbeat_age_sec": age_sec, "previous_pid": registry.pid},
        )

    # ------------------------------------------------------------------
    # Step 3 & 4: load or initialize
    # ------------------------------------------------------------------

    def _load_or_initialize(self, virtual_balance: Decimal) -> BotState:
        # SELECT FOR UPDATE NOWAIT — raises DuplicateBotError if locked
        state = self._state_repo.load(for_update=True)

        if state is None:
            state = self._manager.initialize(virtual_balance)
            self._emit(
                "STATE_LOADED",
                "INFO",
                "First run — bot_state initialized.",
                {"is_new": True, "virtual_balance": str(virtual_balance)},
            )
        else:
            self._emit(
                "STATE_LOADED",
                "INFO",
                f"State loaded: {state.cycle_status.value} v{state.version}",
                {
                    "is_new": False,
                    "cycle_status": state.cycle_status.value,
                    "version": state.version,
                    "cycle_id": state.cycle_id,
                    "has_position": state.has_position,
                },
            )

        return state

    # ------------------------------------------------------------------
    # Step 5: reconciliation
    # ------------------------------------------------------------------

    def _reconcile(
        self,
        state: BotState,
        broker: Optional["IBroker"],
    ) -> BotState:
        """
        Reconcile persisted state with the exchange.

        MVP: handles IDLE (trivial) and emits RECONCILIATION_STARTED/FINISHED.
        Full reconciliation (TP/DCA order matching, fill replay, position
        verification) is implemented in Punkt 7 alongside TickContext and
        DecisionEngine where broker interaction patterns are fully defined.

        If broker is None (paper trading startup, tests) — skip exchange check.
        """
        self._emit(
            "RECONCILIATION_STARTED",
            "INFO",
            "Starting reconciliation with exchange.",
            {"cycle_status": state.cycle_status.value, "broker_available": broker is not None},
        )

        if state.cycle_status == CycleStatus.STOP_CRANE:
            # Bot left off in emergency stop — do not trade, wait for operator
            self._emit(
                "RECONCILIATION_ERROR",
                "WARNING",
                "Bot state is STOP_CRANE. Manual operator resolution required "
                "before trading can resume.",
                {"cycle_status": CycleStatus.STOP_CRANE.value},
            )
            # Return state as-is — BotLoop will check STOP_CRANE and halt
            return state

        if state.cycle_status == CycleStatus.IDLE:
            # No open position — nothing to reconcile with exchange
            self._emit(
                "RECONCILIATION_FINISHED",
                "INFO",
                "Reconciliation complete: IDLE state, no open position.",
                {},
            )
            return state

        if broker is None:
            # No broker available (e.g. PaperBroker not yet connected, tests)
            # Acceptable for paper trading — positions survive in DB
            self._emit(
                "RECONCILIATION_FINISHED",
                "INFO",
                "Broker not available — skipping exchange reconciliation. "
                "Persisted state accepted as-is.",
                {"cycle_status": state.cycle_status.value},
            )
            return state

        # Non-IDLE state with broker available:
        # Full reconciliation (Punkt 7):
        # - Query all open orders from exchange by ticker
        # - Match by client_order_id / pending_client_order_id
        # - Replay fills since last_applied_trade_id
        # - Verify position_qty matches exchange
        # - Restore FSM to correct state
        # - Handle orphaned orders, missing TP, etc.
        self._emit(
            "RECONCILIATION_FINISHED",
            "INFO",
            f"Reconciliation deferred to Punkt 7 for non-IDLE state: "
            f"{state.cycle_status.value}. State accepted from DB.",
            {"cycle_status": state.cycle_status.value},
        )
        return state

    # ------------------------------------------------------------------
    # Emit helper
    # ------------------------------------------------------------------

    def _emit(
        self,
        event_type: str,
        level: str,
        message: str,
        payload: dict,
    ) -> None:
        if not self._emitter:
            return
        try:
            self._emitter.emit(
                event_type=event_type,
                level=level,
                message=message,
                payload=payload,
            )
        except Exception:
            pass
