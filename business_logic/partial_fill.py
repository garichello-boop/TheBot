"""
PartialFillHandler — обработка частичного исполнения ордеров.

Единая политика для всех типов ордеров (ТЗ 7):

  ENTRY (частичное исполнение):
    - filled_pct >= PARTIAL_FILL_THRESHOLD_PCT → открыть позицию на filled_qty.
      Остаток ордера отменить (лимитный) или принять как есть (маркет).
    - filled_pct < порога → отменить ордер полностью, вернуться в IDLE.

  DCA (частичное исполнение):
    - Принять filled_qty, пересчитать avg_price от фактического объёма.
    - Остаток ордера отменить.
    - Пересоздать TP по новой avg_price и текущему position_qty.

  TP (частичное исполнение):
    - Уменьшить position_qty на filled_qty.
    - TP продолжает висеть на остаток (не трогать).
    - Если filled_pct >= TP_PARTIAL_CLOSE_THRESHOLD_PCT → FSM в CLOSING.
      DCA запрещены, mass cancel. TP НЕ отменяется.
    - CLOSING → IDLE только через Close Protocol когда position_qty <= dust.

  Ручная отмена любого ордера:
    - Telegram-алерт с типом отменённого ордера.
    - Бот → CLOSE_ONLY (статус в bot_configs). Ждёт явной команды.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from .types import FillEvent, OrderType, OrderStatus

if TYPE_CHECKING:
    from bot_state import BotState, StateManager
    from observability import EventEmitter
    from .order_manager import OrderManager

logger = logging.getLogger(__name__)


class PartialFillHandler:
    """Применяет результаты частичного исполнения к bot_state."""

    def __init__(
        self,
        state_manager: "StateManager",
        order_manager: "OrderManager",
        emitter: "EventEmitter",
        *,
        partial_fill_threshold_pct: Decimal,
        tp_partial_close_threshold_pct: Decimal,
    ) -> None:
        self._state_manager                  = state_manager
        self._order_manager                  = order_manager
        self._emitter                        = emitter
        self._partial_fill_threshold_pct     = partial_fill_threshold_pct
        self._tp_partial_close_threshold_pct = tp_partial_close_threshold_pct

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def handle_entry_fill(
        self,
        state: "BotState",
        fill: FillEvent,
    ) -> tuple["BotState", bool]:
        """
        Применить fill по ордеру на вход.

        Returns:
          (new_state, position_opened)
          position_opened=True → нужно выставить TP.
          position_opened=False → вернулись в IDLE.
        """
        if fill.is_fully_filled:
            return self._accept_entry(state, fill), True

        if fill.is_partial:
            if fill.fill_pct >= self._partial_fill_threshold_pct:
                # Достаточно для открытия позиции
                logger.info(
                    "Entry частично исполнен (%.1f%% >= %.1f%%) — принимаем позицию",
                    float(fill.fill_pct),
                    float(self._partial_fill_threshold_pct),
                )
                new_state = self._accept_entry(state, fill)

                # Отменяем остаток лимитного ордера
                if state.active_entry_order_id:
                    try:
                        self._order_manager.cancel_order(
                            state.active_entry_order_id, order_role="ENTRY"
                        )
                    except Exception as exc:
                        logger.warning(
                            "Не удалось отменить остаток entry ордера %s: %s",
                            state.active_entry_order_id, exc,
                        )
                return new_state, True
            else:
                # Слишком мало исполнено — отменяем и возвращаемся в IDLE
                logger.info(
                    "Entry частично исполнен (%.1f%% < %.1f%%) — отменяем, IDLE",
                    float(fill.fill_pct),
                    float(self._partial_fill_threshold_pct),
                )
                self._emitter.emit(
                    event_type="ORDER_PARTIALLY_FILLED",
                    level="WARNING",
                    message=(
                        f"Entry исполнен только на {fill.fill_pct:.1f}% "
                        f"(< {self._partial_fill_threshold_pct}%) — возврат в IDLE"
                    ),
                    payload={
                        "order_id":    fill.exchange_order_id,
                        "fill_pct":    str(fill.fill_pct),
                        "filled_qty":  str(fill.filled_qty),
                        "order_type":  "ENTRY",
                    },
                )
                if state.active_entry_order_id:
                    self._order_manager.cancel_order(
                        state.active_entry_order_id, order_role="ENTRY"
                    )
                # Сбросить entry order id, не трогать позицию (её нет)
                new_state = self._state_manager.commit(
                    state,
                    replace(
                        state,
                        active_entry_order_id=None,
                        pending_client_order_id=None,
                    ),
                )
                return new_state, False

        # CANCELLED или REJECTED — вызывающий код должен обрабатывать отдельно
        return state, False

    # ------------------------------------------------------------------
    # DCA
    # ------------------------------------------------------------------

    def handle_dca_fill(
        self,
        state: "BotState",
        fill: FillEvent,
    ) -> "BotState":
        """
        Применить fill по DCA-ордеру.

        Пересчитывает avg_price, обновляет quote_spent и position_qty.
        Удаляет ордер из active_dca_order_ids.
        Вызывающий код (bot_loop) после этого вызывает recreate_tp_after_dca.
        """
        if not (fill.is_fully_filled or fill.is_partial):
            return state

        if fill.is_partial:
            self._emitter.emit(
                event_type="ORDER_PARTIALLY_FILLED",
                level="WARNING",
                message=f"DCA частично исполнен: {fill.fill_pct:.1f}%",
                payload={
                    "order_id":       fill.exchange_order_id,
                    "fill_pct":       str(fill.fill_pct),
                    "filled_qty":     str(fill.filled_qty),
                    "remaining_qty":  str(fill.remaining_qty),
                    "order_type":     "DCA",
                },
            )
            # Отменить остаток ордера
            try:
                self._order_manager.cancel_order(
                    fill.exchange_order_id, order_role="DCA"
                )
            except Exception as exc:
                logger.warning("Не удалось отменить остаток DCA: %s", exc)

        # Пересчитать avg_price и quote_spent
        fill_cost = fill.filled_qty * (fill.avg_fill_price or Decimal(0))
        new_qty   = state.position_qty + fill.filled_qty
        new_spent = state.quote_spent + fill_cost + fill.commission

        if new_qty > 0:
            new_avg = new_spent / new_qty
        else:
            new_avg = state.position_avg_price

        # Убрать ордер из списка активных DCA
        updated_dca_ids = tuple(
            oid for oid in state.active_dca_order_ids
            if oid != fill.exchange_order_id
        )

        new_state = self._state_manager.commit(
            state,
            replace(
                state,
                position_qty=new_qty,
                position_avg_price=new_avg,
                quote_spent=new_spent,
                dca_count=state.dca_count + 1,
                active_dca_order_ids=updated_dca_ids,
                last_applied_trade_id=fill.exchange_order_id,
            ),
        )

        self._emitter.emit(
            event_type="ORDER_FILLED",
            level="INFO",
            message=f"DCA исполнен: qty={fill.filled_qty}, avg={fill.avg_fill_price}",
            payload={
                "order_id":       fill.exchange_order_id,
                "filled_qty":     str(fill.filled_qty),
                "avg_price":      str(fill.avg_fill_price),
                "commission":     str(fill.commission),
                "new_position":   str(new_qty),
                "new_avg_price":  str(new_avg),
                "dca_count":      new_state.dca_count,
            },
        )
        return new_state

    # ------------------------------------------------------------------
    # TP
    # ------------------------------------------------------------------

    def handle_tp_fill(
        self,
        state: "BotState",
        fill: FillEvent,
    ) -> tuple["BotState", bool]:
        """
        Применить fill по TP-ордеру.

        Returns:
          (new_state, should_close)
          should_close=True → запустить Close Protocol.
          should_close=False → IN_POSITION продолжается.
        """
        if not (fill.is_fully_filled or fill.is_partial):
            return state, False

        received = fill.filled_qty * (fill.avg_fill_price or Decimal(0))
        new_qty  = state.position_qty - fill.filled_qty
        if new_qty < 0:
            new_qty = Decimal(0)

        new_state = self._state_manager.commit(
            state,
            replace(
                state,
                position_qty=new_qty,
                quote_received=state.quote_received + received - fill.commission,
                last_applied_trade_id=fill.exchange_order_id,
            ),
        )

        if fill.is_fully_filled:
            self._emitter.emit(
                event_type="ORDER_FILLED",
                level="INFO",
                message=f"TP полностью исполнен: qty={fill.filled_qty}",
                payload={
                    "order_id":   fill.exchange_order_id,
                    "filled_qty": str(fill.filled_qty),
                    "avg_price":  str(fill.avg_fill_price),
                    "commission": str(fill.commission),
                },
            )
            return new_state, True

        # Частичное исполнение TP
        self._emitter.emit(
            event_type="TP_PARTIALLY_FILLED",
            level="INFO",
            message=(
                f"TP частично исполнен: {fill.fill_pct:.1f}%, "
                f"остаток={new_qty}"
            ),
            payload={
                "order_id":          fill.exchange_order_id,
                "fill_pct":          str(fill.fill_pct),
                "remaining_qty":     str(fill.remaining_qty),
                "position_qty_after": str(new_qty),
            },
        )

        should_close = fill.fill_pct >= self._tp_partial_close_threshold_pct
        if should_close:
            logger.info(
                "TP заполнен %.1f%% >= порога %.1f%% — переходим в CLOSING",
                float(fill.fill_pct),
                float(self._tp_partial_close_threshold_pct),
            )
        return new_state, should_close

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------

    def _accept_entry(
        self,
        state: "BotState",
        fill: FillEvent,
    ) -> "BotState":
        """Зафиксировать открытие позиции по fill."""
        fill_cost = fill.filled_qty * (fill.avg_fill_price or Decimal(0))

        new_state = self._state_manager.commit(
            state,
            replace(
                state,
                position_qty=fill.filled_qty,
                position_avg_price=fill.avg_fill_price or Decimal(0),
                quote_spent=fill_cost + fill.commission,
                active_entry_order_id=None,
                pending_client_order_id=None,
                last_applied_trade_id=fill.exchange_order_id,
            ),
        )

        self._emitter.emit(
            event_type="ORDER_FILLED",
            level="INFO",
            message=f"Вход: qty={fill.filled_qty} @ {fill.avg_fill_price}",
            payload={
                "order_id":   fill.exchange_order_id,
                "filled_qty": str(fill.filled_qty),
                "avg_price":  str(fill.avg_fill_price),
                "commission": str(fill.commission),
                "fill_pct":   str(fill.fill_pct),
            },
        )
        return new_state
