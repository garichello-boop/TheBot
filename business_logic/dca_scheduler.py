"""
DCAScheduler — управление DCA-ордерами в EAGER и LAZY режимах.

EAGER режим (из ТЗ 7):
  При входе в позицию все DCA-ордеры выставляются сразу.
  Биржа исполняет их автоматически при достижении цены.
  При исполнении TP → mass cancel всех DCA немедленно.
  Без mass cancel DCA откроют новую позицию.

LAZY режим (из ТЗ 7):
  DCA выставляется когда цена достигает уровня на тике.
  Пробой нескольких уровней за тик → обрабатываем по одному,
  начиная с ближайшего к текущей цене. Остальные — следующий тик.
  Telegram-уведомление о пропуске уровней.

DCA уровни хранятся в strategy_params из CycleSnapshot.
Формат зависит от стратегии — DCAScheduler читает через абстракцию.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from .types import DecisionAction

if TYPE_CHECKING:
    from .tick_context import TickContext
    from .order_manager import OrderManager
    from bot_state import BotState, StateManager
    from bot_config import CycleSnapshot
    from observability import EventEmitter

logger = logging.getLogger(__name__)


class DCAScheduler:
    """
    Управляет расстановкой DCA-ордеров в двух режимах.

    Инициализируется при старте бота с параметрами из AppSettings.
    """

    def __init__(
        self,
        order_manager: "OrderManager",
        state_manager: "StateManager",
        emitter: "EventEmitter",
        *,
        dca_mode: str,          # "EAGER" | "LAZY"
        max_dca_count: int,
    ) -> None:
        self._order_manager = order_manager
        self._state_manager = state_manager
        self._emitter       = emitter
        self._dca_mode      = dca_mode
        self._max_dca_count = max_dca_count

    # ------------------------------------------------------------------
    # EAGER: размещение всех DCA при входе в позицию
    # ------------------------------------------------------------------

    def place_eager_dca_orders(
        self,
        state: "BotState",
        snapshot: "CycleSnapshot",
        ticker: str,
    ) -> "BotState":
        """
        Выставить все DCA-ордеры сразу при открытии позиции (EAGER-режим).

        Уровни берутся из snapshot.strategy_params['dca_levels'].
        Ожидаемый формат:
          [{"price": "3100.0", "qty": "0.5"}, ...]
        отсортированы по убыванию цены (первый уровень — ближайший к entry).

        Если strategy_params не содержит dca_levels — пропускаем тихо
        (стратегия без DCA или LAZY-только конфиг).
        """
        if self._dca_mode != "EAGER":
            return state

        raw_levels = snapshot.strategy_params.get("dca_levels", [])
        if not raw_levels:
            logger.debug("EAGER DCA: dca_levels пуст — пропускаем")
            return state

        # Ограничиваем количество уровней
        levels = raw_levels[: self._max_dca_count]
        if len(raw_levels) > self._max_dca_count:
            logger.warning(
                "EAGER DCA: в параметрах %d уровней, но MAX_DCA_COUNT=%d. "
                "Лишние уровни отброшены.",
                len(raw_levels), self._max_dca_count,
            )

        current_state = state

        for i, level in enumerate(levels):
            try:
                price = Decimal(str(level["price"]))
                qty   = Decimal(str(level["qty"]))
            except (KeyError, Exception) as exc:
                logger.error(
                    "EAGER DCA: некорректный формат уровня #%d: %s — %s",
                    i, level, exc,
                )
                continue

            _, current_state = self._order_manager.place_dca_order(
                current_state,
                qty=qty,
                price=price,
                ticker=ticker,
                cycle_id=current_state.cycle_id or "",
            )
            logger.info(
                "EAGER DCA #%d: order размещён @ %s, qty=%s",
                i + 1, price, qty,
            )

        return current_state

    # ------------------------------------------------------------------
    # LAZY: проверка уровней на каждом тике
    # ------------------------------------------------------------------

    def get_triggered_levels(
        self,
        ctx: "TickContext",
        state: "BotState",
        snapshot: "CycleSnapshot",
    ) -> list[tuple[Decimal, Decimal]]:
        """
        Найти DCA-уровни, пробитые текущей ценой (LAZY-режим).

        Returns:
          Список (price, qty) отсортированный от ближайшего к текущей цене
          к дальнему. Первый элемент нужно исполнить сейчас, остальные
          обрабатываются на следующих тиках.

          Пустой список — нет пробитых уровней.
        """
        if self._dca_mode != "LAZY":
            return []

        raw_levels = snapshot.strategy_params.get("dca_levels", [])
        if not raw_levels:
            return []

        current_price = ctx.price_data.ask
        already_done  = state.dca_count
        remaining     = self._max_dca_count - already_done

        if remaining <= 0:
            return []

        triggered: list[tuple[Decimal, Decimal]] = []

        for i, level in enumerate(raw_levels):
            if i < already_done:
                # Этот уровень уже применён ранее
                continue
            if i >= self._max_dca_count:
                break

            try:
                price = Decimal(str(level["price"]))
                qty   = Decimal(str(level["qty"]))
            except (KeyError, Exception) as exc:
                logger.error("LAZY DCA: некорректный уровень #%d: %s", i, exc)
                continue

            if current_price <= price:
                triggered.append((price, qty))

        if not triggered:
            return []

        # Сортируем от ближайшего (наибольший price) к дальнему
        triggered.sort(key=lambda x: x[0], reverse=True)

        if len(triggered) > 1:
            # Несколько уровней пробито за один тик — уведомить
            self._emitter.emit(
                event_type="ORDER_PARTIALLY_FILLED",  # используем WARNING
                level="WARNING",
                message=(
                    f"За тик пробито {len(triggered)} DCA-уровней, "
                    f"обрабатываем только ближайший. "
                    f"Остальные {len(triggered) - 1} — следующие тики."
                ),
                payload={
                    "triggered_count": len(triggered),
                    "processing_now":  str(triggered[0][0]),
                    "deferred_prices": [str(p) for p, _ in triggered[1:]],
                    "dca_count":       state.dca_count,
                },
            )
            # Возвращаем только первый (ближайший) — остальные следующий тик
            return triggered[:1]

        return triggered

    # ------------------------------------------------------------------
    # Пересоздание TP после DCA
    # ------------------------------------------------------------------

    def recreate_tp_after_dca(
        self,
        state: "BotState",
        *,
        new_avg_price: Decimal,
        new_position_qty: Decimal,
        tp_price: Decimal,
        ticker: str,
    ) -> "BotState":
        """
        После исполнения DCA: отменить старый TP, выставить новый.

        Последовательность (ТЗ 7):
          1. Cancel текущего TP.
          2. Strategy пересчитала TP по новой avg_price (вызывающий код).
          3. Выставить новый TP на полный текущий position_qty.
        """
        current_tp_id = state.active_tp_order_id

        if current_tp_id is not None:
            logger.info(
                "DCA: отменяем старый TP %s перед пересозданием", current_tp_id
            )
            self._order_manager.cancel_order(current_tp_id, order_role="TP")
            state = self._state_manager.commit(
                state,
                replace(state, active_tp_order_id=None),
            )
            self._emitter.emit(
                event_type="TP_CANCEL_REQUESTED",
                level="INFO",
                message=f"TP {current_tp_id} отменён для пересоздания после DCA",
                payload={
                    "order_id":       current_tp_id,
                    "new_avg_price":  str(new_avg_price),
                    "new_qty":        str(new_position_qty),
                    "new_tp_price":   str(tp_price),
                },
            )

        self._emitter.emit(
            event_type="TP_REPLACE_STARTED",
            level="INFO",
            message="Выставляем новый TP после DCA",
            payload={
                "avg_price": str(new_avg_price),
                "qty":       str(new_position_qty),
                "tp_price":  str(tp_price),
            },
        )

        _, state = self._order_manager.place_tp_order(
            state,
            qty=new_position_qty,
            price=tp_price,
            ticker=ticker,
            cycle_id=state.cycle_id or "",
        )

        self._emitter.emit(
            event_type="TP_REPLACE_FINISHED",
            level="INFO",
            message=f"Новый TP выставлен @ {tp_price}",
            payload={
                "tp_price":   str(tp_price),
                "qty":        str(new_position_qty),
                "avg_price":  str(new_avg_price),
            },
        )

        return state
