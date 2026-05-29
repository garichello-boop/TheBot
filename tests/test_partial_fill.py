"""
tests/test_partial_fill.py — Unit тесты PartialFillHandler.

PartialFillHandler применяет результаты частичного исполнения к bot_state.
Единая политика для ENTRY, DCA, TP ордеров.

Покрытие:
  ENTRY: полное исполнение, частичное >= порога, частичное < порога
  DCA:   полное исполнение, частичное (отмена остатка, пересчёт avg_price)
  TP:    полное исполнение, частичное >= порога → CLOSING, частичное < порога
         математика: position_qty, quote_received, fill_pct граничные случаи
"""
from __future__ import annotations

# Мокаем внешние зависимости до импортов проекта
import sys
from unittest.mock import MagicMock, call

_db = MagicMock()
sys.modules.setdefault("db", _db)
sys.modules.setdefault("db.connection", _db)
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())

import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Optional

import pytest

from business_logic.partial_fill import PartialFillHandler
from business_logic.types import FillEvent, OrderStatus, OrderType


# ---------------------------------------------------------------------------
# FakeState — минимальный датакласс совместимый с dataclasses.replace()
# PartialFillHandler вызывает replace(state, ...) внутри, поэтому
# SimpleNamespace не подходит — нужен настоящий dataclass.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeState:
    cycle_status: str = "IDLE"
    position_qty: Decimal = Decimal("0")
    position_avg_price: Optional[Decimal] = None
    dca_count: int = 0
    active_entry_order_id: Optional[str] = None
    active_tp_order_id: Optional[str] = None
    active_dca_order_ids: tuple = ()
    cycle_id: Optional[str] = "cycle_001"
    quote_spent: Decimal = Decimal("0")
    quote_received: Decimal = Decimal("0")
    virtual_balance_free: Decimal = Decimal("1000")
    last_applied_trade_id: Optional[str] = None
    pending_client_order_id: Optional[str] = None
    bot_id: str = "bot1"
    user_id: str = "user1"

    @property
    def has_position(self) -> bool:
        return self.position_qty > Decimal("0")


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_handler(
    partial_fill_threshold_pct: float = 80.0,
    tp_partial_close_threshold_pct: float = 80.0,
) -> tuple[PartialFillHandler, MagicMock, MagicMock, MagicMock]:
    """
    Вернуть (handler, state_manager, order_manager, emitter).

    state_manager.commit() настроен возвращать второй аргумент (new_state),
    что имитирует успешное сохранение без реального PostgreSQL.
    """
    state_manager = MagicMock()
    state_manager.commit = MagicMock(side_effect=lambda old, new: new)

    order_manager = MagicMock()
    emitter = MagicMock()

    handler = PartialFillHandler(
        state_manager=state_manager,
        order_manager=order_manager,
        emitter=emitter,
        partial_fill_threshold_pct=Decimal(str(partial_fill_threshold_pct)),
        tp_partial_close_threshold_pct=Decimal(str(tp_partial_close_threshold_pct)),
    )
    return handler, state_manager, order_manager, emitter


def make_fill(
    order_type: OrderType,
    status: OrderStatus = OrderStatus.FILLED,
    filled_qty: float = 0.1,
    remaining_qty: float = 0.0,
    avg_fill_price: float = 50000.0,
    commission: float = 5.0,
    order_id: str = "order1",
) -> FillEvent:
    return FillEvent(
        exchange_order_id=order_id,
        client_order_id=f"client_{order_id}",
        status=status,
        order_type=order_type,
        filled_qty=Decimal(str(filled_qty)),
        remaining_qty=Decimal(str(remaining_qty)),
        avg_fill_price=Decimal(str(avg_fill_price)),
        commission=Decimal(str(commission)),
        timestamp_ms=int(time.time() * 1000),
    )


def make_entry_state(order_id: str = "entry1") -> FakeState:
    return FakeState(
        cycle_status="ENTERING",
        active_entry_order_id=order_id,
        position_qty=Decimal("0"),
    )


def make_position_state(
    qty: float = 0.1,
    avg: float = 50000.0,
    quote_spent: float = 5000.0,
    tp_id: str = "tp1",
    dca_ids: tuple = (),
    dca_count: int = 0,
) -> FakeState:
    return FakeState(
        cycle_status="IN_POSITION",
        position_qty=Decimal(str(qty)),
        position_avg_price=Decimal(str(avg)),
        quote_spent=Decimal(str(quote_spent)),
        active_tp_order_id=tp_id,
        active_dca_order_ids=dca_ids,
        dca_count=dca_count,
    )


# ---------------------------------------------------------------------------
# ENTRY fill tests
# ---------------------------------------------------------------------------

class TestEntryFill:

    def test_full_fill_opens_position(self):
        """Полное исполнение entry → позиция открыта."""
        handler, sm, om, em = make_handler()
        state = make_entry_state()
        fill = make_fill(
            order_type=OrderType.ENTRY,
            status=OrderStatus.FILLED,
            filled_qty=0.1,
            remaining_qty=0.0,
            avg_fill_price=50000.0,
            commission=5.0,
        )

        new_state, position_opened = handler.handle_entry_fill(state, fill)

        assert position_opened is True
        assert new_state.position_qty == Decimal("0.1")
        assert new_state.position_avg_price == Decimal("50000.0")
        assert new_state.quote_spent == Decimal("0.1") * Decimal("50000.0") + Decimal("5.0")
        assert new_state.active_entry_order_id is None

    def test_full_fill_emits_trade_applied(self):
        """Полное исполнение → эмитируется TRADE_APPLIED."""
        handler, sm, om, em = make_handler()
        state = make_entry_state()
        fill = make_fill(OrderType.ENTRY, status=OrderStatus.FILLED)

        handler.handle_entry_fill(state, fill)

        em.emit.assert_called()
        event_types = [
            c.kwargs.get("event_type") or (c.args[0] if c.args else None)
            for c in em.emit.call_args_list
            if c.args or c.kwargs
        ]
        assert any("TRADE_APPLIED" in str(et) for et in event_types if et is not None)

    def test_partial_above_threshold_opens_position(self):
        """Частичное >= порога (80%) → позиция открыта, остаток отменяется."""
        handler, sm, om, em = make_handler(partial_fill_threshold_pct=80.0)
        state = make_entry_state("entry1")
        # 85% исполнено
        fill = make_fill(
            order_type=OrderType.ENTRY,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.085,
            remaining_qty=0.015,   # 85% от 0.1
        )

        new_state, position_opened = handler.handle_entry_fill(state, fill)

        assert position_opened is True
        assert new_state.position_qty == Decimal("0.085")
        # Остаток ордера отменяется
        om.cancel_order.assert_called_once_with("entry1", order_role="ENTRY")

    def test_partial_below_threshold_cancels_and_idles(self):
        """Частичное < порога → позиция не открывается, возврат в IDLE."""
        handler, sm, om, em = make_handler(partial_fill_threshold_pct=80.0)
        state = make_entry_state("entry1")
        # 50% исполнено — меньше порога
        fill = make_fill(
            order_type=OrderType.ENTRY,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.05,
            remaining_qty=0.05,
        )

        new_state, position_opened = handler.handle_entry_fill(state, fill)

        assert position_opened is False
        # Позиция не открылась
        assert new_state.position_qty == Decimal("0")
        # Ордер отменён
        om.cancel_order.assert_called_once()

    def test_threshold_boundary_exactly_80_pct_opens(self):
        """Ровно 80% = порог → позиция открывается (>= порога)."""
        handler, sm, om, em = make_handler(partial_fill_threshold_pct=80.0)
        state = make_entry_state()
        fill = make_fill(
            order_type=OrderType.ENTRY,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.08,
            remaining_qty=0.02,   # ровно 80%
        )

        _, position_opened = handler.handle_entry_fill(state, fill)
        assert position_opened is True

    def test_threshold_boundary_79_pct_does_not_open(self):
        """79% < порога 80% → позиция не открывается."""
        handler, sm, om, em = make_handler(partial_fill_threshold_pct=80.0)
        state = make_entry_state()
        fill = make_fill(
            order_type=OrderType.ENTRY,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.079,
            remaining_qty=0.021,   # ~79%
        )

        _, position_opened = handler.handle_entry_fill(state, fill)
        assert position_opened is False

    def test_cancelled_or_unknown_returns_false(self):
        """CANCELLED/UNKNOWN fill → позиция не открывается."""
        handler, sm, om, em = make_handler()
        state = make_entry_state()
        fill = make_fill(
            order_type=OrderType.ENTRY,
            status=OrderStatus.CANCELLED,
            filled_qty=0.0,
            remaining_qty=0.1,
        )

        _, position_opened = handler.handle_entry_fill(state, fill)
        assert position_opened is False


# ---------------------------------------------------------------------------
# DCA fill tests
# ---------------------------------------------------------------------------

class TestDcaFill:

    def test_full_dca_fill_updates_position(self):
        """Полное DCA исполнение → qty, avg_price, dca_count обновлены."""
        handler, sm, om, em = make_handler()
        # Текущая позиция: 0.1 BTC @ 50000, потрачено 5005 USDT
        state = make_position_state(qty=0.1, avg=50000.0, quote_spent=5005.0, dca_count=0)
        # DCA: покупаем ещё 0.1 BTC @ 49000
        fill = make_fill(
            order_type=OrderType.DCA,
            status=OrderStatus.FILLED,
            filled_qty=0.1,
            remaining_qty=0.0,
            avg_fill_price=49000.0,
            commission=4.9,
        )

        new_state = handler.handle_dca_fill(state, fill)

        # new_qty = 0.1 + 0.1 = 0.2
        assert new_state.position_qty == Decimal("0.2")
        # new_spent = 5005 + 0.1*49000 + 4.9 = 5005 + 4900 + 4.9 = 9909.9
        expected_spent = Decimal("5005.0") + Decimal("0.1") * Decimal("49000.0") + Decimal("4.9")
        assert new_state.quote_spent == expected_spent
        # avg_price = quote_spent / position_qty = 9909.9 / 0.2 = 49549.5
        expected_avg = expected_spent / Decimal("0.2")
        assert new_state.position_qty > Decimal("0")
        assert new_state.position_avg_price == expected_avg
        # dca_count инкрементирован
        assert new_state.dca_count == 1

    def test_dca_removes_order_from_active_ids(self):
        """После DCA — ордер убирается из active_dca_order_ids."""
        handler, sm, om, em = make_handler()
        state = FakeState(
            cycle_status="IN_POSITION",
            position_qty=Decimal("0.1"),
            quote_spent=Decimal("5000"),
            active_dca_order_ids=("dca1", "dca2"),
        )
        fill = make_fill(
            order_type=OrderType.DCA,
            status=OrderStatus.FILLED,
            filled_qty=0.05,
            order_id="dca1",
        )

        new_state = handler.handle_dca_fill(state, fill)

        assert "dca1" not in new_state.active_dca_order_ids
        assert "dca2" in new_state.active_dca_order_ids

    def test_partial_dca_cancels_remainder(self):
        """Частичное DCA → остаток ордера отменяется."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=0.1, quote_spent=5000.0, dca_ids=("dca1",))
        fill = make_fill(
            order_type=OrderType.DCA,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.03,
            remaining_qty=0.07,
            order_id="dca1",
        )

        handler.handle_dca_fill(state, fill)

        om.cancel_order.assert_called_once_with("dca1", order_role="DCA")

    def test_dca_avg_price_calculation(self):
        """avg_price = quote_spent / position_qty — математически верно."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=1.0, avg=50000.0, quote_spent=50000.0)
        fill = make_fill(
            order_type=OrderType.DCA,
            status=OrderStatus.FILLED,
            filled_qty=1.0,
            avg_fill_price=48000.0,
            commission=0.0,   # без комиссии для чистой математики
        )

        new_state = handler.handle_dca_fill(state, fill)

        # new_spent = 50000 + 1.0 * 48000 + 0 = 98000
        # new_qty = 1.0 + 1.0 = 2.0
        # new_avg = 98000 / 2.0 = 49000
        assert new_state.position_qty == Decimal("2.0")
        assert new_state.quote_spent == Decimal("98000")
        assert new_state.position_avg_price == Decimal("49000")

    def test_dca_emits_order_filled(self):
        """DCA исполнение → ORDER_FILLED эмитируется."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=0.1, quote_spent=5000.0)
        fill = make_fill(OrderType.DCA, status=OrderStatus.FILLED)

        handler.handle_dca_fill(state, fill)

        em.emit.assert_called()


# ---------------------------------------------------------------------------
# TP fill tests
# ---------------------------------------------------------------------------

class TestTpFill:

    def test_full_tp_fill_closes_position(self):
        """Полное TP → position_qty уменьшен, should_close=True."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=0.1, avg=50000.0, quote_spent=5000.0)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.FILLED,
            filled_qty=0.1,
            remaining_qty=0.0,
            avg_fill_price=52000.0,
            commission=5.2,
        )

        new_state, should_close = handler.handle_tp_fill(state, fill)

        assert should_close is True
        assert new_state.position_qty == Decimal("0")
        # quote_received = filled_qty * avg_price - commission = 0.1 * 52000 - 5.2 = 5194.8
        expected = Decimal("0.1") * Decimal("52000.0") - Decimal("5.2")
        assert new_state.quote_received == expected

    def test_partial_tp_above_threshold_triggers_close(self):
        """Частичное TP >= 80% → should_close=True, FSM → CLOSING."""
        handler, sm, om, em = make_handler(tp_partial_close_threshold_pct=80.0)
        state = make_position_state(qty=0.1)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.085,
            remaining_qty=0.015,   # 85% >= 80%
        )

        new_state, should_close = handler.handle_tp_fill(state, fill)

        assert should_close is True
        # Остаток позиции сохранён — position_qty уменьшен только на filled
        assert new_state.position_qty == Decimal("0.1") - Decimal("0.085")

    def test_partial_tp_below_threshold_stays_in_position(self):
        """Частичное TP < 80% → should_close=False, IN_POSITION продолжается."""
        handler, sm, om, em = make_handler(tp_partial_close_threshold_pct=80.0)
        state = make_position_state(qty=0.1)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.05,
            remaining_qty=0.05,   # 50% < 80%
        )

        new_state, should_close = handler.handle_tp_fill(state, fill)

        assert should_close is False
        assert new_state.position_qty == Decimal("0.05")   # уменьшен на filled

    def test_tp_position_qty_never_negative(self):
        """Если filled_qty > position_qty (граничный случай) → qty = 0, не отрицательное."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=0.05)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.FILLED,
            filled_qty=0.1,   # больше position_qty
            remaining_qty=0.0,
        )

        new_state, should_close = handler.handle_tp_fill(state, fill)

        assert new_state.position_qty == Decimal("0")
        assert new_state.position_qty >= Decimal("0")

    def test_tp_exact_threshold_triggers_close(self):
        """Ровно 80% = порог → should_close=True."""
        handler, sm, om, em = make_handler(tp_partial_close_threshold_pct=80.0)
        state = make_position_state(qty=0.1)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.08,
            remaining_qty=0.02,   # ровно 80%
        )

        _, should_close = handler.handle_tp_fill(state, fill)
        assert should_close is True

    def test_tp_one_below_threshold_no_close(self):
        """79.9% < 80% → should_close=False."""
        handler, sm, om, em = make_handler(tp_partial_close_threshold_pct=80.0)
        state = make_position_state(qty=1.0)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.799,
            remaining_qty=0.201,   # 79.9%
        )

        _, should_close = handler.handle_tp_fill(state, fill)
        assert should_close is False

    def test_tp_partial_fill_emits_tp_partially_filled(self):
        """Частичное TP → TP_PARTIALLY_FILLED эмитируется."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=0.1)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=0.05,
            remaining_qty=0.05,
        )

        handler.handle_tp_fill(state, fill)

        event_types = [
            str(c.kwargs.get("event_type", ""))
            for c in em.emit.call_args_list
        ]
        assert any("TP_PARTIALLY_FILLED" in et for et in event_types)

    def test_tp_full_fill_emits_trade_applied(self):
        """Полное TP → эмитируется TRADE_APPLIED."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=0.1)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.FILLED,
        )

        handler.handle_tp_fill(state, fill)

        event_types = [
            c.kwargs.get("event_type") or (c.args[0] if c.args else None)
            for c in em.emit.call_args_list
            if c.args or c.kwargs
        ]
        assert any("TRADE_APPLIED" in str(et) for et in event_types if et is not None)

    def test_no_fill_status_returns_no_change(self):
        """CANCELLED fill → состояние не изменяется, should_close=False."""
        handler, sm, om, em = make_handler()
        state = make_position_state(qty=0.1)
        fill = make_fill(
            order_type=OrderType.TP,
            status=OrderStatus.CANCELLED,
            filled_qty=0.0,
            remaining_qty=0.1,
        )

        new_state, should_close = handler.handle_tp_fill(state, fill)

        assert should_close is False
        # state_manager.commit не должен вызываться
        sm.commit.assert_not_called()
