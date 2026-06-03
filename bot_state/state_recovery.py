from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple, Set, List, Any, TYPE_CHECKING

from bot_state.models import BotState, CycleStatus, OperationalStatus
from bot_state.state_repo import StateRepository, DuplicateBotError
from bot_state.registry_repo import RegistryRepository
from bot_state.state_manager import StateManager

if TYPE_CHECKING:
    from broker.broker import IBroker
    from observability.emitter import EventEmitter

logger = logging.getLogger(__name__)


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
        user_id=..., bot_id=..., ticker=..., broker=...,
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
    3. Reconcile persisted state with exchange (8-step TZ-7 recovery).
    4. Return verified BotState ready for trading.

    Reconciliation (Step 3) handles all non-IDLE, non-STOP_CRANE states:
    ENTERING / IN_POSITION / CLOSING / WAITING_FOR_LIQUIDITY.

    For PaperBroker: get_fills() always returns [] and get_open_orders()
    only sees in-memory orders (lost on restart). Reconciliation degrades
    gracefully — DB state is accepted as-is for position fields, orphaned
    order IDs are cleared.

    For BybitBroker: full reconciliation against live exchange data.
    """

    HEARTBEAT_TIMEOUT_SEC: int = 300
    OHLCV_MIN_GAP_SEC:      int = 60

    def __init__(
        self,
        user_id: str,
        bot_id: str,
        ticker: str,
        state_repo: StateRepository,
        state_manager: StateManager,
        registry_repo: RegistryRepository,
        emitter: Optional["EventEmitter"],
        virtual_balance: Optional[Decimal],
        market: Optional[Any] = None,
    ) -> None:
        self._user_id         = user_id
        self._bot_id          = bot_id
        self._ticker          = ticker
        self._state_repo      = state_repo
        self._state_manager   = state_manager
        self._registry_repo   = registry_repo
        self._emitter         = emitter
        self._virtual_balance = virtual_balance
        self._market          = market

    # ------------------------------------------------------------------
    # Public entry point (classmethod — called without prior instantiation)
    # ------------------------------------------------------------------

    @classmethod
    def startup(
        cls,
        user_id: str,
        bot_id: str,
        ticker: str,
        broker: "IBroker",
        state_repo: StateRepository,
        state_manager: StateManager,
        registry_repo: RegistryRepository,
        emitter: Optional["EventEmitter"] = None,
        virtual_balance: Optional[Decimal] = None,
        market: Optional[Any] = None,
    ) -> BotState:
        """
        Full startup sequence. Returns verified BotState ready for trading.

        Called from bot.py as:
            StateRecovery.startup(
                user_id=user_id, bot_id=bot_id, ticker=ticker,
                broker=broker, state_repo=state_repo,
                state_manager=state_manager, registry_repo=registry_repo,
                emitter=emitter, virtual_balance=bot_config.virtual_balance,
            )

        ticker
        ------
        Required for reconciliation: broker.get_open_orders(ticker) and
        broker.get_fills(ticker, ...) filter by instrument. Pass
        bot_config.ticker — already available in bot.py before this call.

        virtual_balance
        ---------------
        Required only when bot_state row does not yet exist (first run).
        Source: bot_config.virtual_balance from ConfigWatcher/ConfigRepository.
        If None and no state row exists, raises RuntimeError with a clear
        message.

        Raises
        ------
        BotAlreadyRunningError  — another live process detected via heartbeat.
        DuplicateBotError       — bot_state row locked by another process.
        RuntimeError            — first run without virtual_balance provided.
        """
        instance = cls(
            user_id, bot_id, ticker,
            state_repo, state_manager, registry_repo,
            emitter, virtual_balance,
            market=market,
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
    # Step 5: reconciliation — main dispatcher
    # ------------------------------------------------------------------

    def _reconcile(self, state: BotState, broker: "IBroker") -> BotState:
        """
        8-step Startup/Restart Recovery (TZ-7).

        IDLE   в†' trivial, no broker calls needed.
        STOP_CRANE в†' block trading, operator must intervene.
        All other states в†' load broker data, match orders, replay fills,
        verify position, restore FSM.
        """
        self._emit(
            "RECONCILIATION_STARTED",
            "INFO",
            f"Starting reconciliation: {state.cycle_status}",
            {"cycle_status": str(state.cycle_status), "ticker": self._ticker},
        )

        # --- Trivial cases ---

        if state.cycle_status == CycleStatus.STOP_CRANE:
            self._emit(
                "RECONCILIATION_ERROR",
                "WARNING",
                "Bot state is STOP_CRANE. Manual operator resolution required "
                "before trading can resume.",
                {"cycle_status": str(CycleStatus.STOP_CRANE)},
            )
            return state

        if state.cycle_status == CycleStatus.IDLE:
            self._emit(
                "RECONCILIATION_FINISHED",
                "INFO",
                "Reconciliation complete: IDLE state, no open position.",
                {},
            )
            return state

        # --- Steps 2 & 4: load broker data ---

        open_orders, fills = self._load_broker_data(broker, state.last_applied_trade_id)

        # Build O(1) lookup indexes
        by_exchange_id: dict = {o.exchange_order_id: o for o in open_orders}
        by_client_id: dict = {
            o.client_order_id: o
            for o in open_orders
            if o.client_order_id
        }

        # Dust threshold: position_qty <= dust в†' consider position closed.
        # Use MarketInfo.min_qty if available; fall back to strict zero.
        dust_threshold = self._get_dust_threshold(broker)

        # --- Steps 5–7: state-specific reconciliation ---

        try:
            if state.cycle_status == CycleStatus.ENTERING:
                state = self._reconcile_entering(
                    state, by_exchange_id, by_client_id, fills
                )
            elif state.cycle_status == CycleStatus.IN_POSITION:
                state = self._reconcile_in_position(
                    state, by_exchange_id, fills, dust_threshold, broker
                )
            elif state.cycle_status == CycleStatus.CLOSING:
                state = self._reconcile_closing(
                    state, by_exchange_id, fills, dust_threshold
                )
            elif state.cycle_status == CycleStatus.WAITING_FOR_LIQUIDITY:
                state = self._reconcile_waiting(
                    state, by_exchange_id, fills, dust_threshold
                )
            else:
                # Unknown status — shouldn't happen; guard against future additions
                logger.error(
                    "StateRecovery: unknown cycle_status %s — skipping reconciliation",
                    state.cycle_status,
                )

        except Exception as exc:
            # Unexpected error in reconciliation logic itself в†' STOP_CRANE
            logger.exception("StateRecovery: unexpected error during reconciliation")
            state = self._goto_stop_crane(
                state,
                f"Unexpected reconciliation error: {exc}",
            )
            return state

        self._emit(
            "RECONCILIATION_FINISHED",
            "INFO",
            f"Reconciliation complete: {state.cycle_status}",
            {
                "cycle_status":      str(state.cycle_status),
                "position_qty":      str(state.position_qty),
                "open_orders_count": len(open_orders),
                "new_fills_count":   len(fills),
            },
        )
        return state

    # ------------------------------------------------------------------
    # Broker data loading (Steps 2 & 4)
    # ------------------------------------------------------------------

    def _load_broker_data(
        self,
        broker: "IBroker",
        last_applied_trade_id: Optional[str],
    ) -> Tuple[list, list]:
        """
        Load open orders and historical fills from the broker.

        Failures are non-fatal: returns empty lists and emits WARNING.
        Trading will not start until reconciliation decides the state is
        consistent; a WARNING here means we fall back to accepting DB state.
        """
        # Step 2: active orders on exchange
        try:
            open_orders = broker.get_open_orders(ticker=self._ticker)
            logger.debug(
                "StateRecovery: loaded %d open orders for %s",
                len(open_orders), self._ticker,
            )
        except Exception as exc:
            self._emit(
                "RECONCILIATION_WARNING", "WARNING",
                f"get_open_orders failed: {exc}. Using empty order list.",
                {"error": str(exc)},
            )
            open_orders = []

        # Step 4: historical fills since last known trade
        try:
            fills = broker.get_fills(
                ticker=self._ticker,
                since_trade_id=last_applied_trade_id,
            )
            logger.debug(
                "StateRecovery: loaded %d fills for %s (since %s)",
                len(fills), self._ticker, last_applied_trade_id,
            )
        except Exception as exc:
            self._emit(
                "RECONCILIATION_WARNING", "WARNING",
                f"get_fills failed: {exc}. Using empty fills list.",
                {"error": str(exc)},
            )
            fills = []

        return open_orders, fills

    def _get_dust_threshold(self, broker: "IBroker") -> Decimal:
        """
        Return min_qty for the ticker as a proxy for dust_threshold.
        Falls back to Decimal("0") if market info is unavailable.
        """
        try:
            info = broker.get_market_info(self._ticker)
            return info.min_qty
        except Exception:
            return Decimal("0")

    # ------------------------------------------------------------------
    # ENTERING reconciliation
    # ------------------------------------------------------------------

    def _reconcile_entering(
        self,
        state: BotState,
        by_exchange_id: dict,
        by_client_id: dict,
        fills: list,
    ) -> BotState:
        """
        Reconcile ENTERING state.

        Two sub-cases:
        (a) pending_client_order_id only — order was sent but bot crashed
            before receiving exchange_order_id from the response.
        (b) active_entry_order_id — normal: order placed, awaiting fill.

        Outcomes:
          order still on exchange     в†' keep ENTERING (no DB change)
          order filled (in fills)     в†' apply fill, transition IN_POSITION
          order gone, no fill         в†' order cancelled, transition IDLE
          sent but lost (case a only) в†' STOP_CRANE (unknown outcome)
        """
        # Sub-case (a): crash between sending order and receiving its ID
        if state.pending_client_order_id and not state.active_entry_order_id:
            return self._reconcile_pending_send(state, by_client_id, fills)

        # Sub-case (b): have exchange_order_id
        entry_id = state.active_entry_order_id
        if not entry_id:
            # Inconsistent: ENTERING with neither ID
            return self._goto_stop_crane(
                state,
                "ENTERING state has neither active_entry_order_id nor "
                "pending_client_order_id — inconsistent bot_state",
            )

        # Order still pending on exchange?
        if entry_id in by_exchange_id:
            self._emit(
                "RECONCILIATION_FINISHED", "INFO",
                f"ENTERING: entry order {entry_id[:8]}… still pending on exchange",
                {"entry_order_id": entry_id},
            )
            return state  # nothing to change

        # Not in open_orders — check fills (Step 4)
        fill = self._find_fill(fills, exchange_order_id=entry_id)
        if fill is not None:
            return self._apply_entry_fill(state, fill)

        # Not in open_orders and no fill в†' order was cancelled
        self._emit(
            "RECONCILIATION_FINISHED", "INFO",
            f"ENTERING: entry order {entry_id[:8]}… not found on exchange "
            "and not in fills — treating as cancelled → IDLE",
            {"entry_order_id": entry_id},
        )
        return self._state_manager.transition(
            state, CycleStatus.IDLE,
            active_entry_order_id=None,
            pending_client_order_id=None,
        )

    def _reconcile_pending_send(
        self,
        state: BotState,
        by_client_id: dict,
        fills: list,
    ) -> BotState:
        """
        Handle ENTERING where order was sent but exchange_order_id was never
        persisted (crash between create_order() call and DB save of the response).

        pending_client_order_id is known, active_entry_order_id is None.

        Outcomes:
          found in open_orders  в†' recover exchange_order_id, keep ENTERING
          found in fills        в†' order was placed AND filled в†' IN_POSITION
          not found anywhere    в†' STOP_CRANE (truly unknown outcome)
        """
        client_id = state.pending_client_order_id

        # Found on exchange by client_order_id?
        open_order = by_client_id.get(client_id)
        if open_order is not None:
            self._emit(
                "RECONCILIATION_FINISHED", "INFO",
                f"Pending order {client_id[:8]}… found on exchange "
                f"(exchange_id={open_order.exchange_order_id[:8]}…) — recovering",
                {"exchange_order_id": open_order.exchange_order_id},
            )
            return self._state_manager.update(
                state,
                active_entry_order_id=open_order.exchange_order_id,
                pending_client_order_id=None,
            )

        # Found in fills by client_order_id?
        fill = self._find_fill(fills, client_order_id=client_id)
        if fill is not None:
            return self._apply_entry_fill(state, fill)

        # Not found anywhere — outcome of the send is unknown
        return self._goto_stop_crane(
            state,
            f"Entry order with client_order_id={client_id} was sent "
            "but found neither in open orders nor in fills. "
            "Cannot determine if the order landed on the exchange.",
        )

    def _apply_entry_fill(self, state: BotState, fill) -> BotState:
        """
        Apply a confirmed entry fill and transition ENTERING в†' IN_POSITION.

        Calculates weighted average price (handles the rare case where
        position_qty > 0 on ENTERING, e.g. partial fills before restart).
        """
        fill_qty   = fill.filled_qty
        fill_price = fill.avg_price

        old_qty    = state.position_qty
        old_price  = state.position_avg_price or Decimal("0")
        new_qty    = old_qty + fill_qty

        if new_qty > 0:
            new_avg_price = (
                (old_qty * old_price + fill_qty * fill_price) / new_qty
            )
        else:
            new_avg_price = fill_price  # safety fallback

        new_quote_spent = (
            state.quote_spent + fill_qty * fill_price + fill.commission
        )

        self._emit(
            "RECONCILIATION_FINISHED", "INFO",
            f"ENTERING: entry fill found — qty={fill_qty}, "
            f"price={fill_price} в†' IN_POSITION",
            {
                "fill_qty":   str(fill_qty),
                "fill_price": str(fill_price),
                "trade_id":   fill.trade_id,
            },
        )

        return self._state_manager.transition(
            state, CycleStatus.IN_POSITION,
            position_qty=new_qty,
            position_avg_price=new_avg_price,
            quote_spent=new_quote_spent,
            active_entry_order_id=None,
            pending_client_order_id=None,
            last_applied_trade_id=fill.trade_id,
        )

    # ------------------------------------------------------------------
    # IN_POSITION reconciliation
    # ------------------------------------------------------------------

    def _reconcile_in_position(
        self,
        state: BotState,
        by_exchange_id: dict,
        fills: list,
        dust_threshold: Decimal,
        broker: "IBroker",
    ) -> BotState:
        """
        Reconcile IN_POSITION state.

        Steps:
        1. Apply new fills (DCA fills в†' add to position; TP fills в†' subtract).
        2. Check active TP order — missing without a fill means manual cancel.
        3. Check active DCA orders — missing without a fill → remove from list.
        4. If position closed by fills в†' transition CLOSING (BotLoop finalises).
        """
        # Step 5: apply fills that arrived while bot was down
        state, filled_dca_ids = self._apply_position_fills(state, fills)

        # Collect field updates for a single DB write
        updates: dict = {}

        # --- TP order check ---
        tp_id = state.active_tp_order_id
        if tp_id is not None and tp_id not in by_exchange_id:
            tp_fill = self._find_fill(fills, exchange_order_id=tp_id)
            if tp_fill is None:
                # Before treating as manual cancel, try OHLCV playback.
                # PaperBroker: check historical klines for the downtime period.
                ohlcv_hit = self._try_ohlcv_tp_playback(state, broker)
                if ohlcv_hit:
                    # Fill simulated and queued in PaperBroker._fill_queue.
                    # Transition to CLOSING — BotLoop picks it up next tick.
                    updates["active_tp_order_id"] = None
                    updates["active_tp_price"]    = None
                else:
                    # TP vanished with no fill and OHLCV shows no hit:
                    # manual cancel per TZ-7 policy.
                    self._emit(
                        "TP_MANUALLY_CANCELLED", "WARNING",
                        f"TP order {tp_id[:8]}… not found on exchange and "
                        "not in fills (manually cancelled?). "
                        "Position has no TP cover — operator should review.",
                        {"tp_order_id": tp_id},
                    )
                    updates["active_tp_order_id"] = None
            # If tp_fill exists: already applied by _apply_position_fills;
            # TP order consumed — clear it
            else:
                updates["active_tp_order_id"] = None

        # --- DCA orders check ---
        surviving_dca: list = []
        for dca_id in state.active_dca_order_ids:
            if dca_id in filled_dca_ids:
                # Already applied by _apply_position_fills — drop from list
                continue
            if dca_id in by_exchange_id:
                surviving_dca.append(dca_id)  # still active on exchange
            else:
                # Not on exchange, not in fills в†' manual cancel or error
                self._emit(
                    "DCA_MANUALLY_CANCELLED", "WARNING",
                    f"DCA order {dca_id[:8]}… not found on exchange "
                    "and not in fills — removing from active list.",
                    {"dca_order_id": dca_id},
                )
                # Don't add to surviving_dca

        new_dca_tuple = tuple(surviving_dca)
        if new_dca_tuple != state.active_dca_order_ids:
            updates["active_dca_order_ids"] = new_dca_tuple

        # Apply accumulated field updates
        if updates:
            state = self._state_manager.update(state, **updates)

        # Step 6: verify position after fills
        if state.position_qty <= dust_threshold:
            # Position fully closed by fills while bot was down
            # Transition to CLOSING; Close Protocol on next tick в†' IDLE
            self._emit(
                "RECONCILIATION_FINISHED", "INFO",
                f"IN_POSITION: position closed by fills "
                f"(qty={state.position_qty} ≤ dust={dust_threshold}) → CLOSING",
                {"position_qty": str(state.position_qty)},
            )
            return self._state_manager.transition(
                state, CycleStatus.CLOSING,
            )

        return state

    # ------------------------------------------------------------------
    # OHLCV playback (PaperBroker restart recovery)
    # ------------------------------------------------------------------

    def _try_ohlcv_tp_playback(
        self,
        state: BotState,
        broker: "IBroker",
    ) -> bool:
        """
        Check via historical klines if the TP order was filled during downtime.

        Only runs when ALL conditions are met:
          1. Broker has apply_downtime_tp_fill() — i.e. it is PaperBroker.
          2. bot_state.active_tp_price is set (persisted by OrderManager).
          3. A MarketDataProvider with get_klines() was passed to startup().
          4. bot_state.last_order_at is set (when the TP order was placed).
          5. Downtime gap >= OHLCV_MIN_GAP_SEC (default 60 s).

        If any candle HIGH >= active_tp_price within the downtime period:
          - Calls broker.apply_downtime_tp_fill() to queue a simulated fill.
          - Emits OHLCV_TP_SIMULATED event.
          - Returns True → caller skips the "manual cancel" path.

        Returns False in all other cases (not PaperBroker, no klines,
        TP not hit, fetch failed, etc.).
        """
        if not hasattr(broker, "apply_downtime_tp_fill"):
            return False

        tp_price = state.active_tp_price
        if tp_price is None or tp_price <= 0:
            return False

        if self._market is None or not hasattr(self._market, "get_klines"):
            logger.debug(
                "StateRecovery: OHLCV playback skipped — "
                "no market provider with get_klines()"
            )
            return False

        last_order_at = state.last_order_at
        if last_order_at is None:
            return False

        now = datetime.now(timezone.utc)
        if last_order_at.tzinfo is None:
            last_order_at = last_order_at.replace(tzinfo=timezone.utc)

        gap_sec = (now - last_order_at).total_seconds()
        if gap_sec < self.OHLCV_MIN_GAP_SEC:
            logger.debug(
                "StateRecovery: OHLCV playback skipped — "
                "gap %.1fs < OHLCV_MIN_GAP_SEC=%ds",
                gap_sec, self.OHLCV_MIN_GAP_SEC,
            )
            return False

        start_ms = int(last_order_at.timestamp() * 1000)
        end_ms   = int(now.timestamp() * 1000)

        try:
            klines = self._market.get_klines(
                ticker=self._ticker,
                interval_min=1,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception as exc:
            self._emit(
                "RECONCILIATION_WARNING", "WARNING",
                f"OHLCV playback: klines fetch failed: {exc}",
                {"error": str(exc), "ticker": self._ticker},
            )
            return False

        if not klines:
            logger.info(
                "StateRecovery: OHLCV playback — no klines for gap of %.0fs. "
                "TP @ %s treated as not hit.",
                gap_sec, tp_price,
            )
            return False

        tp_hit = any(k.high_price >= tp_price for k in klines)

        if not tp_hit:
            logger.info(
                "StateRecovery: OHLCV playback — TP @ %s NOT hit during "
                "%.0fs downtime (%d candles).",
                tp_price, gap_sec, len(klines),
            )
            return False

        logger.info(
            "StateRecovery: OHLCV playback — TP @ %s WAS HIT during "
            "%.0fs downtime (%d candles). Simulating fill for order %s.",
            tp_price, gap_sec, len(klines),
            (state.active_tp_order_id or "")[:12],
        )

        broker.apply_downtime_tp_fill(
            order_id=state.active_tp_order_id or "",
            tp_price=tp_price,
            ticker=self._ticker,
            qty=state.position_qty,
            bot_id=self._bot_id,
            cycle_id=state.cycle_id or "",
        )

        self._emit(
            "OHLCV_TP_SIMULATED", "INFO",
            f"OHLCV playback: TP @ {tp_price} simulated "
            f"(downtime {gap_sec:.0f}s, {len(klines)} candles)",
            {
                "tp_price":        str(tp_price),
                "downtime_sec":    round(gap_sec),
                "candles_checked": len(klines),
                "order_id":        state.active_tp_order_id,
                "cycle_id":        state.cycle_id,
            },
        )
        return True

        # ------------------------------------------------------------------
    # CLOSING reconciliation
    # ------------------------------------------------------------------

    def _reconcile_closing(
        self,
        state: BotState,
        by_exchange_id: dict,
        fills: list,
        dust_threshold: Decimal,
    ) -> BotState:
        """
        Reconcile CLOSING state.

        Apply remaining fills. If position is fully gone в†' IDLE directly
        (reconciliation can skip Close Protocol since the exchange already
        closed the position). Otherwise keep CLOSING; BotLoop continues.
        """
        state, _ = self._apply_position_fills(state, fills)

        if state.position_qty <= dust_threshold:
            self._emit(
                "RECONCILIATION_FINISHED", "INFO",
                f"CLOSING: position fully closed "
                f"(qty={state.position_qty} ≤ dust={dust_threshold}) → IDLE",
                {"position_qty": str(state.position_qty)},
            )
            # Clear all order IDs — position is gone
            state = self._state_manager.update(
                state,
                active_tp_order_id=None,
                active_dca_order_ids=(),
                active_entry_order_id=None,
                pending_client_order_id=None,
            )
            return self._state_manager.transition(state, CycleStatus.IDLE)

        # Position still open — check TP order
        tp_id = state.active_tp_order_id
        if tp_id is not None and tp_id not in by_exchange_id:
            tp_fill = self._find_fill(fills, exchange_order_id=tp_id)
            if tp_fill is None:
                self._emit(
                    "RECONCILIATION_WARNING", "WARNING",
                    f"CLOSING: TP/close order {tp_id[:8]}… not found on exchange "
                    "and not in fills. Position still open without cover.",
                    {"tp_order_id": tp_id},
                )
                state = self._state_manager.update(state, active_tp_order_id=None)
            else:
                # TP fill applied by _apply_position_fills; clear the ID
                state = self._state_manager.update(state, active_tp_order_id=None)

        return state

    # ------------------------------------------------------------------
    # WAITING_FOR_LIQUIDITY reconciliation
    # ------------------------------------------------------------------

    def _reconcile_waiting(
        self,
        state: BotState,
        by_exchange_id: dict,
        fills: list,
        dust_threshold: Decimal,
    ) -> BotState:
        """
        Reconcile WAITING_FOR_LIQUIDITY state.

        Bot had a position but insufficient funds to place a DCA order.
        After restart: apply fills, clean up stale order IDs, restore
        IN_POSITION (DecisionEngine decides what to do next tick).
        """
        state, filled_dca_ids = self._apply_position_fills(state, fills)

        # If position somehow closed в†' CLOSING
        if state.position_qty <= dust_threshold:
            self._emit(
                "RECONCILIATION_FINISHED", "INFO",
                f"WAITING_FOR_LIQUIDITY: position closed в†' CLOSING",
                {"position_qty": str(state.position_qty)},
            )
            return self._state_manager.transition(state, CycleStatus.CLOSING)

        updates: dict = {}

        # Check TP order if any
        tp_id = state.active_tp_order_id
        if tp_id is not None and tp_id not in by_exchange_id:
            self._emit(
                "RECONCILIATION_WARNING", "WARNING",
                f"WAITING_FOR_LIQUIDITY: TP order {tp_id[:8]}… not found "
                "on exchange — clearing.",
                {"tp_order_id": tp_id},
            )
            updates["active_tp_order_id"] = None

        # Clean stale DCA IDs
        surviving_dca = tuple(
            oid for oid in state.active_dca_order_ids
            if oid not in filled_dca_ids and oid in by_exchange_id
        )
        if surviving_dca != state.active_dca_order_ids:
            updates["active_dca_order_ids"] = surviving_dca

        if updates:
            state = self._state_manager.update(state, **updates)

        # Restore to IN_POSITION — position still exists
        return self._state_manager.transition(state, CycleStatus.IN_POSITION)

    # ------------------------------------------------------------------
    # Fill application helpers
    # ------------------------------------------------------------------

    def _apply_position_fills(
        self,
        state: BotState,
        fills: list,
    ) -> Tuple[BotState, Set[str]]:
        """
        Apply historical fills to position state.

        Matches each fill against the order IDs known in bot_state:
          entry order в†' BUY: increase position_qty, update avg_price
          DCA orders  в†' BUY: same
          TP order    в†' SELL: decrease position_qty, update quote_received

        Fills are sorted by timestamp to apply in chronological order.
        Unrecognised fills (from other cycles or bots) are skipped with WARNING.

        Returns (updated_state, set_of_dca_order_ids_that_were_filled).
        If no fills apply, returns (original_state, empty_set) without DB write.

        Note: virtual_balance is NOT updated here. The virtual_balance
        reflects locked/free USDT at order placement time, which is already
        correct in DB. BalanceReconciler will catch any drift on the next tick.
        """
        if not fills:
            return state, set()

        entry_id  = state.active_entry_order_id
        tp_id     = state.active_tp_order_id
        dca_ids   = set(state.active_dca_order_ids)
        pend_id   = state.pending_client_order_id

        position_qty   = state.position_qty
        avg_price      = state.position_avg_price or Decimal("0")
        quote_spent    = state.quote_spent
        quote_received = state.quote_received
        last_trade_id  = state.last_applied_trade_id
        filled_dca_ids: Set[str] = set()
        changed = False

        for fill in sorted(fills, key=lambda f: f.timestamp):
            oid = fill.exchange_order_id
            cid = fill.client_order_id

            is_entry = (oid == entry_id) or (cid and cid == pend_id)
            is_tp    = (oid == tp_id)
            is_dca   = (oid in dca_ids)

            if is_entry or is_dca:
                # BUY fill — add to position
                fq = fill.filled_qty
                fp = fill.avg_price
                new_qty = position_qty + fq
                if new_qty > 0:
                    avg_price = (
                        (position_qty * avg_price + fq * fp) / new_qty
                    )
                position_qty    = new_qty
                quote_spent    += fq * fp + fill.commission
                last_trade_id   = fill.trade_id
                if is_dca:
                    filled_dca_ids.add(oid)
                changed = True

            elif is_tp:
                # SELL fill — reduce position
                fq = fill.filled_qty
                fp = fill.avg_price
                position_qty    = max(Decimal("0"), position_qty - fq)
                quote_received += fq * fp - fill.commission
                last_trade_id   = fill.trade_id
                changed = True

            else:
                logger.warning(
                    "StateRecovery: fill %s (exchange_order_id=%s) "
                    "does not match any known order in bot_state — skipping.",
                    fill.trade_id, oid,
                )

        if not changed:
            return state, filled_dca_ids

        updates: dict = {
            "position_qty":            position_qty,
            "position_avg_price":      avg_price if position_qty > 0 else None,
            "quote_spent":             quote_spent,
            "quote_received":          quote_received,
            "last_applied_trade_id":   last_trade_id,
        }
        new_state = self._state_manager.update(state, **updates)
        return new_state, filled_dca_ids

    # ------------------------------------------------------------------
    # STOP_CRANE transition
    # ------------------------------------------------------------------

    def _goto_stop_crane(self, state: BotState, reason: str) -> BotState:
        """
        Transition to STOP_CRANE due to unresolvable reconciliation conflict.
        Emits STOP_CRANE_TRIGGERED (CRITICAL) before the FSM transition.
        """
        self._emit(
            "STOP_CRANE_TRIGGERED", "CRITICAL",
            f"Reconciliation STOP_CRANE: {reason}",
            {
                "reason":                  reason,
                "previous_cycle_status":   str(state.cycle_status),
                "ticker":                  self._ticker,
            },
        )
        return self._state_manager.transition(state, CycleStatus.STOP_CRANE)

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_fill(
        fills: list,
        exchange_order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ):
        """
        Find the first fill matching exchange_order_id or client_order_id.
        Returns the fill object or None.
        """
        for f in fills:
            if exchange_order_id and f.exchange_order_id == exchange_order_id:
                return f
            if client_order_id and f.client_order_id == client_order_id:
                return f
        return None

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