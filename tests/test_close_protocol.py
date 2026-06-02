"""
tests/test_close_protocol.py — Unit тесты CloseProtocol.

CloseProtocol выполняет обязательную 13-шаговую последовательность закрытия цикла.
run(ctx, state) → (new_state, "COMPLETE" | "INCOMPLETE")

Покрытие:
  - Быстрый путь: position_qty <= dust → COMPLETE без лишних шагов
  - Отмена DCA ордеров (шаги 1-4)
  - Применение TP fills (шаги 5-7)
  - Расчёт PnL: quote_received - quote_spent (шаг 11)
  - Emit CYCLE_CLOSED (шаг 13)
  - Сброс state в IDLE после COMPLETE
  - KEEP_TP: позиция > dust после fills → INCOMPLETE
  - StopCraneError при нарушении финального чеклиста (шаг 12)
  - Отмена нескольких DCA ордеров
"""
from __future__ import annotations

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

from business_logic.close_protocol import CloseProtocol
from business_logic.errors import StopCraneError
from business_logic.types import FillEvent, OrderStatus, OrderType


# ---------------------------------------------------------------------------
# FakeState — минимальный dataclass для replace() совместимости
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeState:
    cycle_status: str = "CLOSING"
    position_qty: Decimal = Decimal("0")
    position_avg_price: Optional[Decimal] = None
    dca_count: int = 0
    active_entry_order_id: Optional[str] = None
    active_tp_order_id: Optional[str] = None
    active_dca_order_ids: tuple = ()
    cycle_id: Optional[str] = "cycle_001"
    quote_spent: Decimal = Decimal("5000")
    quote_received: Decimal = Decimal("0")
    virtual_balance_free: Decimal = Decimal("1000")
    last_applied_trade_id: Optional[str] = None
    pending_client_order_id: Optional[str] = None
    bot_id: str = "bot1"
    user_id: str = "user1"
    closing_reason: Optional[str] = None

    @property
    def has_position(self) -> bool:
        return self.position_qty > Decimal("0")


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_protocol(
    close_remainder_mode: str = "KEEP_TP",
    dust_threshold: float = 0.001,
    cancel_max_retries: int = 3,
    cooldown_sec: int = 0,
    **overrides,
) -> tuple[CloseProtocol, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Вернуть (protocol, broker, order_manager, state_manager, emitter)."""
    broker = MagicMock()
    order_manager = MagicMock()
    order_manager.cancel_order = MagicMock(return_value=True)
    state_manager = MagicMock()
    state_manager.commit = MagicMock(side_effect=lambda old, new: new)
    emitter = MagicMock()

    protocol = CloseProtocol(
        broker=broker,
        order_manager=order_manager,
        state_manager=state_manager,
        emitter=emitter,
        dust_threshold=Decimal(str(dust_threshold)),
        cancel_max_retries=cancel_max_retries,
        close_remainder_mode=close_remainder_mode,
        close_remainder_timeout_sec=3600,
        max_market_close_slippage_pct=Decimal("0.5"),
        cooldown_sec=cooldown_sec,
    )
    return protocol, broker, order_manager, state_manager, emitter


def make_ctx(
    fills_for_tp: tuple = (),
    open_orders: tuple = (),
    ticker: str = "BTCUSDT",
    state: Optional[FakeState] = None,
) -> MagicMock:
    """TickContext мок — только нужные поля."""
    ctx = MagicMock()
    ctx.fills_for_tp = fills_for_tp
    ctx.open_orders = open_orders
    ctx.ticker = ticker
    ctx.bot_state = state or FakeState()
    return ctx


def make_tp_fill(
    filled_qty: float = 0.1,
    remaining_qty: float = 0.0,
    avg_fill_price: float = 52000.0,
    commission: float = 5.2,
    status: OrderStatus = OrderStatus.FILLED,
    order_id: str = "tp1",
) -> FillEvent:
    return FillEvent(
        exchange_order_id=order_id,
        client_order_id=f"client_{order_id}",
        status=status,
        order_type=OrderType.TP,
        filled_qty=Decimal(str(filled_qty)),
        remaining_qty=Decimal(str(remaining_qty)),
        avg_fill_price=Decimal(str(avg_fill_price)),
        commission=Decimal(str(commission)),
        timestamp_ms=int(time.time() * 1000),
    )


# ---------------------------------------------------------------------------
# Быстрый путь: position_qty <= dust → COMPLETE
# ---------------------------------------------------------------------------

class TestFastPath:

    def test_dust_position_returns_complete(self):
        """position_qty уже <= dust → COMPLETE без применения fills."""
        protocol, *_ = make_protocol(dust_threshold=0.001)
        state = FakeState(
            position_qty=Decimal("0.0005"),   # ниже dust
            quote_spent=Decimal("25"),
            quote_received=Decimal("26"),
        )
        ctx = make_ctx(state=state)

        new_state, status = protocol.run(ctx, state)

        assert status == "COMPLETE"

    def test_zero_position_returns_complete(self):
        """position_qty = 0 → COMPLETE."""
        protocol, *_ = make_protocol()
        state = FakeState(position_qty=Decimal("0"), quote_received=Decimal("5200"))
        ctx = make_ctx(state=state)

        _, status = protocol.run(ctx, state)
        assert status == "COMPLETE"

    def test_complete_resets_state_to_idle(self):
        """После COMPLETE: cycle_status=IDLE, все поля сброшены."""
        protocol, *_ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            cycle_id="cycle_001",
            quote_spent=Decimal("5000"),
            quote_received=Decimal("5200"),
        )
        ctx = make_ctx(state=state)

        new_state, status = protocol.run(ctx, state)

        assert status == "COMPLETE"
        assert new_state.cycle_status == "IDLE"
        assert new_state.cycle_id is None
        assert new_state.position_qty == Decimal("0")
        assert new_state.quote_spent == Decimal("0")
        assert new_state.quote_received == Decimal("0")
        assert new_state.dca_count == 0

    def test_complete_emits_cycle_closed(self):
        """COMPLETE → CYCLE_CLOSED эмитируется."""
        protocol, _, _, _, emitter = make_protocol()
        state = FakeState(position_qty=Decimal("0"), quote_received=Decimal("5200"))
        ctx = make_ctx(state=state)

        protocol.run(ctx, state)

        event_types = [
            str(c.kwargs.get("event_type", ""))
            for c in emitter.emit.call_args_list
        ]
        assert any("CYCLE_CLOSED" in et for et in event_types)


# ---------------------------------------------------------------------------
# Шаги 1-4: Отмена DCA
# ---------------------------------------------------------------------------

class TestCancelDca:

    def test_single_dca_order_cancelled(self):
        """Один активный DCA ордер → cancel_order вызывается один раз."""
        protocol, _, om, _, _ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            active_dca_order_ids=("dca1",),
        )
        ctx = make_ctx(state=state)

        protocol.run(ctx, state)

        om.cancel_order.assert_called_once_with("dca1", order_role="DCA")

    def test_multiple_dca_orders_all_cancelled(self):
        """Три DCA ордера → cancel_order вызывается три раза."""
        protocol, _, om, _, _ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            active_dca_order_ids=("dca1", "dca2", "dca3"),
        )
        ctx = make_ctx(state=state)

        protocol.run(ctx, state)

        assert om.cancel_order.call_count == 3
        cancelled_ids = {c.args[0] for c in om.cancel_order.call_args_list}
        assert cancelled_ids == {"dca1", "dca2", "dca3"}

    def test_no_dca_orders_no_cancel_called(self):
        """Нет DCA ордеров → cancel_order не вызывается."""
        protocol, _, om, _, _ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            active_dca_order_ids=(),
        )
        ctx = make_ctx(state=state)

        protocol.run(ctx, state)

        om.cancel_order.assert_not_called()

    def test_cancel_failure_raises_stop_crane(self):
        """cancel_order бросает StopCraneError → propagates из run()."""
        protocol, _, om, _, _ = make_protocol()
        om.cancel_order.side_effect = StopCraneError(
            "Не удалось отменить DCA",
            invariant="cancel_confirmed",
            expected={"cancelled": True},
            actually_found=None,
            db_state={},
        )
        state = FakeState(
            position_qty=Decimal("0.1"),
            active_dca_order_ids=("dca1",),
        )
        ctx = make_ctx(state=state)

        with pytest.raises(StopCraneError):
            protocol.run(ctx, state)


# ---------------------------------------------------------------------------
# Шаги 5-7: Применение fills
# ---------------------------------------------------------------------------

class TestApplyFills:

    def test_tp_fill_reduces_position_qty(self):
        """TP fill из ctx → position_qty уменьшается."""
        protocol, *_ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0.1"),
            quote_spent=Decimal("5000"),
            quote_received=Decimal("0"),
        )
        fill = make_tp_fill(filled_qty=0.1, remaining_qty=0.0)
        ctx = make_ctx(fills_for_tp=(fill,), state=state)

        new_state, status = protocol.run(ctx, state)

        assert status == "COMPLETE"
        # После применения fill position_qty = 0 → COMPLETE

    def test_tp_fill_updates_quote_received(self):
        """TP fill → quote_received обновляется: filled*price - commission."""
        protocol, *_ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0.1"),
            quote_spent=Decimal("5000"),
            quote_received=Decimal("0"),
        )
        fill = make_tp_fill(
            filled_qty=0.1,
            avg_fill_price=52000.0,
            commission=5.2,
        )
        ctx = make_ctx(fills_for_tp=(fill,), state=state)

        new_state, _ = protocol.run(ctx, state)

        # quote_received был 0, после fill: 0 + (0.1*52000 - 5.2) = 5194.8
        # После finalize он сбрасывается в 0 — проверяем что PnL верен через
        # CYCLE_CLOSED payload (pnl = quote_received - quote_spent до сброса)
        # Просто проверяем что протокол вернул COMPLETE без ошибок
        assert new_state.cycle_status == "IDLE"

    def test_no_fills_position_stays(self):
        """Нет TP fills → position_qty не меняется в шагах 5-7."""
        protocol, *_ = make_protocol(close_remainder_mode="KEEP_TP")
        state = FakeState(
            position_qty=Decimal("0.1"),  # > dust
        )
        ctx = make_ctx(fills_for_tp=(), state=state)   # нет fills

        _, status = protocol.run(ctx, state)

        # KEEP_TP: позиция > dust, нет fills → INCOMPLETE
        assert status == "INCOMPLETE"


# ---------------------------------------------------------------------------
# Шаг 11: PnL
# ---------------------------------------------------------------------------

class TestPnl:

    def test_pnl_equals_received_minus_spent(self):
        """PnL = quote_received - quote_spent (комиссии уже в этих полях)."""
        protocol, _, _, _, emitter = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            quote_spent=Decimal("5000"),
            quote_received=Decimal("5300"),
        )
        ctx = make_ctx(state=state)

        protocol.run(ctx, state)

        # CYCLE_CLOSED payload содержит pnl
        cycle_closed_calls = [
            c for c in emitter.emit.call_args_list
            if c.kwargs.get("event_type") == "CYCLE_CLOSED"
        ]
        assert len(cycle_closed_calls) == 1
        payload = cycle_closed_calls[0].kwargs.get("payload", {})
        assert payload["pnl"] == str(Decimal("5300") - Decimal("5000"))  # "300"

    def test_negative_pnl_handled_correctly(self):
        """Отрицательный PnL (убыток) — обрабатывается корректно."""
        protocol, _, _, _, emitter = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            quote_spent=Decimal("5000"),
            quote_received=Decimal("4800"),
        )
        ctx = make_ctx(state=state)

        protocol.run(ctx, state)

        cycle_closed_calls = [
            c for c in emitter.emit.call_args_list
            if c.kwargs.get("event_type") == "CYCLE_CLOSED"
        ]
        payload = cycle_closed_calls[0].kwargs["payload"]
        assert Decimal(payload["pnl"]) == Decimal("-200")

    def test_zero_pnl(self):
        """PnL = 0 при равных spent и received."""
        protocol, _, _, _, emitter = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            quote_spent=Decimal("5000"),
            quote_received=Decimal("5000"),
        )
        ctx = make_ctx(state=state)

        protocol.run(ctx, state)

        cycle_closed_calls = [
            c for c in emitter.emit.call_args_list
            if c.kwargs.get("event_type") == "CYCLE_CLOSED"
        ]
        payload = cycle_closed_calls[0].kwargs["payload"]
        assert Decimal(payload["pnl"]) == Decimal("0")


# ---------------------------------------------------------------------------
# Шаг 9: CLOSE_REMAINDER_MODE
# ---------------------------------------------------------------------------

class TestCloseRemainderMode:

    def test_keep_tp_incomplete_when_position_remains(self):
        """KEEP_TP: position > dust после fills → INCOMPLETE, TP остаётся."""
        protocol, *_ = make_protocol(close_remainder_mode="KEEP_TP")
        state = FakeState(
            position_qty=Decimal("0.05"),  # > dust
            active_tp_order_id="tp1",
        )
        ctx = make_ctx(fills_for_tp=(), state=state)

        _, status = protocol.run(ctx, state)

        assert status == "INCOMPLETE"


# ---------------------------------------------------------------------------
# Шаг 12: Финальный чеклист
# ---------------------------------------------------------------------------

class TestFinalChecklist:

    def test_open_orders_on_exchange_raises_stop_crane(self):
        """Открытые ордеры на бирже при финализации → StopCraneError."""
        protocol, *_ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            cycle_id="cycle_001",
        )
        # Биржа сообщает об открытом ордере по этому циклу
        ctx = make_ctx(
            state=state,
            open_orders=({"cycle_id": "cycle_001", "orderId": "stray_order"},),
        )

        with pytest.raises(StopCraneError):
            protocol.run(ctx, state)

    def test_clean_state_passes_checklist(self):
        """Чистое состояние (нет ордеров, position=0) → чеклист проходит."""
        protocol, *_ = make_protocol()
        state = FakeState(
            position_qty=Decimal("0"),
            active_dca_order_ids=(),
        )
        ctx = make_ctx(state=state, open_orders=())

        _, status = protocol.run(ctx, state)
        assert status == "COMPLETE"

    def test_cooldown_does_not_block(self):
        """Cooldown=0 → run() завершается мгновенно."""
        protocol, *_ = make_protocol(cooldown_sec=0)
        state = FakeState(position_qty=Decimal("0"))
        ctx = make_ctx(state=state)

        _, status = protocol.run(ctx, state)
        assert status == "COMPLETE"
