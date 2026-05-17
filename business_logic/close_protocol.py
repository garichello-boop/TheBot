"""
CloseProtocol — обязательная 13-шаговая последовательность закрытия цикла.

Из ТЗ 7: переход CLOSING → IDLE разрешён только после прохождения
всех 13 шагов. Пропуск любого шага — ошибка.

Шаги:
  1.  Запретить новые BUY/DCA ордера.
  2.  Проверить статус TP на бирже.
  3.  Отменить все активные DCA-ордера текущего цикла.
  4.  Дождаться подтверждения cancel от биржи.
       → При ошибке: retry до CANCEL_MAX_RETRIES, потом STOP_CRANE.
  5.  Перечитать trades/fills с момента last_applied_trade_id.
  6.  Применить новые fills в applied_trades.
  7.  Пересчитать position_qty, quote_spent, quote_received, комиссии, avg_price.
  8.  Проверить: position_qty <= dust_threshold?
       → Да: позиция закрыта, перейти к шагу 11.
       → Нет: бот остаётся в CLOSING.
  9.  Применить CLOSE_REMAINDER_MODE:
       KEEP_TP           — TP остаётся открытым на остаток (дефолт).
       LIMIT_WITH_TIMEOUT — лимитный ордер, при истечении → CLOSE_ONLY.
       MARKET            — только если MAX_MARKET_CLOSE_SLIPPAGE_PCT позволяет.
  10. После закрытия остатка — перечитать trades/fills. Вернуться к шагу 6.
  11. Рассчитать PnL.
  12. Финальный чеклист:
       - position_qty <= dust_threshold
       - нет активных DCA текущего цикла
       - нет неучтённых fills
       - bot_state согласован с биржей
       → Если что-то не сходится — STOP_CRANE.
  13. FSM → IDLE. Emit CYCLE_CLOSED. Применить COOLDOWN_SEC если > 0.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from .errors import StopCraneError
from .types import OrderStatus

if TYPE_CHECKING:
    from broker import IBroker
    from bot_state import BotState, StateManager
    from observability import EventEmitter
    from .order_manager import OrderManager
    from .tick_context import TickContext

logger = logging.getLogger(__name__)

# Статус прогона CloseProtocol
_INCOMPLETE = "INCOMPLETE"   # позиция ещё не закрыта, следующий тик
_COMPLETE   = "COMPLETE"     # цикл финализирован, FSM → IDLE


class CloseProtocol:
    """
    Выполняет обязательную последовательность закрытия цикла.

    run() вызывается ботом при CLOSING или при заполнении TP >= порога.
    Возвращает (new_state, "COMPLETE" | "INCOMPLETE").

    COMPLETE → bot_loop переводит FSM в IDLE.
    INCOMPLETE → бот остаётся в CLOSING, run() вызовут на следующем тике.
    """

    def __init__(
        self,
        broker: "IBroker",
        order_manager: "OrderManager",
        state_manager: "StateManager",
        emitter: "EventEmitter",
        *,
        dust_threshold: Decimal,
        cancel_max_retries: int,
        close_remainder_mode: str,      # "KEEP_TP" | "LIMIT_WITH_TIMEOUT" | "MARKET"
        close_remainder_timeout_sec: int,
        max_market_close_slippage_pct: Decimal,
        cooldown_sec: int,
    ) -> None:
        self._broker                        = broker
        self._order_manager                 = order_manager
        self._state_manager                 = state_manager
        self._emitter                       = emitter
        self._dust_threshold                = dust_threshold
        self._cancel_max_retries            = cancel_max_retries
        self._close_remainder_mode          = close_remainder_mode
        self._close_remainder_timeout_sec   = close_remainder_timeout_sec
        self._max_market_close_slippage_pct = max_market_close_slippage_pct
        self._cooldown_sec                  = cooldown_sec

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def run(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> tuple["BotState", str]:
        """
        Запустить/продолжить Close Protocol.

        Returns:
          (new_state, status): status = "COMPLETE" | "INCOMPLETE".
        """
        logger.info(
            "CloseProtocol.run: cycle_id=%s, position_qty=%s",
            state.cycle_id, state.position_qty,
        )
        self._emitter.emit(
            event_type="CYCLE_CLOSING_STARTED",
            level="INFO",
            message=f"Close Protocol запущен: position_qty={state.position_qty}",
            payload={
                "cycle_id":      state.cycle_id,
                "position_qty":  str(state.position_qty),
                "dca_count":     state.dca_count,
            },
        )

        # Шаги 1-4: запрет DCA и mass cancel
        state = self._step_1_4_cancel_dca(state)

        # Шаги 5-7: применить fills и пересчитать позицию
        state = self._step_5_7_apply_fills(ctx, state)

        # Шаг 8: проверить закрыта ли позиция
        if state.position_qty <= self._dust_threshold:
            # Шаги 11-13: PnL, чеклист, FSM → IDLE
            state = self._step_11_13_finalize(ctx, state)
            return state, _COMPLETE

        # Шаг 9: CLOSE_REMAINDER_MODE
        state = self._step_9_handle_remainder(ctx, state)

        # Если после шага 9 позиция ещё открыта — продолжим на следующем тике
        if state.position_qty > self._dust_threshold:
            logger.info(
                "CloseProtocol: позиция %s > dust %s — ожидаем следующий тик",
                state.position_qty, self._dust_threshold,
            )
            return state, _INCOMPLETE

        # Шаги 11-13
        state = self._step_11_13_finalize(ctx, state)
        return state, _COMPLETE

    # ------------------------------------------------------------------
    # Шаги 1-4: Отмена DCA
    # ------------------------------------------------------------------

    def _step_1_4_cancel_dca(self, state: "BotState") -> "BotState":
        """Шаги 1-4: запретить DCA, отменить все активные, дождаться confirm."""
        dca_ids = list(state.active_dca_order_ids)

        if not dca_ids:
            return state

        logger.info(
            "CloseProtocol шаги 1-4: mass cancel %d DCA-ордеров",
            len(dca_ids),
        )

        for order_id in dca_ids:
            # OrderManager.cancel_order уже реализует retry до cancel_max_retries
            # и бросает StopCraneError если не вышло
            self._order_manager.cancel_order(order_id, order_role="DCA")

        # Очистить active_dca_order_ids
        state = self._state_manager.commit(
            state,
            replace(state, active_dca_order_ids=()),
        )
        return state

    # ------------------------------------------------------------------
    # Шаги 5-7: Применить fills
    # ------------------------------------------------------------------

    def _step_5_7_apply_fills(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> "BotState":
        """
        Шаги 5-7: перечитать fills с момента last_applied_trade_id,
        пересчитать position_qty, quote_received, комиссии.
        """
        tp_fills = ctx.fills_for_tp

        for fill in tp_fills:
            # Пропустить fill который уже применён partial_fill_handler-ом на этом тике.
            # last_applied_trade_id выставляется в handle_tp_fill — это гарантия
            # что заново считать quote_received не нужно.
            if fill.exchange_order_id == state.last_applied_trade_id:
                logger.debug(
                    "CloseProtocol шаги 5-7: fill %s уже применён — пропускаем",
                    fill.exchange_order_id,
                )
                continue

            if fill.is_fully_filled or fill.is_partial:
                received = fill.filled_qty * (fill.avg_fill_price or Decimal(0))
                new_qty  = state.position_qty - fill.filled_qty
                if new_qty < 0:
                    new_qty = Decimal(0)

                state = self._state_manager.commit(
                    state,
                    replace(
                        state,
                        position_qty=new_qty,
                        quote_received=(
                            state.quote_received + received - fill.commission
                        ),
                        last_applied_trade_id=fill.exchange_order_id,
                    ),
                )
                logger.info(
                    "CloseProtocol шаги 5-7: применён fill %s, qty после=%s",
                    fill.exchange_order_id, new_qty,
                )

        return state

    # ------------------------------------------------------------------
    # Шаг 9: Политика закрытия остатка
    # ------------------------------------------------------------------

    def _step_9_handle_remainder(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> "BotState":
        """
        Шаг 9: Применить CLOSE_REMAINDER_MODE к остатку позиции.

        KEEP_TP           — TP остаётся висеть. Ничего не делаем.
        LIMIT_WITH_TIMEOUT — проверяем timeout и переходим к следующему режиму.
        MARKET            — закрываем market-ордером если slippage приемлем.
        """
        if self._close_remainder_mode == "KEEP_TP":
            logger.debug(
                "CloseProtocol шаг 9: KEEP_TP — TP висит на остаток %s",
                state.position_qty,
            )
            return state

        if self._close_remainder_mode == "LIMIT_WITH_TIMEOUT":
            return self._handle_limit_with_timeout(ctx, state)

        if self._close_remainder_mode == "MARKET":
            return self._handle_market_close(ctx, state)

        logger.warning(
            "Неизвестный CLOSE_REMAINDER_MODE=%s — применяем KEEP_TP",
            self._close_remainder_mode,
        )
        return state

    def _handle_limit_with_timeout(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> "BotState":
        """LIMIT_WITH_TIMEOUT: после таймаута переходим к MARKET."""
        import datetime  # noqa: PLC0415
        if state.last_order_at is None:
            return state

        age_sec = (
            datetime.datetime.now(datetime.timezone.utc) - state.last_order_at
        ).total_seconds()

        if age_sec < self._close_remainder_timeout_sec:
            logger.debug(
                "CloseProtocol LIMIT_WITH_TIMEOUT: ожидаем ещё %.0f сек",
                self._close_remainder_timeout_sec - age_sec,
            )
            return state

        # Timeout истёк — переходим к MARKET
        logger.warning(
            "CloseProtocol LIMIT_WITH_TIMEOUT: таймаут истёк, переходим к MARKET"
        )
        self._emitter.emit(
            event_type="ORDER_CANCELLED",
            level="WARNING",
            message=(
                f"LIMIT_WITH_TIMEOUT истёк ({age_sec:.0f}с), "
                f"переходим к рыночному закрытию"
            ),
            payload={
                "timeout_sec":    self._close_remainder_timeout_sec,
                "elapsed_sec":    age_sec,
                "position_qty":   str(state.position_qty),
            },
        )
        return self._handle_market_close(ctx, state)

    def _handle_market_close(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> "BotState":
        """
        MARKET: закрыть остаток market-ордером.

        Проверяет MAX_MARKET_CLOSE_SLIPPAGE_PCT перед отправкой.
        Если slippage недопустим — остаёмся в KEEP_TP.
        """
        bid = ctx.price_data.bid
        avg = state.position_avg_price

        if avg > 0:
            slippage_pct = abs(avg - bid) / avg * 100
            if slippage_pct > self._max_market_close_slippage_pct:
                logger.warning(
                    "MARKET close пропущен: slippage %.2f%% > MAX=%.2f%%",
                    float(slippage_pct),
                    float(self._max_market_close_slippage_pct),
                )
                self._emitter.emit(
                    event_type="ORDER_CREATE_FAILED",
                    level="WARNING",
                    message=(
                        f"Рыночное закрытие отложено: slippage "
                        f"{slippage_pct:.2f}% > "
                        f"{self._max_market_close_slippage_pct}%"
                    ),
                    payload={
                        "slippage_pct":    str(slippage_pct),
                        "max_allowed_pct": str(self._max_market_close_slippage_pct),
                        "bid":             str(bid),
                        "avg_price":       str(avg),
                    },
                )
                return state

        # Отменяем существующий TP перед рыночным закрытием
        if state.active_tp_order_id:
            try:
                self._order_manager.cancel_order(
                    state.active_tp_order_id, order_role="TP"
                )
                state = self._state_manager.commit(
                    state,
                    replace(state, active_tp_order_id=None),
                )
            except StopCraneError:
                raise  # пробрасываем — нельзя закрывать без отмены TP
            except Exception as exc:
                logger.warning("Не удалось отменить TP перед MARKET close: %s", exc)

        # Размещаем MARKET SELL на весь остаток
        _, state = self._order_manager.place_tp_order(
            state,
            qty=state.position_qty,
            price=bid,  # PaperBroker использует bid, BybitBroker — MARKET
            ticker=ctx.ticker,
            cycle_id=state.cycle_id or "",
        )

        self._emitter.emit(
            event_type="ORDER_CREATED",
            level="INFO",
            message=f"Market close: {state.position_qty} @ ~{bid}",
            payload={
                "qty":      str(state.position_qty),
                "price":    str(bid),
                "mode":     "MARKET",
                "reason":   "close_remainder",
            },
        )
        return state

    # ------------------------------------------------------------------
    # Шаги 11-13: Финализация цикла
    # ------------------------------------------------------------------

    def _step_11_13_finalize(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> "BotState":
        """
        Шаги 11-13: PnL, финальный чеклист, FSM → IDLE.

        Шаг 12: Финальный чеклист — если что-то не сходится → STOP_CRANE.
        Шаг 13: FSM → IDLE, emit CYCLE_CLOSED, cooldown.
        """
        # Шаг 11: PnL
        pnl = state.quote_received - state.quote_spent

        # Захватить до сброса — state после commit будет обнулён
        log_quote_received = state.quote_received
        log_quote_spent    = state.quote_spent

        logger.info(
            "CloseProtocol PnL breakdown: quote_received=%s, quote_spent=%s, pnl=%s",
            state.quote_received, state.quote_spent, pnl,
        )
        logger.info(
            "CloseProtocol финализация: cycle_id=%s, pnl=%s",
            state.cycle_id, pnl,
        )

        # Шаг 12: Финальный чеклист
        self._final_checklist(ctx, state)

        # Шаг 13: FSM → IDLE
        state = self._state_manager.commit(
            state,
            replace(
                state,
                cycle_status="IDLE",
                cycle_id=None,
                position_qty=Decimal(0),
                position_avg_price=Decimal(0),
                quote_spent=Decimal(0),
                quote_received=Decimal(0),
                dca_count=0,
                active_entry_order_id=None,
                active_tp_order_id=None,
                active_dca_order_ids=(),
                pending_client_order_id=None,
                last_applied_trade_id=None,
            ),
        )

        self._emitter.emit(
            event_type="CYCLE_CLOSED",
            level="INFO",
            message=f"Цикл закрыт: PnL={pnl}",
            payload={
                "cycle_id":       ctx.bot_state.cycle_id,
                "pnl":            str(pnl),
                "quote_spent":    str(log_quote_spent),
                "quote_received": str(log_quote_received),
                "dca_count":      ctx.bot_state.dca_count,
            },
        )

        # Cooldown перед следующим циклом
        if self._cooldown_sec > 0:
            logger.info("Cooldown %ds после закрытия цикла", self._cooldown_sec)
            time.sleep(self._cooldown_sec)

        return state

    def _final_checklist(
        self,
        ctx: "TickContext",
        state: "BotState",
    ) -> None:
        """
        Шаг 12: проверить что всё чисто перед переходом в IDLE.
        При нарушении → StopCraneError.
        """
        errors: list[str] = []

        if state.position_qty > self._dust_threshold:
            errors.append(
                f"position_qty={state.position_qty} > dust={self._dust_threshold}"
            )

        if state.active_dca_order_ids:
            errors.append(
                f"active_dca_order_ids не пусты: {state.active_dca_order_ids}"
            )

        # Проверяем нет ли открытых ордеров по тикеру
        open_orders = ctx.open_orders
        cycle_id = state.cycle_id or ""
        cycle_orders = [
            o for o in open_orders
            if o.get("cycle_id") == cycle_id or o.get("symbol") == ctx.ticker
        ]
        if cycle_orders:
            errors.append(
                f"На бирже остались ордеры: {[o.get('orderId') for o in cycle_orders]}"
            )

        if errors:
            raise StopCraneError(
                f"Close Protocol шаг 12: финальный чеклист не прошёл: "
                f"{'; '.join(errors)}",
                invariant="clean_state_before_idle",
                expected={
                    "position_qty":      "<= dust",
                    "active_dca_orders": "empty",
                    "open_orders":       "empty",
                },
                actually_found={
                    "errors": errors,
                },
                db_state={
                    "cycle_id":       state.cycle_id,
                    "position_qty":   str(state.position_qty),
                    "dca_order_ids":  list(state.active_dca_order_ids),
                    "open_on_exchange": len(cycle_orders),
                },
            )
