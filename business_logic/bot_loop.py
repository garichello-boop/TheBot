"""
BotLoop — главный бесконечный цикл бота (Пункт 7).

Оркестрирует все подсистемы в единый последовательный поток.
10-шаговая последовательность на каждом тике (ТЗ 7):

  1. Собрать TickContext.
  2. Проверить статус бота (STOPPED / CLOSE_ONLY / FORCE_CLOSE).
  3. Reconciliation при расхождении.
  4. Перечитать конфиг (ConfigWatcher, только при новом цикле).
  5. Применить изменения ордеров (fills → FSM-переходы).
  6. DecisionEngine: одно решение.
  7. Выполнить решение.
  8. Commit + Emit (атомарно; emit после commit).
  9. Watchdog: если тик > TICK_MAX_DURATION_SEC → WARNING.
  10. Sleep до следующего тика.

Kill-switch: CRITICAL_ERROR_THRESHOLD подряд → KillSwitchError.
TickSkippedError не инкрементирует счётчик ошибок.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from .balance_reconciler import BalanceReconciler
from .close_protocol import CloseProtocol
from .dca_scheduler import DCAScheduler
from .decision import DecisionEngine
from .errors import (
    CriticalError,
    InsufficientFundsError,
    KillSwitchError,
    RecoverableError,
    StopCraneError,
    TickSkippedError,
)
from .heartbeat import HeartbeatEmitter
from .order_manager import OrderManager
from .partial_fill import PartialFillHandler
from .retry_manager import RetryManager
from .strategy import BaseStrategy
from .tick_context import TickContext
from .types import DecisionAction, FillEvent, OrderType
from bot_state.models import ClosingReason, CycleStatus

if TYPE_CHECKING:
    from broker import IBroker
    from market_data import MarketDataProvider
    from bot_state import BotState, StateManager, StateRepository, RegistryRepository
    from bot_config import ConfigWatcher, CycleSnapshot
    from observability import EventEmitter
    from config import BotLoopSettings

logger = logging.getLogger(__name__)


class BotLoop:
    """
    Главный цикл бота.

    Инициализируется при старте с уже созданными подсистемами.
    run() запускает бесконечный цикл. Для остановки снаружи
    установить bot_configs.status=STOPPED в PostgreSQL.

    Аварийная остановка: CriticalError / KillSwitchError.
    """

    def __init__(
        self,
        *,
        market:           "MarketDataProvider",
        broker:           "IBroker",
        state_manager:    "StateManager",
        state_repo:       "StateRepository",
        registry_repo:    "RegistryRepository",
        config_watcher:   "ConfigWatcher",
        strategy:         BaseStrategy,
        emitter:          "EventEmitter",
        settings:         "BotLoopSettings",
        bot_id:           str,
        user_id:          str,
    ) -> None:
        self._market         = market
        self._broker         = broker
        self._state_manager  = state_manager
        self._state_repo     = state_repo
        self._config_watcher = config_watcher
        self._strategy       = strategy
        self._emitter        = emitter
        self._settings       = settings
        self._bot_id         = bot_id
        self._user_id        = user_id

        # Инициализируем вспомогательные компоненты
        self._order_manager = OrderManager(
            broker=broker,
            state_manager=state_manager,
            emitter=emitter,
            broker_request_timeout_sec=settings.broker_request_timeout_sec,
            broker_retry_delay_sec=settings.broker_retry_delay_sec,
            broker_max_retries=settings.broker_max_retries,
            cancel_max_retries=settings.cancel_max_retries,
        )
        self._dca_scheduler = DCAScheduler(
            order_manager=self._order_manager,
            state_manager=state_manager,
            emitter=emitter,
            dca_mode=settings.dca_mode,
            max_dca_count=settings.max_dca_count,
        )
        self._partial_fill = PartialFillHandler(
            state_manager=state_manager,
            order_manager=self._order_manager,
            emitter=emitter,
            partial_fill_threshold_pct=Decimal(str(settings.partial_fill_threshold_pct)),
            tp_partial_close_threshold_pct=Decimal(
                str(settings.tp_partial_close_threshold_pct)
            ),
        )
        self._close_protocol = CloseProtocol(
            broker=broker,
            order_manager=self._order_manager,
            state_manager=state_manager,
            emitter=emitter,
            dust_threshold=Decimal(str(settings.dust_threshold)),
            cancel_max_retries=settings.cancel_max_retries,
            close_remainder_mode=settings.close_remainder_mode,
            close_remainder_timeout_sec=settings.close_remainder_timeout_sec,
            max_market_close_slippage_pct=Decimal(
                str(settings.max_market_close_slippage_pct)
            ),
            cooldown_sec=settings.cooldown_sec,
            sl_max_market_slippage_pct=Decimal(
                str(settings.sl_max_market_slippage_pct)
            ),
        )
        self._decision_engine = DecisionEngine(
            max_entry_slippage_pct=Decimal(str(settings.max_entry_slippage_pct)),
            partial_fill_threshold_pct=Decimal(str(settings.partial_fill_threshold_pct)),
            tp_partial_close_threshold_pct=Decimal(
                str(settings.tp_partial_close_threshold_pct)
            ),
            entry_order_timeout_sec=settings.entry_order_timeout_sec,
            max_dca_count=settings.max_dca_count,
            dca_mode=settings.dca_mode,
            max_position_days=settings.max_position_days,
            force_close_on_timeout=settings.force_close_on_timeout,
            dust_threshold=Decimal(str(settings.dust_threshold)),
        )
        self._balance_reconciler = BalanceReconciler(
            emitter=emitter,
            check_interval_ticks=10,
            balance_drift_pct=Decimal(str(settings.balance_drift_pct)),
            paper_mode=(broker.get_mode().value == "PAPER"),
        )
        self._heartbeat = HeartbeatEmitter(
            registry_repo=registry_repo,
            emitter=emitter,
            interval_ticks=settings.heartbeat_interval_ticks,
            bot_id=bot_id,
            user_id=user_id,
        )

        # Снапшот параметров текущего цикла (обновляется при смене цикла)
        self._cycle_snapshot: "CycleSnapshot | None" = None

        # Счётчик последовательных ошибок для kill-switch
        self._consecutive_errors: int = 0

        # Флаг работы (для корректной остановки)
        self._running: bool = False

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Запустить бесконечный tick-loop.

        Блокирует текущий поток. Вызывать из main/bot.py после
        завершения startup sequence (StateRecovery.reconcile уже выполнен).
        """
        self._running = True
        tick_number   = 0

        self._emitter.emit(
            event_type="BOT_STARTED",
            level="INFO",
            message=f"Бот запущен: {self._bot_id}",
            payload={
                "bot_id":          self._bot_id,
                "user_id":         self._user_id,
                "strategy":        self._strategy.name(),
                "dca_mode":        self._settings.dca_mode,
                "tick_interval":   self._settings.tick_interval_sec,
            },
        )

        while self._running:
            tick_start = time.monotonic()

            try:
                # --- Шаг 1: собрать TickContext -----------------------
                ctx = TickContext.collect(
                    market=self._market,
                    broker=self._broker,
                    state_repo=self._state_repo,
                    config_watcher=self._config_watcher,
                    tick_number=tick_number,
                )

                # --- Шаги 2-8: основная логика тика ------------------
                self._run_tick(ctx, tick_number)

                # Сбрасываем счётчик ошибок при успешном тике
                self._consecutive_errors = 0

            except KillSwitchError as exc:
                self._handle_kill_switch(exc)
                break

            except CriticalError as exc:
                self._handle_critical(exc)
                break

            except TickSkippedError as exc:
                # Бизнес-пропуск тика — не считается ошибкой
                logger.info("Тик пропущен: %s", exc)

            except RecoverableError as exc:
                self._handle_recoverable(exc)
                if self._consecutive_errors >= self._settings.critical_error_threshold:
                    kill = KillSwitchError(
                        f"Kill-switch: {self._consecutive_errors} ошибок подряд",
                        reason=str(exc),
                        error_count=self._consecutive_errors,
                    )
                    self._handle_kill_switch(kill)
                    break

            except Exception as exc:
                # Неожиданное исключение — как RecoverableError
                logger.exception("Неожиданная ошибка в тике %d", tick_number)
                self._handle_recoverable(
                    RecoverableError(f"Неожиданная ошибка: {exc}")
                )
                if self._consecutive_errors >= self._settings.critical_error_threshold:
                    kill = KillSwitchError(
                        f"Kill-switch после неожиданных ошибок",
                        reason=str(exc),
                        error_count=self._consecutive_errors,
                    )
                    self._handle_kill_switch(kill)
                    break

            # --- Шаг 9: Watchdog ------------------------------------
            tick_duration = time.monotonic() - tick_start
            if tick_duration > self._settings.tick_max_duration_sec:
                self._emitter.emit(
                    event_type="BOT_HEARTBEAT",  # используем как TICK_LATENCY
                    level="WARNING",
                    message=(
                        f"Тик {tick_number} занял {tick_duration:.1f}с "
                        f"> TICK_MAX_DURATION_SEC={self._settings.tick_max_duration_sec}"
                    ),
                    payload={
                        "tick_number":      tick_number,
                        "duration_sec":     round(tick_duration, 2),
                        "max_allowed_sec":  self._settings.tick_max_duration_sec,
                    },
                )

            # --- Шаг 10: Sleep --------------------------------------
            elapsed   = time.monotonic() - tick_start
            sleep_sec = max(0.0, self._settings.tick_interval_sec - elapsed)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

            tick_number += 1

        # Выход из цикла — помечаем в registry
        self._heartbeat.mark_stopped()
        self._emitter.emit(
            event_type="BOT_STOPPED",
            level="INFO",
            message=f"Бот остановлен: {self._bot_id}",
            payload={"bot_id": self._bot_id, "ticks_total": tick_number},
        )

    # ------------------------------------------------------------------
    # Один тик (шаги 2-8)
    # ------------------------------------------------------------------

    def _run_tick(self, ctx: TickContext, tick_number: int) -> None:
        """Обработка одного тика: шаги 2-8."""

        # Шаг 2: статус бота -----------------------------------------
        bot_status = ctx.bot_status

        if bot_status == "STOPPED":
            logger.info("bot_configs.status=STOPPED — останавливаемся")
            self._running = False
            return

        # FORCE_CLOSE: принудительное закрытие позиции по рынку
        if bot_status == "FORCE_CLOSE" and ctx.has_open_position:
            self._execute_force_close(ctx, ctx.bot_state)
            return

        # Шаг 3: reconciliation при расхождении ----------------------
        # Если cycle_status=STOP_CRANE — торговля уже заблокирована,
        # DecisionEngine вернёт WAIT.
        # Развёрнутый reconciliation происходит в StateRecovery при старте.
        if ctx.cycle_status == "STOP_CRANE":
            logger.debug("STOP_CRANE активен — ожидаем ручного резолва")
            self._heartbeat.maybe_emit(ctx)
            return

        # Шаг 4: конфиг (ConfigWatcher, только при смене цикла) ------
        # config_watcher.get_config() уже вызван в TickContext.collect().
        # CycleSnapshot обновляется при переходе IDLE → ENTERING.

        # Шаг 5: применить события ордеров --------------------------
        state = self._apply_order_events(ctx)

        # Шаг 6: сигнал стратегии -----------------------------------
        signal = self._strategy.evaluate(
            ctx.price_data,
            self._cycle_snapshot or _empty_snapshot(ctx.bot_config.strategy_params),
            state.position_qty,
        )

        # DecisionEngine принимает решение
        decision = self._decision_engine.decide(ctx, state, signal)
        logger.debug(
            "Tick %d: status=%s, decision=%s (%s)",
            tick_number, state.cycle_status,
            decision.action.value, decision.reason,
        )

        # Шаг 7: выполнить решение ----------------------------------
        state = self._execute_decision(ctx, state, signal, decision)

        # Шаг 8: commit + emit (уже выполнено внутри execute методов) -

        # Периодическая сверка баланса
        self._balance_reconciler.maybe_check(ctx)

        # Heartbeat
        self._heartbeat.maybe_emit(ctx)

    # ------------------------------------------------------------------
    # Шаг 5: применение событий ордеров
    # ------------------------------------------------------------------

    def _apply_order_events(self, ctx: TickContext) -> "BotState":
        """
        Применить события fills/cancels к bot_state.

        Вызывается ДО DecisionEngine — DecisionEngine видит уже
        обновлённое состояние (post-fill).
        """
        state = ctx.bot_state

        for fill in ctx.order_events:
            state = self._apply_single_fill(ctx, state, fill)

        return state

    def _apply_single_fill(
        self,
        ctx: TickContext,
        state: "BotState",
        fill: FillEvent,
    ) -> "BotState":
        """Применить один fill к состоянию."""
        order_type = fill.order_type

        if order_type == OrderType.ENTRY:
            state, position_opened = self._partial_fill.handle_entry_fill(state, fill)
            if position_opened:
                # Открываем позицию: FSM ENTERING → IN_POSITION
                cycle_id = state.cycle_id or _new_cycle_id()
                state = self._state_manager.commit(
                    state,
                    replace(
                        state,
                        cycle_status="IN_POSITION",
                        cycle_id=cycle_id,
                    ),
                )
                self._emitter.emit(
                    event_type="CYCLE_STARTED",
                    level="INFO",
                    message=f"Цикл открыт: entry={fill.avg_fill_price}",
                    payload={
                        "cycle_id":    cycle_id,
                        "entry_price": str(fill.avg_fill_price),
                        "qty":         str(fill.filled_qty),
                    },
                )
            else:
                # Не открылась позиция — возврат в IDLE
                state = self._state_manager.commit(
                    state,
                    replace(state, cycle_status="IDLE", cycle_id=None),
                )

        elif order_type == OrderType.DCA:
            state = self._partial_fill.handle_dca_fill(state, fill)

        elif order_type == OrderType.TP:
            state, should_close = self._partial_fill.handle_tp_fill(state, fill)
            if should_close:
                # TP >= порога → запускаем Close Protocol
                state = self._state_manager.commit(
                    state,
                    replace(state, cycle_status="CLOSING",
                            closing_reason=ClosingReason.TP),
                )
                self._emitter.emit(
                    event_type="CYCLE_STATUS_CHANGED",
                    level="INFO",
                    message="IN_POSITION → CLOSING",
                    payload={
                        "from":           "IN_POSITION",
                        "to":             "CLOSING",
                        "reason":         "tp_filled_above_threshold",
                        "closing_reason": "TP",
                    },
                )

        return state

    # ------------------------------------------------------------------
    # Шаг 7: выполнение решения
    # ------------------------------------------------------------------

    def _execute_decision(
        self,
        ctx: TickContext,
        state: "BotState",
        signal,
        decision,
    ) -> "BotState":
        """Выполнить решение DecisionEngine."""
        action = decision.action

        if action == DecisionAction.WAIT:
            return state

        elif action == DecisionAction.ENTER:
            return self._execute_enter(ctx, state, decision)

        elif action == DecisionAction.PLACE_TP:
            return self._execute_place_tp(ctx, state, signal)

        elif action == DecisionAction.REPLACE_TP:
            return self._execute_replace_tp(ctx, state, signal)

        elif action == DecisionAction.PLACE_DCA:
            return self._execute_place_dca(ctx, state, decision, signal)

        elif action == DecisionAction.PLACE_EAGER_DCA:
            return self._execute_place_eager_dca(ctx, state)

        elif action == DecisionAction.CANCEL_ENTRY:
            return self._execute_cancel_entry(ctx, state, decision)

        elif action == DecisionAction.CLOSE_PROTOCOL:
            return self._execute_close_protocol(ctx, state)

        elif action == DecisionAction.FORCE_CLOSE:
            return self._execute_force_close(ctx, state)

        elif action == DecisionAction.SET_CLOSE_ONLY:
            return self._execute_set_close_only(ctx, state, decision.reason)

        elif action == DecisionAction.RETRY_LIQUIDITY:
            return self._execute_retry_liquidity(ctx, state, decision, signal)

        elif action == DecisionAction.INITIATE_SL_CLOSE:
            return self._execute_sl_close(ctx, state, decision)

        elif action == DecisionAction.STOP_CRANE:
            if decision.stop_crane_error:
                raise decision.stop_crane_error
            raise StopCraneError(
                f"STOP_CRANE: {decision.reason}",
                invariant="unknown",
                expected={},
                actually_found=None,
                db_state={},
            )

        logger.error("Неизвестный action: %s", action)
        return state

    # ------------------------------------------------------------------
    # Конкретные execute-методы
    # ------------------------------------------------------------------

    def _execute_enter(
        self, ctx: TickContext, state: "BotState", decision
    ) -> "BotState":
        """ENTER: выставить ордер на вход, обновить snapshot."""
        cycle_id = _new_cycle_id()

        # Обновляем CycleSnapshot для нового цикла (шаг 4 ТЗ)
        self._cycle_snapshot = self._config_watcher.create_snapshot()

        # FSM: IDLE → ENTERING
        state = self._state_manager.commit(
            state,
            replace(
                state,
                cycle_status="ENTERING",
                cycle_id=cycle_id,
            ),
        )

        try:
            _, state = self._order_manager.place_entry_order(
                state,
                qty=decision.entry_qty,
                price=decision.entry_price,
                ticker=ctx.ticker,
                cycle_id=cycle_id,
            )
        except InsufficientFundsError as exc:
            # Возврат в IDLE — нехватка средств при входе нетипична,
            # но обрабатываем
            state = self._state_manager.commit(
                state,
                replace(state, cycle_status="IDLE", cycle_id=None),
            )
            self._emitter.emit(
                event_type="INSUFFICIENT_FUNDS",
                level="WARNING",
                message=f"Нехватка средств при входе: {exc}",
                payload={
                    "required":  exc.required,
                    "available": exc.available,
                    "action":    "ENTER",
                },
            )

        # Если режим EAGER и есть DCA-уровни — выставляем сразу
        if (
            self._settings.dca_mode == "EAGER"
            and decision.dca_levels
            and self._cycle_snapshot
        ):
            # Передаём уровни через snapshot для EAGER
            state = self._dca_scheduler.place_eager_dca_orders(
                state,
                snapshot=self._cycle_snapshot,
                ticker=ctx.ticker,
            )

        return state

    def _execute_place_tp(
        self, ctx: TickContext, state: "BotState", signal
    ) -> "BotState":
        """PLACE_TP: выставить TP после исполнения entry.

        tp_price может быть None если DecisionEngine передал None
        (Strategy пересчитает на основе avg_price). В этом случае
        запрашиваем TP-цену у стратегии напрямую — аналогично
        _execute_replace_tp.
        """
        tp_price = signal.tp_price
        if tp_price is None:
            recalc = self._strategy.evaluate(
                ctx.price_data,
                self._cycle_snapshot or _empty_snapshot(ctx.bot_config.strategy_params),
                state.position_qty,
            )
            tp_price = recalc.tp_price

        if tp_price is None:
            logger.warning("PLACE_TP: tp_price=None даже после пересчёта стратегии — пропускаем")
            return state

        _, state = self._order_manager.place_tp_order(
            state,
            qty=state.position_qty,
            price=tp_price,
            ticker=ctx.ticker,
            cycle_id=state.cycle_id or "",
        )
        return state

    def _execute_replace_tp(
        self, ctx: TickContext, state: "BotState", signal
    ) -> "BotState":
        """REPLACE_TP: пересоздать TP после DCA."""
        if signal.tp_price is None:
            # Пересчитать TP через стратегию
            new_signal = self._strategy.evaluate(
                ctx.price_data,
                self._cycle_snapshot or _empty_snapshot(ctx.bot_config.strategy_params),
                state.position_qty,
            )
            tp_price = new_signal.tp_price
        else:
            tp_price = signal.tp_price

        if tp_price is None:
            logger.warning("REPLACE_TP: tp_price=None — пропускаем")
            return state

        state = self._dca_scheduler.recreate_tp_after_dca(
            state,
            new_avg_price=state.position_avg_price,
            new_position_qty=state.position_qty,
            tp_price=tp_price,
            ticker=ctx.ticker,
        )
        return state

    def _execute_place_dca(
        self, ctx: TickContext, state: "BotState", decision, signal
    ) -> "BotState":
        """PLACE_DCA: LAZY-режим, один уровень."""
        try:
            _, state = self._order_manager.place_dca_order(
                state,
                qty=decision.dca_qty,
                price=decision.dca_price,
                ticker=ctx.ticker,
                cycle_id=state.cycle_id or "",
            )
        except InsufficientFundsError as exc:
            state = self._state_manager.commit(
                state,
                replace(state, cycle_status="WAITING_FOR_LIQUIDITY"),
            )
            retry_in = RetryManager.get_delay_for_attempt(0)
            self._emitter.emit(
                event_type="INSUFFICIENT_FUNDS",
                level="WARNING",
                message=RetryManager.format_next_attempt_message(0),
                payload={
                    "required":    exc.required,
                    "available":   exc.available,
                    "retry_in_sec": retry_in,
                    "action":      "DCA",
                },
            )
        return state

    def _execute_place_eager_dca(
        self, ctx: TickContext, state: "BotState"
    ) -> "BotState":
        """PLACE_EAGER_DCA: все уровни сразу."""
        if self._cycle_snapshot is None:
            return state
        return self._dca_scheduler.place_eager_dca_orders(
            state,
            snapshot=self._cycle_snapshot,
            ticker=ctx.ticker,
        )

    def _execute_cancel_entry(
        self, ctx: TickContext, state: "BotState", decision
    ) -> "BotState":
        """CANCEL_ENTRY: отменить ордер на вход, вернуться в IDLE."""
        order_id = decision.cancel_order_id or state.active_entry_order_id

        if order_id:
            self._order_manager.cancel_order(order_id, order_role="ENTRY")

        state = self._state_manager.commit(
            state,
            replace(
                state,
                cycle_status="IDLE",
                cycle_id=None,
                active_entry_order_id=None,
                pending_client_order_id=None,
            ),
        )
        self._cycle_snapshot = None

        self._emitter.emit(
            event_type="ORDER_CANCELLED",
            level="WARNING",
            message=f"Entry ордер отменён (таймаут): {order_id}",
            payload={
                "order_id":     order_id,
                "initiated_by": "bot",
                "reason":       decision.reason,
            },
        )
        return state

    def _execute_close_protocol(
        self, ctx: TickContext, state: "BotState"
    ) -> "BotState":
        """CLOSE_PROTOCOL: запустить / продолжить 13-шаговый Close Protocol."""
        # Переводим в CLOSING если ещё не там
        if state.cycle_status != "CLOSING":
            state = self._state_manager.commit(
                state,
                replace(state, cycle_status="CLOSING"),
            )

        state, status = self._close_protocol.run(ctx, state)

        if status == "COMPLETE":
            self._cycle_snapshot = None
            logger.info("Close Protocol завершён — FSM в IDLE")

        return state

    def _execute_force_close(
        self, ctx: TickContext, state: "BotState"
    ) -> "BotState":
        """FORCE_CLOSE: закрыть позицию по рынку."""
        logger.warning("FORCE_CLOSE: закрываем позицию по рынку")

        self._emitter.emit(
            event_type="ORDER_CREATED",
            level="WARNING",
            message=f"FORCE_CLOSE: {state.position_qty} {ctx.ticker} по рынку",
            payload={
                "qty":    str(state.position_qty),
                "reason": "force_close_command",
                "bid":    str(ctx.price_data.bid),
            },
        )

        # Устанавливаем closing_reason перед запуском Close Protocol
        if state.cycle_status != "CLOSING":
            state = self._state_manager.commit(
                state,
                replace(state, cycle_status="CLOSING",
                        closing_reason=ClosingReason.FORCE_CLOSE),
            )

        # Используем CloseProtocol с mode=MARKET
        original_mode = self._close_protocol._close_remainder_mode  # noqa: SLF001
        self._close_protocol._close_remainder_mode = "MARKET"  # noqa: SLF001
        try:
            state, _ = self._close_protocol.run(ctx, state)
        finally:
            self._close_protocol._close_remainder_mode = original_mode  # noqa: SLF001

        self._cycle_snapshot = None
        return state

    def _execute_sl_close(
        self, ctx: TickContext, state: "BotState", decision
    ) -> "BotState":
        """
        INITIATE_SL_CLOSE: стоп-лосс сработал.

        Последовательность (ТЗ-7 StopLoss §6):
          1. Emit SL_TRIGGERED с диагностическим payload.
          2. FSM IN_POSITION → CLOSING с closing_reason=SL.
          3. Close Protocol — шаг 9 видит closing_reason=SL и форсирует
             MARKET с проверкой SL_MAX_MARKET_SLIPPAGE_PCT.
        """
        params     = ctx.bot_config.strategy_params
        sl_pct     = params.get("SL_PCT", 0)
        avg_price  = state.position_avg_price or Decimal(0)
        current_bid = ctx.price_data.bid
        sl_price   = avg_price * (1 - Decimal(str(sl_pct)) / 100) if avg_price > 0 else Decimal(0)
        loss_pct   = (avg_price - current_bid) / avg_price * 100 if avg_price > 0 else Decimal(0)

        self._emitter.emit(
            event_type="SL_TRIGGERED",
            level="WARNING",
            message=(
                f"Stop-loss сработал: bid={current_bid} <= sl_level={sl_price:.4f} "
                f"(loss={loss_pct:.2f}%)"
            ),
            payload={
                "sl_price":   str(sl_price),
                "current_bid": str(current_bid),
                "avg_price":  str(avg_price),
                "sl_pct":     sl_pct,
                "loss_pct":   str(loss_pct.quantize(Decimal("0.01"))),
                "cycle_id":   state.cycle_id,
            },
        )

        # FSM → CLOSING с причиной SL
        state = self._state_manager.transition(
            state,
            CycleStatus.CLOSING,
            closing_reason=ClosingReason.SL,
        )

        # Close Protocol: шаг 9 обнаружит closing_reason=SL и форсирует MARKET
        state, status = self._close_protocol.run(ctx, state)

        if status == "COMPLETE":
            self._cycle_snapshot = None
            logger.info("SL Close Protocol завершён — FSM в IDLE")

        return state

    def _execute_set_close_only(
        self, ctx: TickContext, state: "BotState", reason: str
    ) -> "BotState":
        """SET_CLOSE_ONLY: установить bot_configs.status=CLOSE_ONLY."""
        logger.warning("SET_CLOSE_ONLY: %s", reason)

        # Устанавливаем через ConfigRepository (не StateManager — это bot_configs)
        try:
            self._config_watcher.set_close_only(ctx.user_id, ctx.bot_id)
        except Exception as exc:
            logger.error("Не удалось установить CLOSE_ONLY через ConfigWatcher: %s", exc)

        self._emitter.emit(
            event_type="BOT_STOPPING",
            level="WARNING",
            message=f"Переход в CLOSE_ONLY: {reason}",
            payload={
                "reason":   reason,
                "bot_id":   ctx.bot_id,
                "ticker":   ctx.ticker,
            },
        )
        return state

    def _execute_retry_liquidity(
        self, ctx: TickContext, state: "BotState", decision, signal
    ) -> "BotState":
        """RETRY_LIQUIDITY: повторить ордер после WAITING_FOR_LIQUIDITY."""
        logger.info("RETRY_LIQUIDITY: попытка повторить ордер")

        try:
            _, state = self._order_manager.place_dca_order(
                state,
                qty=decision.dca_qty or signal.target_qty or Decimal(0),
                price=decision.dca_price,
                ticker=ctx.ticker,
                cycle_id=state.cycle_id or "",
            )
            # Успех — возвращаемся в IN_POSITION
            state = self._state_manager.commit(
                state,
                replace(state, cycle_status="IN_POSITION"),
            )

        except InsufficientFundsError as exc:
            attempt = state.dca_count  # упрощённо
            retry_in = RetryManager.get_delay_for_attempt(attempt)
            self._emitter.emit(
                event_type="INSUFFICIENT_FUNDS",
                level="WARNING",
                message=RetryManager.format_next_attempt_message(attempt),
                payload={
                    "required":     exc.required,
                    "available":    exc.available,
                    "retry_in_sec": retry_in,
                    "attempt":      attempt + 1,
                },
            )

        return state

    # ------------------------------------------------------------------
    # Обработка ошибок
    # ------------------------------------------------------------------

    def _handle_critical(self, exc: CriticalError) -> None:
        """Критическая ошибка: бот останавливается."""
        logger.critical("CriticalError: %s", exc)
        self._heartbeat.mark_error(str(exc))
        self._emitter.emit(
            event_type="BOT_CRASHED",
            level="CRITICAL",
            message=f"Бот аварийно остановлен: {exc}",
            payload={
                "error_type": type(exc).__name__,
                "message":    str(exc),
            },
        )

    def _handle_recoverable(self, exc: RecoverableError) -> None:
        """Восстанавливаемая ошибка: тик пропускается, счётчик растёт."""
        self._consecutive_errors += 1
        logger.error(
            "RecoverableError (подряд: %d/%d): %s",
            self._consecutive_errors,
            self._settings.critical_error_threshold,
            exc,
        )
        self._emitter.emit(
            event_type="BOT_HEARTBEAT",  # нет отдельного TICK_FAILED в реестре MVP
            level="ERROR",
            message=f"Ошибка тика: {exc}",
            payload={
                "error_type":          type(exc).__name__,
                "message":             str(exc),
                "consecutive_errors":  self._consecutive_errors,
            },
        )

    def _handle_kill_switch(self, exc: KillSwitchError) -> None:
        """Kill-switch: паттерн ошибок."""
        logger.critical("KillSwitchError: %s", exc)
        self._heartbeat.mark_error(f"kill_switch: {exc.reason}")
        self._emitter.emit(
            event_type="KILL_SWITCH_TRIGGERED",
            level="CRITICAL",
            message=f"Kill-switch сработал: {exc.reason}",
            payload=exc.to_payload(),
        )
        # Выставить STOP_CRANE в bot_state если возможно
        try:
            state = self._state_repo.load(self._user_id, self._bot_id)
            if state:
                self._state_manager.commit(
                    state,
                    replace(state, cycle_status="STOP_CRANE"),
                )
        except Exception as inner:
            logger.error("Не удалось установить STOP_CRANE после kill-switch: %s", inner)

    def _handle_stop_crane(self, exc: StopCraneError, state: "BotState") -> None:
        """STOP_CRANE: неизвестный исход ордера."""
        logger.critical("STOP_CRANE: %s", exc)
        self._emitter.emit(
            event_type="STOP_CRANE_TRIGGERED",
            level="CRITICAL",
            message=str(exc),
            payload=exc.to_payload(),
        )
        try:
            self._state_manager.commit(
                state,
                replace(state, cycle_status="STOP_CRANE"),
            )
        except Exception as inner:
            logger.error("Не удалось сохранить STOP_CRANE в БД: %s", inner)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _new_cycle_id() -> str:
    """Генерировать уникальный ID нового цикла."""
    return str(uuid.uuid4())


def _empty_snapshot(strategy_params: dict):
    """Заглушка CycleSnapshot когда цикл ещё не начался (IDLE)."""
    from datetime import datetime, timezone  # noqa: PLC0415
    from bot_config import CycleSnapshot     # noqa: PLC0415
    return CycleSnapshot(
        strategy_params=strategy_params,
        config_version=0,
        started_at=datetime.now(timezone.utc),
    )
