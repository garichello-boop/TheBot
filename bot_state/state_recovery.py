from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from bot_state.models import BotState, CycleStatus, OperationalStatus
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

    Entry point
    -----------
    StateRecovery.startup(
        user_id=..., bot_id=..., broker=...,
        state_repo=..., state_manager=..., registry_repo=...,
        emitter=...,          # optional
        virtual_balance=...,  # required only on first run
    )

    All dependencies are passed as arguments to the classmethod — no
    prior instance construction needed. Internally creates a temporary
    instance to share state across the startup steps.

    Responsibilities
    ----------------
    1. Duplicate process detection (registry heartbeat guard).
    2. Load or initialize bot_state (SELECT FOR UPDATE as second lock).
    3. Reconcile persisted state with exchange.
    4. Return verified BotState ready for trading.

    Full reconciliation (TP/DCA order matching, fill replay, position
    verification) is implemented in Punkt 7 alongside TickContext and
    DecisionEngine where broker interaction patterns are fully defined.
    """

    HEARTBEAT_TIMEOUT_SEC: int = 300

    def __init__(
        self,
        user_id: str,
        bot_id: str,
        state_repo: StateRepository,
        state_manager: StateManager,
        registry_repo: RegistryRepository,
        emitter: Optional["EventEmitter"],
        virtual_balance: Optional[Decimal],
    ) -> None:
        self._user_id         = user_id
        self._bot_id          = bot_id
        self._state_repo      = state_repo
        self._state_manager   = state_manager
        self._registry_repo   = registry_repo
        self._emitter         = emitter
        self._virtual_balance = virtual_balance

    # ------------------------------------------------------------------
    # Public entry point (classmethod — called without prior instantiation)
    # ------------------------------------------------------------------

    @classmethod
    def startup(
        cls,
        user_id: str,
        bot_id: str,
        broker: "IBroker",
        state_repo: StateRepository,
        state_manager: StateManager,
        registry_repo: RegistryRepository,
        emitter: Optional["EventEmitter"] = None,
        virtual_balance: Optional[Decimal] = None,
    ) -> BotState:
        """
        Full startup sequence. Returns verified BotState ready for trading.

        Called from bot.py as:
            StateRecovery.startup(
                user_id=user_id, bot_id=bot_id, broker=broker,
                state_repo=state_repo, state_manager=state_manager,
                registry_repo=registry_repo, emitter=emitter,
            )

        virtual_balance
        ---------------
        Required only when bot_state row does not yet exist (first run).
        Source: bot_config.virtual_balance from ConfigWatcher/ConfigRepository.
        If None and no state row exists, raises RuntimeError with a clear
        message — bot.py should be updated to pass bot_config.virtual_balance.

        Raises
        ------
        BotAlreadyRunningError  — another live process detected via heartbeat.
        DuplicateBotError       — bot_state row locked by another process.
        RuntimeError            — first run without virtual_balance provided.
        """
        instance = cls(
            user_id, bot_id,
            state_repo, state_manager, registry_repo,
            emitter, virtual_balance,
        )
        return instance._run(broker)

    # ------------------------------------------------------------------
    # Internal startup sequence
    # ------------------------------------------------------------------

    def _run(self, broker: "IBroker") -> BotState:
        """Execute the 6-step startup sequence."""

        # Step 1: heartbeat guard — detect live duplicate process
        self._check_registry()

        # Step 2: mark STARTING in registry
        self._registry_repo.upsert(
            self._user_id, self._bot_id,
            status=OperationalStatus.STARTING,
            started_at=datetime.now(timezone.utc),
        )

        # Steps 3 & 4: load existing state or initialize on first run
        state = self._load_or_initialize()

        # Step 5: reconcile persisted state with exchange
        state = self._reconcile(state, broker)

        # Step 6: mark RUNNING
        self._registry_repo.upsert(
            self._user_id, self._bot_id,
            status=OperationalStatus.RUNNING,
            last_heartbeat=datetime.now(timezone.utc),
        )

        self._emit(
            "BOT_STARTED",
            "INFO",
            f"Bot ({self._user_id}/{self._bot_id}) ready. "
            f"Status: {state.cycle_status}, version: {state.version}",
            {
                "cycle_status":          str(state.cycle_status),
                "version":               state.version,
                "cycle_id":              state.cycle_id,
                "virtual_balance_free":  str(state.virtual_balance_free),
            },
        )

        return state

    # ------------------------------------------------------------------
    # Step 1: duplicate process guard
    # ------------------------------------------------------------------

    def _check_registry(self) -> None:
        registry = self._registry_repo.load(self._user_id, self._bot_id)
        if registry is None:
            return  # first ever start — no conflict possible

        if registry.operational_status != OperationalStatus.RUNNING:
            return  # stopped / error / starting — safe to proceed

        if registry.last_heartbeat is None:
            return  # RUNNING but no heartbeat — treat as stale

        now = datetime.now(timezone.utc)
        heartbeat = registry.last_heartbeat
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)

        age_sec = (now - heartbeat).total_seconds()
        if age_sec < self.HEARTBEAT_TIMEOUT_SEC:
            raise BotAlreadyRunningError(
                f"Bot ({self._user_id}/{self._bot_id}) appears to be running. "
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
    # Steps 3 & 4: load or initialize
    # ------------------------------------------------------------------

    def _load_or_initialize(self) -> BotState:
        # SELECT FOR UPDATE NOWAIT — raises DuplicateBotError if locked
        state = self._state_repo.load(
            self._user_id, self._bot_id, for_update=True
        )

        if state is None:
            if self._virtual_balance is None:
                raise RuntimeError(
                    f"No bot_state row found for ({self._user_id}/{self._bot_id}) "
                    "and virtual_balance was not provided. "
                    "Pass virtual_balance=bot_config.virtual_balance to "
                    "StateRecovery.startup() on first run."
                )
            state = self._state_manager.initialize(
                self._user_id, self._bot_id, self._virtual_balance
            )
            self._emit(
                "STATE_LOADED",
                "INFO",
                "First run — bot_state initialized.",
                {"is_new": True, "virtual_balance": str(self._virtual_balance)},
            )
        else:
            self._emit(
                "STATE_LOADED",
                "INFO",
                f"State loaded: {state.cycle_status} v{state.version}",
                {
                    "is_new":        False,
                    "cycle_status":  str(state.cycle_status),
                    "version":       state.version,
                    "cycle_id":      state.cycle_id,
                    "has_position":  state.has_position,
                },
            )

        return state

    # ------------------------------------------------------------------
    # Step 5: reconciliation
    # ------------------------------------------------------------------

    def _reconcile(self, state: BotState, broker: "IBroker") -> BotState:
        """
        Reconcile persisted state with the exchange.

        MVP: handles IDLE (trivial) and STOP_CRANE (blocks trading).
        Full reconciliation — TP/DCA order matching, fill replay, position
        verification, FSM restore — is deferred to Punkt 7 where TickContext
        and broker interaction patterns are fully defined.
        """
        self._emit(
            "RECONCILIATION_STARTED",
            "INFO",
            "Starting reconciliation with exchange.",
            {"cycle_status": str(state.cycle_status)},
        )

        if state.cycle_status == CycleStatus.STOP_CRANE:
            self._emit(
                "RECONCILIATION_ERROR",
                "WARNING",
                "Bot state is STOP_CRANE. Manual operator resolution required "
                "before trading can resume.",
                {"cycle_status": str(CycleStatus.STOP_CRANE)},
            )
            # Return as-is — BotLoop checks STOP_CRANE and halts
            return state

        if state.cycle_status == CycleStatus.IDLE:
            self._emit(
                "RECONCILIATION_FINISHED",
                "INFO",
                "Reconciliation complete: IDLE state, no open position.",
                {},
            )
            return state

        # Non-IDLE: full reconciliation deferred to Punkt 7
        # Steps from TZ-7 Startup/Restart Recovery:
        #   1. Load all open orders from exchange by ticker
        #   2. Match by client_order_id / pending_client_order_id
        #   3. Replay fills since last_applied_trade_id
        #   4. Verify position_qty vs exchange
        #   5. Restore FSM to correct state
        #   6. Handle orphaned orders, missing TP, etc.
        self._emit(
            "RECONCILIATION_FINISHED",
            "INFO",
            f"Reconciliation deferred to Punkt 7 for non-IDLE state: "
            f"{state.cycle_status}. State accepted from DB.",
            {"cycle_status": str(state.cycle_status)},
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
