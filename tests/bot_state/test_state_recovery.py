"""
tests/bot_state/test_state_recovery.py

Unit-тесты для StateRecovery._reconcile().

Архитектура:
  - DB-слой мокается через tests/conftest.py (state_manager, state_repo).
  - IBroker мокается через MagicMock / FakeBroker per-test.
  - Каждый тест создаёт StateRecovery напрямую через __init__ (обходим
    startup-шаги heartbeat / registry, которые тестируются отдельно).
  - state_manager.transition() / update() мокаются через возврат нового
    BotState с правильным cycle_status — это позволяет проверять FSM
    без реального PostgreSQL.
"""
from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch
from dataclasses import replace

from bot_state.models import BotState, CycleStatus
from bot_state.state_recovery import StateRecovery, ReconciliationError


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

TICKER = "BTCUSDT"
USER   = "test_user"
BOT    = "test_bot"


def make_state(**overrides) -> BotState:
    """BotState с разумными дефолтами."""
    defaults = dict(
        user_id=USER,
        bot_id=BOT,
        version=1,
        cycle_status=CycleStatus.IDLE,
        virtual_balance_free=Decimal("1000"),
        virtual_balance_locked=Decimal("0"),
        position_qty=Decimal("0"),
        quote_spent=Decimal("0"),
        quote_received=Decimal("0"),
        active_dca_order_ids=(),
    )
    defaults.update(overrides)
    return BotState(**defaults)


def make_open_order(exchange_order_id="EX1", client_order_id="CL1", ticker=TICKER):
    """Минимальный объект OpenOrder (достаточно атрибутов для reconciliation)."""
    o = MagicMock()
    o.exchange_order_id = exchange_order_id
    o.client_order_id   = client_order_id
    o.ticker            = ticker
    return o


def make_historical_fill(
    trade_id="T1",
    exchange_order_id="EX1",
    client_order_id="CL1",
    filled_qty=Decimal("0.1"),
    avg_price=Decimal("50000"),
    commission=Decimal("0"),
    timestamp=1_000_000.0,
):
    """Минимальный объект HistoricalFill."""
    f = MagicMock()
    f.trade_id          = trade_id
    f.exchange_order_id = exchange_order_id
    f.client_order_id   = client_order_id
    f.filled_qty        = filled_qty
    f.avg_price         = avg_price
    f.commission        = commission
    f.timestamp         = timestamp
    return f


def make_broker(open_orders=None, fills=None, min_qty=Decimal("0.001")):
    """
    Брокер-мок: get_open_orders / get_fills / get_market_info настраиваются.
    """
    broker = MagicMock()
    broker.get_open_orders.return_value = open_orders or []
    broker.get_fills.return_value       = fills or []
    market_info = MagicMock()
    market_info.min_qty = min_qty
    broker.get_market_info.return_value = market_info
    return broker


def make_recovery(state_manager=None, state_repo=None):
    """Создать StateRecovery с мок-зависимостями."""
    if state_manager is None:
        state_manager = MagicMock()
        # update() / transition() по умолчанию возвращают то же состояние
        state_manager.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        state_manager.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
    if state_repo is None:
        state_repo = MagicMock()

    return StateRecovery(
        user_id=USER,
        bot_id=BOT,
        ticker=TICKER,
        state_repo=state_repo,
        state_manager=state_manager,
        registry_repo=MagicMock(),
        emitter=None,
        virtual_balance=None,
    )


# ---------------------------------------------------------------------------
# _reconcile: trivial cases
# ---------------------------------------------------------------------------

class TestReconcileTrivial:
    def test_idle_returns_unchanged(self):
        rec = make_recovery()
        state = make_state(cycle_status=CycleStatus.IDLE)
        broker = make_broker()
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IDLE
        broker.get_open_orders.assert_not_called()
        broker.get_fills.assert_not_called()

    def test_stop_crane_returns_unchanged(self):
        rec = make_recovery()
        state = make_state(cycle_status=CycleStatus.STOP_CRANE)
        broker = make_broker()
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.STOP_CRANE
        broker.get_open_orders.assert_not_called()

    def test_idle_no_broker_calls(self):
        rec = make_recovery()
        state = make_state(cycle_status=CycleStatus.IDLE)
        broker = make_broker()
        rec._reconcile(state, broker)
        broker.get_open_orders.assert_not_called()
        broker.get_fills.assert_not_called()


# ---------------------------------------------------------------------------
# _reconcile: ENTERING — order still on exchange
# ---------------------------------------------------------------------------

class TestReconcileEnteringOrderStillPending:
    def test_order_in_open_orders_no_change(self):
        sm = MagicMock()
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        broker = make_broker(open_orders=[make_open_order(exchange_order_id="EX1")])
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.ENTERING
        sm.transition.assert_not_called()
        sm.update.assert_not_called()

    def test_order_in_open_orders_by_correct_ticker(self):
        rec = make_recovery()
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX_MY",
        )
        broker = make_broker(
            open_orders=[make_open_order(exchange_order_id="EX_MY")]
        )
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.ENTERING


# ---------------------------------------------------------------------------
# _reconcile: ENTERING — order cancelled (not on exchange, not in fills)
# ---------------------------------------------------------------------------

class TestReconcileEnteringOrderCancelled:
    def test_cancelled_entry_transitions_idle(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        broker = make_broker(open_orders=[], fills=[])
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IDLE
        sm.transition.assert_called_once()
        call_args = sm.transition.call_args
        assert call_args[0][1] == CycleStatus.IDLE

    def test_cancelled_clears_entry_order_id(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        broker = make_broker()
        result = rec._reconcile(state, broker)
        assert result.active_entry_order_id is None

    def test_cancelled_clears_pending_client_order_id(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
            pending_client_order_id="CL1",
        )
        broker = make_broker()
        result = rec._reconcile(state, broker)
        assert result.pending_client_order_id is None


# ---------------------------------------------------------------------------
# _reconcile: ENTERING — order filled while bot was down
# ---------------------------------------------------------------------------

class TestReconcileEnteringOrderFilled:
    def test_fill_found_transitions_in_position(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        fill = make_historical_fill(
            exchange_order_id="EX1",
            filled_qty=Decimal("0.1"),
            avg_price=Decimal("50000"),
        )
        broker = make_broker(open_orders=[], fills=[fill])
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IN_POSITION

    def test_fill_updates_position_qty(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
            position_qty=Decimal("0"),
        )
        fill = make_historical_fill(
            exchange_order_id="EX1",
            filled_qty=Decimal("0.1"),
            avg_price=Decimal("50000"),
        )
        broker = make_broker(fills=[fill])
        result = rec._reconcile(state, broker)
        assert result.position_qty == Decimal("0.1")

    def test_fill_updates_avg_price(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        fill = make_historical_fill(
            exchange_order_id="EX1",
            filled_qty=Decimal("0.1"),
            avg_price=Decimal("50000"),
        )
        broker = make_broker(fills=[fill])
        result = rec._reconcile(state, broker)
        assert result.position_avg_price == Decimal("50000")

    def test_fill_updates_quote_spent(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        fill = make_historical_fill(
            exchange_order_id="EX1",
            filled_qty=Decimal("0.1"),
            avg_price=Decimal("50000"),
            commission=Decimal("5"),
        )
        broker = make_broker(fills=[fill])
        result = rec._reconcile(state, broker)
        # 0.1 * 50000 + 5 commission
        assert result.quote_spent == Decimal("5005")

    def test_fill_clears_active_entry_order_id(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        fill = make_historical_fill(exchange_order_id="EX1")
        broker = make_broker(fills=[fill])
        result = rec._reconcile(state, broker)
        assert result.active_entry_order_id is None

    def test_fill_sets_last_applied_trade_id(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        fill = make_historical_fill(exchange_order_id="EX1", trade_id="TRADE_99")
        broker = make_broker(fills=[fill])
        result = rec._reconcile(state, broker)
        assert result.last_applied_trade_id == "TRADE_99"


# ---------------------------------------------------------------------------
# _reconcile: ENTERING — pending_client_order_id (crash during send)
# ---------------------------------------------------------------------------

class TestReconcileEnteringPendingSend:
    def test_pending_found_in_open_orders_recovers_exchange_id(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            pending_client_order_id="CL1",
            active_entry_order_id=None,
        )
        open_order = make_open_order(exchange_order_id="EX_NEW", client_order_id="CL1")
        broker = make_broker(open_orders=[open_order])
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.ENTERING
        assert result.active_entry_order_id == "EX_NEW"
        assert result.pending_client_order_id is None

    def test_pending_found_in_fills_transitions_in_position(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            pending_client_order_id="CL1",
            active_entry_order_id=None,
        )
        fill = make_historical_fill(
            client_order_id="CL1",
            exchange_order_id="EX_FILLED",
            filled_qty=Decimal("0.1"),
        )
        broker = make_broker(fills=[fill])
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IN_POSITION

    def test_pending_not_found_anywhere_goto_stop_crane(self):
        sm = MagicMock()
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            pending_client_order_id="CL_LOST",
            active_entry_order_id=None,
        )
        broker = make_broker(open_orders=[], fills=[])
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.STOP_CRANE


# ---------------------------------------------------------------------------
# _reconcile: IN_POSITION — no fills, all orders present
# ---------------------------------------------------------------------------

class TestReconcileInPosition:
    def test_all_orders_present_no_changes(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("0.5"),
            active_tp_order_id="TP1",
            active_dca_order_ids=("DCA1",),
        )
        broker = make_broker(
            open_orders=[
                make_open_order(exchange_order_id="TP1"),
                make_open_order(exchange_order_id="DCA1"),
            ],
        )
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IN_POSITION
        assert result.active_tp_order_id == "TP1"
        assert "DCA1" in result.active_dca_order_ids

    def test_missing_tp_no_fill_clears_tp_id(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("0.5"),
            active_tp_order_id="TP1",
        )
        broker = make_broker(open_orders=[], fills=[])
        result = rec._reconcile(state, broker)
        assert result.active_tp_order_id is None
        assert result.cycle_status == CycleStatus.IN_POSITION

    def test_missing_dca_no_fill_removed_from_list(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("0.5"),
            active_dca_order_ids=("DCA1", "DCA2"),
        )
        # only DCA2 survives
        broker = make_broker(
            open_orders=[make_open_order(exchange_order_id="DCA2")],
        )
        result = rec._reconcile(state, broker)
        assert "DCA1" not in result.active_dca_order_ids
        assert "DCA2" in result.active_dca_order_ids

    def test_dca_fill_applied_to_position(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("0.5"),
            position_avg_price=Decimal("40000"),
            active_dca_order_ids=("DCA1",),
        )
        fill = make_historical_fill(
            trade_id="T_DCA",
            exchange_order_id="DCA1",
            filled_qty=Decimal("0.5"),
            avg_price=Decimal("30000"),
        )
        # DCA1 is NOT in open_orders — it filled
        broker = make_broker(fills=[fill])
        result = rec._reconcile(state, broker)
        # position_qty: 0.5 + 0.5 = 1.0
        assert result.position_qty == Decimal("1.0")
        # avg_price: (0.5*40000 + 0.5*30000) / 1.0 = 35000
        assert result.position_avg_price == Decimal("35000")

    def test_tp_fill_reduces_position_qty(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("1.0"),
            active_tp_order_id="TP1",
        )
        tp_fill = make_historical_fill(
            trade_id="T_TP",
            exchange_order_id="TP1",
            filled_qty=Decimal("0.3"),
            avg_price=Decimal("55000"),
        )
        broker = make_broker(fills=[tp_fill])
        result = rec._reconcile(state, broker)
        assert result.position_qty == Decimal("0.7")

    def test_tp_fill_fully_closes_position_transitions_closing(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("0.1"),
            active_tp_order_id="TP1",
        )
        tp_fill = make_historical_fill(
            exchange_order_id="TP1",
            filled_qty=Decimal("0.1"),  # exactly closes position
            avg_price=Decimal("55000"),
        )
        broker = make_broker(
            fills=[tp_fill],
            min_qty=Decimal("0.001"),
        )
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.CLOSING


# ---------------------------------------------------------------------------
# _reconcile: CLOSING
# ---------------------------------------------------------------------------

class TestReconcileClosing:
    def test_position_still_open_stays_closing(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.CLOSING,
            position_qty=Decimal("0.5"),
            active_tp_order_id="TP1",
        )
        broker = make_broker(
            open_orders=[make_open_order(exchange_order_id="TP1")],
        )
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.CLOSING

    def test_position_zero_transitions_idle(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.CLOSING,
            position_qty=Decimal("0"),
        )
        broker = make_broker(min_qty=Decimal("0.001"))
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IDLE

    def test_closing_fill_brings_position_to_zero_then_idle(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.CLOSING,
            position_qty=Decimal("0.2"),
            active_tp_order_id="TP1",
        )
        fill = make_historical_fill(
            exchange_order_id="TP1",
            filled_qty=Decimal("0.2"),
        )
        broker = make_broker(fills=[fill], min_qty=Decimal("0.001"))
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IDLE

    def test_closing_missing_tp_no_fill_clears_id(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.CLOSING,
            position_qty=Decimal("0.5"),
            active_tp_order_id="TP_GONE",
        )
        broker = make_broker(open_orders=[], fills=[])
        result = rec._reconcile(state, broker)
        assert result.active_tp_order_id is None
        assert result.cycle_status == CycleStatus.CLOSING


# ---------------------------------------------------------------------------
# _reconcile: WAITING_FOR_LIQUIDITY
# ---------------------------------------------------------------------------

class TestReconcileWaitingForLiquidity:
    def test_restores_in_position(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.WAITING_FOR_LIQUIDITY,
            position_qty=Decimal("0.5"),
        )
        broker = make_broker(min_qty=Decimal("0.001"))
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IN_POSITION

    def test_if_position_gone_transitions_closing(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.WAITING_FOR_LIQUIDITY,
            position_qty=Decimal("0"),
        )
        broker = make_broker(min_qty=Decimal("0.001"))
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.CLOSING

    def test_stale_tp_order_cleared(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.WAITING_FOR_LIQUIDITY,
            position_qty=Decimal("0.5"),
            active_tp_order_id="TP_GONE",
        )
        broker = make_broker(open_orders=[], min_qty=Decimal("0.001"))
        result = rec._reconcile(state, broker)
        assert result.active_tp_order_id is None


# ---------------------------------------------------------------------------
# _reconcile: broker failures are non-fatal
# ---------------------------------------------------------------------------

class TestReconcileBrokerFailures:
    def test_get_open_orders_failure_falls_back_gracefully(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.ENTERING,
            active_entry_order_id="EX1",
        )
        broker = MagicMock()
        broker.get_open_orders.side_effect = RuntimeError("network error")
        broker.get_fills.return_value = []
        market_info = MagicMock()
        market_info.min_qty = Decimal("0.001")
        broker.get_market_info.return_value = market_info

        # Should not raise; ENTERING → IDLE (no order found, no fill)
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IDLE

    def test_get_fills_failure_falls_back_gracefully(self):
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        rec = make_recovery(state_manager=sm)
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("0.5"),
        )
        broker = MagicMock()
        broker.get_open_orders.return_value = []
        broker.get_fills.side_effect = RuntimeError("timeout")
        market_info = MagicMock()
        market_info.min_qty = Decimal("0.001")
        broker.get_market_info.return_value = market_info

        # Should not raise; IN_POSITION stays (no fills to apply)
        result = rec._reconcile(state, broker)
        assert result.cycle_status == CycleStatus.IN_POSITION


# ---------------------------------------------------------------------------
# _apply_position_fills — unit tests
# ---------------------------------------------------------------------------

class TestApplyPositionFills:
    def _make_rec_with_real_update(self):
        """Recovery where update() actually applies with_updates."""
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        return make_recovery(state_manager=sm)

    def test_no_fills_returns_unchanged_state(self):
        rec = self._make_rec_with_real_update()
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("1.0"),
        )
        new_state, filled_dca = rec._apply_position_fills(state, [])
        assert new_state is state  # same object, no DB write
        assert filled_dca == set()

    def test_dca_fill_adds_to_position(self):
        rec = self._make_rec_with_real_update()
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("1.0"),
            position_avg_price=Decimal("40000"),
            active_dca_order_ids=("DCA1",),
        )
        fill = make_historical_fill(
            exchange_order_id="DCA1",
            filled_qty=Decimal("1.0"),
            avg_price=Decimal("30000"),
        )
        new_state, filled_dca = rec._apply_position_fills(state, [fill])
        assert new_state.position_qty == Decimal("2.0")
        assert new_state.position_avg_price == Decimal("35000")
        assert "DCA1" in filled_dca

    def test_tp_fill_reduces_position(self):
        rec = self._make_rec_with_real_update()
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("1.0"),
            active_tp_order_id="TP1",
        )
        fill = make_historical_fill(
            exchange_order_id="TP1",
            filled_qty=Decimal("0.4"),
            avg_price=Decimal("60000"),
        )
        new_state, _ = rec._apply_position_fills(state, [fill])
        assert new_state.position_qty == Decimal("0.6")

    def test_multiple_fills_applied_in_timestamp_order(self):
        rec = self._make_rec_with_real_update()
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("1.0"),
            position_avg_price=Decimal("40000"),
            active_dca_order_ids=("DCA1", "DCA2"),
        )
        fill1 = make_historical_fill(
            exchange_order_id="DCA1",
            filled_qty=Decimal("1.0"),
            avg_price=Decimal("30000"),
            timestamp=1000.0,
        )
        fill2 = make_historical_fill(
            exchange_order_id="DCA2",
            trade_id="T2",
            filled_qty=Decimal("2.0"),
            avg_price=Decimal("20000"),
            timestamp=2000.0,
        )
        new_state, filled_dca = rec._apply_position_fills(state, [fill2, fill1])
        # Should be 1+1+2 = 4.0 total
        assert new_state.position_qty == Decimal("4.0")
        assert {"DCA1", "DCA2"} == filled_dca

    def test_unrecognised_fill_is_skipped(self):
        rec = self._make_rec_with_real_update()
        state = make_state(
            cycle_status=CycleStatus.IN_POSITION,
            position_qty=Decimal("1.0"),
        )
        fill = make_historical_fill(
            exchange_order_id="UNKNOWN_ORDER",
            filled_qty=Decimal("9999"),
        )
        new_state, _ = rec._apply_position_fills(state, [fill])
        assert new_state.position_qty == Decimal("1.0")  # unchanged

    def test_tp_fill_never_makes_position_qty_negative(self):
        rec = self._make_rec_with_real_update()
        state = make_state(
            cycle_status=CycleStatus.CLOSING,
            position_qty=Decimal("0.1"),
            active_tp_order_id="TP1",
        )
        fill = make_historical_fill(
            exchange_order_id="TP1",
            filled_qty=Decimal("9999"),  # grossly over-fills
        )
        new_state, _ = rec._apply_position_fills(state, [fill])
        assert new_state.position_qty == Decimal("0")  # clamped to 0


# ---------------------------------------------------------------------------
# startup() signature contract
# ---------------------------------------------------------------------------

class TestStartupSignature:
    def test_startup_accepts_ticker_parameter(self):
        """
        Убедиться что startup() принимает ticker как обязательный параметр.
        """
        # Все зависимости мокаются — нам нужно только убедиться что
        # вызов не падает на этапе создания экземпляра.
        sm = MagicMock()
        sm.update.side_effect = lambda s, **kw: s.with_updates(**kw)
        sm.transition.side_effect = (
            lambda s, to, **kw: s.with_updates(cycle_status=to, **kw)
        )
        sm.initialize.return_value = make_state()
        sm.load.return_value = make_state()

        sr = MagicMock()
        sr.load.return_value = None  # first run

        rr = MagicMock()
        rr.load.return_value = None
        rr.upsert.return_value = None

        broker = make_broker()

        # Должен принять ticker без TypeError
        state = StateRecovery.startup(
            user_id=USER,
            bot_id=BOT,
            ticker=TICKER,
            broker=broker,
            state_repo=sr,
            state_manager=sm,
            registry_repo=rr,
            emitter=None,
            virtual_balance=Decimal("1000"),
        )
        assert state is not None
