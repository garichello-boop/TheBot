"""
DecisionEngine — принятие решений на основе TickContext и FSM.

Правила:
  - Получает TickContext (с уже применёнными событиями fills) и StrategySignal.
  - Возвращает ровно одно Decision за тик.
  - Не исполняет ордеров сам — только решает что делать.
  - Не делает запросов к бирже или БД.
  - Все данные — только из TickContext.

Последовательность принятия решения по FSM (ТЗ 7):

  SL-проверка (до FSM):
    → SL_ENABLED=true и bid <= avg_price*(1-SL_PCT/100) и IN_POSITION/WFL:
      INITIATE_SL_CLOSE.

  IDLE:
    → CLOSE_ONLY/STOPPED/FORCE_CLOSE конфига: WAIT.
    → should_enter + цена не ушла за MAX_ENTRY_SLIPPAGE_PCT: ENTER.
    → иначе: WAIT.

  ENTERING:
    → entry ордер исполнен (>= PARTIAL_FILL_THRESHOLD_PCT): PLACE_TP.
    → entry ордер CANCELLED (ручная отмена): SET_CLOSE_ONLY + алерт.
    → entry ордер UNKNOWN (исчез без CANCELLED): STOP_CRANE.
    → timeout (now - last_order_at > ENTRY_ORDER_TIMEOUT_SEC): CANCEL_ENTRY.
    → иначе: WAIT.

  IN_POSITION:
    → TP исполнен >= TP_PARTIAL_CLOSE_THRESHOLD_PCT: CLOSE_PROTOCOL.
    → TP CANCELLED (ручная отмена): SET_CLOSE_ONLY.
    → TP UNKNOWN: STOP_CRANE.
    → DCA-сигнал (delta_qty > 0) + dca_count < MAX_DCA_COUNT: PLACE_DCA.
    → EAGER DCA-режим и ордеров ещё нет: PLACE_EAGER_DCA.
    → позиция старше MAX_POSITION_DAYS: алерт (и FORCE_CLOSE если включено).
    → иначе: WAIT.

  CLOSING:
    → position_qty <= dust_threshold: финализировать цикл (через CloseProtocol).
    → иначе: WAIT (CloseProtocol продолжает работу в bot_loop).

  WAITING_FOR_LIQUIDITY:
    → пауза закончилась: RETRY_LIQUIDITY.
    → иначе: WAIT.

  STOP_CRANE:
    → WAIT (торговля заблокирована до ручного резолва).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from .errors import TickSkippedError
from .types import Decision, DecisionAction, OrderStatus
from .strategy import StrategySignal

if TYPE_CHECKING:
    from .tick_context import TickContext
    from bot_state import BotState

logger = logging.getLogger(__name__)

# Sentinel для отсутствующего last_order_at
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class DecisionEngine:
    """
    Принимает решение для текущего тика на основе FSM-состояния.

    Инициализируется один раз при старте бота.
    Параметры из AppSettings.BotLoopSettings передаются в конструктор.
    """

    def __init__(
        self,
        *,
        max_entry_slippage_pct: Decimal,
        partial_fill_threshold_pct: Decimal,
        tp_partial_close_threshold_pct: Decimal,
        entry_order_timeout_sec: int,
        max_dca_count: int,
        dca_mode: str,                       # "EAGER" | "LAZY"
        max_position_days: int | None,
        force_close_on_timeout: bool,
        dust_threshold: Decimal,
    ) -> None:
        self._max_entry_slippage_pct         = max_entry_slippage_pct
        self._partial_fill_threshold_pct     = partial_fill_threshold_pct
        self._tp_partial_close_threshold_pct = tp_partial_close_threshold_pct
        self._entry_order_timeout_sec        = entry_order_timeout_sec
        self._max_dca_count                  = max_dca_count
        self._dca_mode                       = dca_mode
        self._max_position_days              = max_position_days
        self._force_close_on_timeout         = force_close_on_timeout
        self._dust_threshold                 = dust_threshold

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def decide(
        self,
        ctx: "TickContext",
        state: "BotState",
        signal: StrategySignal,
    ) -> Decision:
        """
        Принять решение для текущего тика.

        Args:
          ctx    — снапшот тика (price, orders, config и т.д.).
          state  — актуальное состояние после применения order_events.
          signal — сигнал стратегии (target_qty, tp_price и т.д.).

        Returns:
          Decision с описанием действия.

        Raises:
          TickSkippedError — если тик пропускается по бизнес-причине
                             (не инкрементирует счётчик ошибок).

        SL-проверка выполняется ПЕРВОЙ — до FSM-диспатча.
        Если SL срабатывает, StrategySignal игнорируется и возвращается
        INITIATE_SL_CLOSE. Это соответствует ТЗ-7 StopLoss §6:
        «SL-проверка встаёт между шагом 5 и шагом 6».
        """
        # ── SL-проверка (до FSM-диспатча) ──────────────────────────────
        sl_decision = self._check_stop_loss(ctx, state)
        if sl_decision is not None:
            return sl_decision

        # ── FSM-диспатч ────────────────────────────────────────────────
        status = state.cycle_status

        if status == "IDLE":
            return self._decide_idle(ctx, state, signal)
        elif status == "ENTERING":
            return self._decide_entering(ctx, state)
        elif status == "IN_POSITION":
            return self._decide_in_position(ctx, state, signal)
        elif status == "CLOSING":
            return self._decide_closing(ctx, state)
        elif status == "WAITING_FOR_LIQUIDITY":
            return self._decide_waiting_for_liquidity(state)
        elif status == "STOP_CRANE":
            return Decision(
                action=DecisionAction.WAIT,
                reason="stop_crane_active: ожидается ручной резолв оператора",
            )
        else:
            logger.error("Неизвестный cycle_status: %s", status)
            return Decision(
                action=DecisionAction.WAIT,
                reason=f"unknown_cycle_status:{status}",
            )

    # ------------------------------------------------------------------
    # Stop-Loss
    # ------------------------------------------------------------------

    def _check_stop_loss(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> "Decision | None":
        """
        Проверить условие стоп-лосса.

        Читает SL_ENABLED и SL_PCT из strategy_params текущего снапшота.
        Срабатывает только в IN_POSITION и WAITING_FOR_LIQUIDITY.
        Сравнивает bid (цену продажи) с уровнем SL.

        Returns:
          Decision(INITIATE_SL_CLOSE) если SL сработал.
          None если SL выключен, параметры невалидны или условие не выполнено.
        """
        params = ctx.bot_config.strategy_params

        if not params.get("SL_ENABLED", False):
            return None

        sl_pct = params.get("SL_PCT")
        if sl_pct is None or sl_pct <= 0:
            return None

        cycle_status = str(state.cycle_status)
        if cycle_status not in ("IN_POSITION", "WAITING_FOR_LIQUIDITY"):
            return None

        avg_price = state.position_avg_price
        if avg_price is None or avg_price <= 0:
            # Некорректное состояние — молча пропускаем, не блокируем торговлю
            return None

        sl_price = avg_price * (1 - Decimal(str(sl_pct)) / 100)
        current_bid = ctx.price_data.bid

        if current_bid > sl_price:
            return None

        loss_pct = (avg_price - current_bid) / avg_price * 100

        logger.warning(
            "SL сработал: bid=%s <= sl_level=%s (avg=%s, sl_pct=%s, loss=%.2f%%)",
            current_bid, sl_price, avg_price, sl_pct, float(loss_pct),
        )

        return Decision(
            action=DecisionAction.INITIATE_SL_CLOSE,
            reason=(
                f"sl_triggered: bid={current_bid} <= sl_price={sl_price:.4f} "
                f"(avg={avg_price}, sl_pct={sl_pct}, loss={loss_pct:.2f}%)"
            ),
        )

    # ------------------------------------------------------------------
    # IDLE
    # ------------------------------------------------------------------

    def _decide_idle(
        self,
        ctx: "TickContext",
        state: "BotState",
        signal: StrategySignal,
    ) -> Decision:
        bot_status = ctx.bot_status

        # Не открывать новый цикл при CLOSE_ONLY / STOPPED / FORCE_CLOSE
        if bot_status in ("CLOSE_ONLY", "STOPPED", "FORCE_CLOSE"):
            return Decision(
                action=DecisionAction.WAIT,
                reason=f"bot_status={bot_status}: новые циклы не открываются",
            )

        if not signal.should_enter or signal.target_qty is None:
            return Decision(action=DecisionAction.WAIT, reason=signal.reason)

        # Проверка проскальзывания: если цена ушла слишком далеко от уровня
        # сигнала, вход пропускается.
        if signal.target_avg_price is not None:
            slippage = self._calc_slippage(
                ctx.price_data.ask, signal.target_avg_price
            )
            if slippage > self._max_entry_slippage_pct:
                raise TickSkippedError(
                    f"Вход пропущен: проскальзывание {slippage:.2f}% > "
                    f"{self._max_entry_slippage_pct}% MAX_ENTRY_SLIPPAGE_PCT. "
                    f"Сигнал: {signal.reason}"
                )

        return Decision(
            action=DecisionAction.ENTER,
            reason=signal.reason,
            entry_qty=signal.target_qty,
            entry_price=signal.target_avg_price,  # None → MARKET в OrderManager
            tp_price=signal.tp_price,
            dca_levels=signal.dca_levels,
        )

    # ------------------------------------------------------------------
    # ENTERING
    # ------------------------------------------------------------------

    def _decide_entering(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> Decision:
        entry_fills = ctx.fills_for_entry

        for fill in entry_fills:
            if fill.is_fully_filled or (
                fill.is_partial
                and fill.fill_pct >= self._partial_fill_threshold_pct
            ):
                # Вход исполнен (полностью или >= порога)
                return Decision(
                    action=DecisionAction.PLACE_TP,
                    reason=(
                        f"entry_filled: fill_pct={fill.fill_pct:.1f}%, "
                        f"qty={fill.filled_qty}"
                    ),
                    entry_qty=fill.filled_qty,
                    tp_price=None,  # Strategy пересчитает в bot_loop на основе avg_price
                )

            if fill.is_cancelled:
                # Ручная отмена ордера на вход — переходим в CLOSE_ONLY
                return Decision(
                    action=DecisionAction.SET_CLOSE_ONLY,
                    reason=(
                        f"entry_manually_cancelled: "
                        f"order_id={fill.exchange_order_id}"
                    ),
                )

            if fill.status == OrderStatus.UNKNOWN:
                # Ордер исчез без CANCELLED — аномалия
                from .errors import StopCraneError
                err = StopCraneError(
                    "Ордер на вход исчез с биржи без статуса CANCELLED",
                    invariant="active_entry_order_exists_on_exchange",
                    expected={
                        "order_id": state.active_entry_order_id,
                        "client_order_id": state.pending_client_order_id,
                        "type": "ENTRY",
                    },
                    actually_found=None,
                    db_state=_state_snapshot(state),
                )
                return Decision(
                    action=DecisionAction.STOP_CRANE,
                    reason="entry_order_unknown_disappearance",
                    stop_crane_error=err,
                )

        # Проверка таймаута ожидания исполнения
        if state.last_order_at is not None:
            age_sec = (
                datetime.now(timezone.utc) - state.last_order_at
            ).total_seconds()
            if age_sec > self._entry_order_timeout_sec:
                return Decision(
                    action=DecisionAction.CANCEL_ENTRY,
                    reason=(
                        f"entry_timeout: ордер висит "
                        f"{age_sec:.0f}с > {self._entry_order_timeout_sec}с"
                    ),
                    cancel_order_id=state.active_entry_order_id,
                )

        return Decision(action=DecisionAction.WAIT, reason="entering: ждём исполнения")

    # ------------------------------------------------------------------
    # IN_POSITION
    # ------------------------------------------------------------------

    def _decide_in_position(
        self,
        ctx: "TickContext",
        state: "BotState",
        signal: StrategySignal,
    ) -> Decision:
        # --- Нет активного TP — нужно выставить -------------------------
        # Срабатывает при первом тике после входа в позицию (PLACE_TP
        # пропускается в _decide_entering т.к. FSM уже IN_POSITION),
        # а также при рестарте когда позиция есть но TP потерян.
        if state.active_tp_order_id is None and not ctx.fills_for_tp:
            return Decision(
                action=DecisionAction.PLACE_TP,
                reason="no_active_tp: выставляем TP для открытой позиции",
                tp_price=signal.tp_price,
            )

        # --- Проверяем события по TP -----------------------------------
        tp_fills = ctx.fills_for_tp

        for fill in tp_fills:
            if fill.is_fully_filled or (
                fill.is_partial
                and fill.fill_pct >= self._tp_partial_close_threshold_pct
            ):
                # TP исполнен достаточно — запускаем Close Protocol
                return Decision(
                    action=DecisionAction.CLOSE_PROTOCOL,
                    reason=(
                        f"tp_filled: fill_pct={fill.fill_pct:.1f}% >= "
                        f"{self._tp_partial_close_threshold_pct}%"
                    ),
                )

            if fill.is_partial and fill.fill_pct < self._tp_partial_close_threshold_pct:
                # TP частично исполнен, ниже порога — обновить qty, остаться IN_POSITION
                # Применение fill происходит в _apply_order_events до DecisionEngine.
                # Здесь просто продолжаем — никакого нового действия.
                logger.info(
                    "TP частично исполнен (%.1f%%), ниже порога %.1f%% — продолжаем",
                    fill.fill_pct,
                    float(self._tp_partial_close_threshold_pct),
                )
                # Проверяем нужно ли DCA после частичного TP
                break

            if fill.is_cancelled:
                # TP отменён вручную — позиция без защиты
                return Decision(
                    action=DecisionAction.SET_CLOSE_ONLY,
                    reason=(
                        f"tp_manually_cancelled: "
                        f"order_id={fill.exchange_order_id}. "
                        f"Позиция без TP."
                    ),
                )

            if fill.status == OrderStatus.UNKNOWN:
                from .errors import StopCraneError
                err = StopCraneError(
                    "TP-ордер исчез с биржи без статуса CANCELLED",
                    invariant="active_tp_order_exists_on_exchange",
                    expected={
                        "order_id": state.active_tp_order_id,
                        "type": "TP",
                    },
                    actually_found=None,
                    db_state=_state_snapshot(state),
                )
                return Decision(
                    action=DecisionAction.STOP_CRANE,
                    reason="tp_order_unknown_disappearance",
                    stop_crane_error=err,
                )

        # --- Проверяем события по DCA (EAGER-режим) --------------------
        dca_fills = ctx.fills_for_dca
        for fill in dca_fills:
            if fill.is_fully_filled or fill.is_partial:
                # DCA исполнен — нужно пересоздать TP по новой avg_price
                return Decision(
                    action=DecisionAction.REPLACE_TP,
                    reason=(
                        f"dca_filled: qty={fill.filled_qty}, "
                        f"dca_count будет {state.dca_count + 1}"
                    ),
                )
            if fill.is_cancelled:
                return Decision(
                    action=DecisionAction.SET_CLOSE_ONLY,
                    reason=f"dca_manually_cancelled: order_id={fill.exchange_order_id}",
                )

        # --- Проверка времени удержания позиции ------------------------
        position_decision = self._check_position_age(ctx, state)
        if position_decision is not None:
            return position_decision

        # --- DCA сигнал (LAZY режим или первичный DCA) -----------------
        if self._dca_mode == "LAZY":
            dca_decision = self._decide_dca_lazy(ctx, state, signal)
            if dca_decision is not None:
                return dca_decision

        return Decision(action=DecisionAction.WAIT, reason="in_position: ждём движения")

    # ------------------------------------------------------------------
    # CLOSING
    # ------------------------------------------------------------------

    def _decide_closing(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> Decision:
        if state.position_qty <= self._dust_threshold:
            # Позиция закрыта — Close Protocol должен финализировать цикл
            return Decision(
                action=DecisionAction.CLOSE_PROTOCOL,
                reason=f"closing: position_qty={state.position_qty} <= dust",
            )

        # TP висит на остаток — ждём или выставляем защитное закрытие.
        # Основная логика — в CloseProtocol.run().
        return Decision(
            action=DecisionAction.WAIT,
            reason=(
                f"closing: position_qty={state.position_qty} > dust, "
                f"ожидаем CloseProtocol"
            ),
        )

    # ------------------------------------------------------------------
    # WAITING_FOR_LIQUIDITY
    # ------------------------------------------------------------------

    def _decide_waiting_for_liquidity(self, state: "BotState") -> Decision:
        from .retry_manager import RetryManager

        if state.last_order_at is None:
            # Нет информации о последней попытке — сразу retry
            return Decision(
                action=DecisionAction.RETRY_LIQUIDITY,
                reason="waiting_for_liquidity: last_order_at=None, пробуем сразу",
            )

        elapsed_sec = (
            datetime.now(timezone.utc) - state.last_order_at
        ).total_seconds()

        retry_delay = RetryManager.get_delay_for_attempt(state.dca_count)

        if elapsed_sec >= retry_delay:
            return Decision(
                action=DecisionAction.RETRY_LIQUIDITY,
                reason=(
                    f"waiting_for_liquidity: прошло {elapsed_sec:.0f}с >= "
                    f"{retry_delay}с, попытка {state.dca_count + 1}"
                ),
            )

        return Decision(
            action=DecisionAction.WAIT,
            reason=(
                f"waiting_for_liquidity: осталось "
                f"{retry_delay - elapsed_sec:.0f}с до следующей попытки"
            ),
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _decide_dca_lazy(
        self,
        ctx: "TickContext",
        state: "BotState",
        signal: StrategySignal,
    ) -> "Decision | None":
        """
        Проверить нужен ли DCA в LAZY-режиме.

        Логика: Strategy вернула target_qty > current position_qty.
        DecisionEngine считает дельту и выставляет ордер на неё.

        Если уровней пробито несколько — обрабатываем один (ближайший).
        Остальные подберёт следующий тик (ТЗ 7: "обрабатывать по одному,
        начиная с ближайшего к текущей цене").
        """
        if signal.target_qty is None:
            return None

        delta_qty = signal.target_qty - state.position_qty

        if delta_qty < 0:
            # WFO запросил уменьшение позиции — блокируем
            raise TickSkippedError(
                f"Отрицательная дельта заблокирована: "
                f"target={signal.target_qty}, current={state.position_qty}. "
                f"Ждём закрытия через TP."
            )

        if delta_qty == 0:
            return None

        if state.dca_count >= self._max_dca_count:
            logger.warning(
                "DCA-сигнал проигнорирован: dca_count=%d >= MAX_DCA_COUNT=%d",
                state.dca_count,
                self._max_dca_count,
            )
            return None

        # Находим цену DCA из signal.dca_levels если есть,
        # иначе используем текущую цену (market DCA).
        dca_price = self._find_nearest_dca_level(
            ctx.price_data.ask, signal.dca_levels
        )

        return Decision(
            action=DecisionAction.PLACE_DCA,
            reason=(
                f"dca_signal_lazy: delta={delta_qty}, "
                f"dca_count={state.dca_count}, "
                f"reason={signal.reason}"
            ),
            dca_qty=delta_qty,
            dca_price=dca_price,
            tp_price=signal.tp_price,
        )

    def _check_position_age(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> "Decision | None":
        """Проверить не висит ли позиция дольше MAX_POSITION_DAYS."""
        if self._max_position_days is None or state.entered_at is None:
            return None

        age_days = (
            datetime.now(timezone.utc) - state.entered_at
        ).total_seconds() / 86400

        if age_days < self._max_position_days:
            return None

        if self._force_close_on_timeout:
            return Decision(
                action=DecisionAction.FORCE_CLOSE,
                reason=(
                    f"position_timeout: {age_days:.1f} дней >= "
                    f"{self._max_position_days} MAX_POSITION_DAYS, "
                    f"FORCE_CLOSE_ON_TIMEOUT=true"
                ),
            )

        # Только алерт — без принудительного закрытия
        logger.warning(
            "Позиция висит %.1f дней >= MAX_POSITION_DAYS=%d. "
            "FORCE_CLOSE_ON_TIMEOUT=false — только алерт.",
            age_days,
            self._max_position_days,
        )
        return None

    @staticmethod
    def _calc_slippage(current_price: Decimal, signal_price: Decimal) -> Decimal:
        """Вычислить % отклонения текущей цены от уровня сигнала."""
        if signal_price == 0:
            return Decimal(0)
        return abs(current_price - signal_price) / signal_price * 100

    @staticmethod
    def _find_nearest_dca_level(
        current_price: Decimal,
        levels: tuple[tuple[Decimal, Decimal], ...],
    ) -> "Decimal | None":
        """Найти цену ближайшего пробитого DCA-уровня."""
        if not levels:
            return None
        # Уровни ниже текущей цены, отсортированы по убыванию.
        # Ближайший — первый уровень ниже current_price.
        for price, _ in levels:
            if price <= current_price:
                return price
        return None


# ---------------------------------------------------------------------------
# Утилита: снапшот BotState в dict для payload STOP_CRANE
# ---------------------------------------------------------------------------


def _state_snapshot(state: "BotState") -> dict:
    """Минимальный снапшот состояния для диагностики STOP_CRANE."""
    return {
        "cycle_status":          state.cycle_status,
        "position_qty":          str(state.position_qty),
        "virtual_balance_free":  str(state.virtual_balance_free),
        "cycle_id":              state.cycle_id,
        "active_entry_order_id": state.active_entry_order_id,
        "active_tp_order_id":    state.active_tp_order_id,
        "dca_count":             state.dca_count,
    }
