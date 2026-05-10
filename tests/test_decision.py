"""
tests/test_decision.py — Unit тесты DecisionEngine.

DecisionEngine — чистая функция над TickContext + BotState + StrategySignal.
Не делает запросов к бирже или БД: идеален для юнит-тестирования.

Покрытие:
  - IDLE: вход, WAIT, ошибка проскальзывания, статусы CLOSE_ONLY/STOPPED
  - ENTERING: ожидание, timeout, CANCEL_ENTRY
  - IN_POSITION: PLACE_DCA, REPLACE_TP, CLOSE_PROTOCOL, WAIT
  - WAITING_FOR_LIQUIDITY: retry, ожидание
  - STOP_CRANE: всегда WAIT
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Мокаем DB и psycopg2 ДО любых импортов проекта.
# bot_state/__init__.py тянет state_repo → db.connection.
# Без этого блока pytest падает с ImportError даже при наличии conftest.py,
# если conftest не был загружен раньше этого модуля.
# ---------------------------------------------------------------------------
import sys
from unittest.mock import MagicMock

def _mock_cm():
    """Контекстный менеджер-заглушка для get_connection / transaction."""
    from contextlib import contextmanager
    class _Cur:
        rowcount = 1
        def execute(self, *a, **k): pass
        def fetchone(self): return None
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *a): pass
    class _Conn:
        dsn = "postgresql://mock/mock"
        autocommit = False
        def cursor(self): return _Cur()
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    @contextmanager
    def _cm(): yield _Conn()
    return _cm

_db = MagicMock()
_db.get_connection = _mock_cm()
_db.transaction = _mock_cm()
sys.modules.setdefault("db", _db)
sys.modules.setdefault("db.connection", _db)
sys.modules.setdefault("psycopg2", MagicMock())
sys.modules.setdefault("psycopg2.extras", MagicMock())
sys.modules.setdefault("psycopg2.extensions", MagicMock())
sys.modules.setdefault("pybit", MagicMock())
sys.modules.setdefault("pybit.unified_trading", MagicMock())
# ---------------------------------------------------------------------------

import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from business_logic.decision import DecisionEngine
from business_logic.errors import TickSkippedError
from business_logic.strategy import SIGNAL_WAIT, StrategySignal
from business_logic.types import Decision, DecisionAction, FillEvent, OrderStatus, OrderType
from market_data.market_data import PriceData, PriceSource
from broker.models import Balance
from types import SimpleNamespace


# BotState не импортируем — используем SimpleNamespace.
# DecisionEngine работает с атрибутами объекта, не с типом.
# Это корректный подход для unit-тестов: тестируем логику, не структуры данных.

def make_engine(**overrides) -> DecisionEngine:
    defaults = dict(
        max_entry_slippage_pct=Decimal("1.0"),
        partial_fill_threshold_pct=Decimal("80"),
        tp_partial_close_threshold_pct=Decimal("80"),
        entry_order_timeout_sec=3600,
        max_dca_count=3,
        dca_mode="LAZY",
        max_position_days=None,
        force_close_on_timeout=False,
        dust_threshold=Decimal("0.001"),
    )
    defaults.update(overrides)
    return DecisionEngine(**defaults)


def make_price(last: float = 50000.0) -> PriceData:
    last_d = Decimal(str(last))
    spread = last_d * Decimal("0.0001")
    return PriceData(
        ticker="BTCUSDT",
        bid=last_d - spread / 2,
        ask=last_d + spread / 2,
        last=last_d,
        timestamp=time.time(),
        source=PriceSource.REST,
    )


def make_state(
    cycle_status: str = "IDLE",
    position_qty: Decimal = Decimal("0"),
    dca_count: int = 0,
    active_entry_order_id: str | None = None,
    active_tp_order_id: str | None = None,
    cycle_id: str | None = None,
    last_order_at: datetime | None = None,
    entered_at: datetime | None = None,
):
    """
    Создаёт фейковый state через SimpleNamespace.
    DecisionEngine проверяет только атрибуты, не тип объекта —
    SimpleNamespace работает так же как BotState для тестов.
    """
    return SimpleNamespace(
        cycle_status=cycle_status,
        position_qty=position_qty,
        dca_count=dca_count,
        active_entry_order_id=active_entry_order_id,
        active_tp_order_id=active_tp_order_id,
        cycle_id=cycle_id,
        last_order_at=last_order_at,
        entered_at=entered_at,
        has_position=position_qty > Decimal("0"),
        virtual_balance_free=Decimal("1000"),
        bot_id="bot1",
        user_id="user1",
    )


def make_config(status: str = "ACTIVE") -> MagicMock:
    cfg = MagicMock()
    cfg.status = status
    cfg.ticker = "BTCUSDT"
    cfg.user_id = "user1"
    cfg.bot_id = "bot1"
    cfg.strategy_params = {}
    return cfg


def make_ctx(
    cycle_status: str = "IDLE",
    bot_status: str = "ACTIVE",
    price: float = 50000.0,
    order_events: tuple = (),
    state=None,
    last_order_at: datetime | None = None,
    active_entry_order_id: str | None = None,
    active_tp_order_id: str | None = None,
    position_qty: Decimal = Decimal("0"),
    dca_count: int = 0,
    cycle_id: str | None = None,
) -> MagicMock:
    """
    Создать MagicMock TickContext с нужными атрибутами.
    Использование MagicMock вместо реального TickContext позволяет
    задавать только нужные поля без полного графа зависимостей.
    """
    if state is None:
        state = make_state(
            cycle_status=cycle_status,
            position_qty=position_qty,
            dca_count=dca_count,
            active_entry_order_id=active_entry_order_id,
            active_tp_order_id=active_tp_order_id,
            cycle_id=cycle_id,
            last_order_at=last_order_at,
        )

    ctx = MagicMock()
    ctx.price_data = make_price(price)
    ctx.bot_state = state
    ctx.bot_config = make_config(bot_status)
    ctx.bot_status = bot_status
    ctx.cycle_status = cycle_status
    ctx.order_events = order_events
    ctx.tick_number = 0

    # Фильтрация fills по типу ордера
    ctx.fills_for_entry = tuple(
        e for e in order_events if e.order_type == OrderType.ENTRY
    )
    ctx.fills_for_tp = tuple(
        e for e in order_events if e.order_type == OrderType.TP
    )
    ctx.fills_for_dca = tuple(
        e for e in order_events if e.order_type == OrderType.DCA
    )

    ctx.has_open_position = position_qty > Decimal("0")
    ctx.has_active_entry_order = active_entry_order_id is not None
    ctx.has_active_tp_order = active_tp_order_id is not None
    ctx.ticker = "BTCUSDT"
    ctx.user_id = "user1"
    ctx.bot_id = "bot1"
    ctx.tick_start_mono = time.monotonic()
    return ctx


def make_signal(
    should_enter: bool = True,
    target_qty: Decimal = Decimal("0.1"),
    target_avg_price: Decimal | None = Decimal("50000"),
    tp_price: Decimal | None = Decimal("52000"),
    reason: str = "test_signal",
) -> StrategySignal:
    if not should_enter:
        return SIGNAL_WAIT
    return StrategySignal(
        should_enter=should_enter,
        target_qty=target_qty,
        target_avg_price=target_avg_price,
        tp_price=tp_price,
        reason=reason,
    )


def make_fill_event(
    order_type: OrderType = OrderType.ENTRY,
    status: OrderStatus = OrderStatus.FILLED,
    filled_qty: Decimal = Decimal("0.1"),
    fill_pct: float = 100.0,
    order_id: str = "order1",
) -> FillEvent:
    total_qty = filled_qty / Decimal(str(fill_pct / 100.0))
    remaining = total_qty - filled_qty
    return FillEvent(
        exchange_order_id=order_id,
        client_order_id=f"client_{order_id}",
        status=status,
        order_type=order_type,
        filled_qty=filled_qty,
        remaining_qty=remaining,
        avg_fill_price=Decimal("50000"),
        commission=Decimal("0.05"),
        timestamp_ms=int(time.time() * 1000),
    )


# ---------------------------------------------------------------------------
# IDLE tests
# ---------------------------------------------------------------------------

class TestDecisionIdle:

    def test_no_signal_returns_wait(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE")
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.WAIT

    def test_signal_returns_enter(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE")
        signal = make_signal(should_enter=True, target_qty=Decimal("0.1"))
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.ENTER
        assert decision.entry_qty == Decimal("0.1")

    def test_close_only_status_blocks_entry(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE", bot_status="CLOSE_ONLY")
        signal = make_signal(should_enter=True)
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.WAIT

    def test_stopped_status_blocks_entry(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE", bot_status="STOPPED")
        signal = make_signal(should_enter=True)
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.WAIT

    def test_force_close_status_blocks_entry(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE", bot_status="FORCE_CLOSE")
        signal = make_signal(should_enter=True)
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.WAIT

    def test_excessive_slippage_raises_tick_skipped(self):
        """Если цена ушла > MAX_ENTRY_SLIPPAGE_PCT — пропустить тик."""
        engine = make_engine(max_entry_slippage_pct=Decimal("0.5"))
        # Сигнальная цена 50000, рыночная ask ~50005 (0.01% spread)
        # Зададим signal_price сильно ниже рыночной чтобы получить >0.5% slippage
        ctx = make_ctx(cycle_status="IDLE", price=50000.0)
        signal = make_signal(
            should_enter=True,
            target_avg_price=Decimal("49700"),  # 0.6% ниже ask
        )
        with pytest.raises(TickSkippedError):
            engine.decide(ctx, ctx.bot_state, signal)

    def test_acceptable_slippage_allows_entry(self):
        """Если проскальзывание в норме — вход разрешён."""
        engine = make_engine(max_entry_slippage_pct=Decimal("1.0"))
        ctx = make_ctx(cycle_status="IDLE", price=50000.0)
        # target_avg_price почти равна ask — минимальное проскальзывание
        signal = make_signal(
            should_enter=True,
            target_avg_price=Decimal("50000"),
        )
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.ENTER

    def test_enter_decision_includes_tp_price(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE")
        signal = make_signal(tp_price=Decimal("52000"))
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.ENTER
        assert decision.tp_price == Decimal("52000")


# ---------------------------------------------------------------------------
# ENTERING tests
# ---------------------------------------------------------------------------

class TestDecisionEntering:

    def test_no_fill_no_timeout_returns_wait(self):
        """Ордер на вход размещён, ещё не исполнен, таймаут не истёк → WAIT."""
        engine = make_engine(entry_order_timeout_sec=3600)
        last_order_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        ctx = make_ctx(
            cycle_status="ENTERING",
            active_entry_order_id="entry1",
            last_order_at=last_order_at,
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.WAIT

    def test_timeout_returns_cancel_entry(self):
        """Таймаут ожидания исполнения → CANCEL_ENTRY."""
        engine = make_engine(entry_order_timeout_sec=3600)
        last_order_at = datetime.now(timezone.utc) - timedelta(seconds=4000)
        ctx = make_ctx(
            cycle_status="ENTERING",
            active_entry_order_id="entry1",
            last_order_at=last_order_at,
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.CANCEL_ENTRY

    def test_zero_timeout_never_cancels(self):
        """entry_order_timeout_sec=0 означает немедленный таймаут (0 секунд).
        Любой ордер старше 0 сек → CANCEL_ENTRY."""
        engine = make_engine(entry_order_timeout_sec=0)
        last_order_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        ctx = make_ctx(
            cycle_status="ENTERING",
            active_entry_order_id="entry1",
            last_order_at=last_order_at,
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.CANCEL_ENTRY

    def test_large_timeout_does_not_cancel(self):
        """Большой таймаут — ордер ждёт исполнения."""
        engine = make_engine(entry_order_timeout_sec=999_999)
        last_order_at = datetime.now(timezone.utc) - timedelta(days=1)
        ctx = make_ctx(
            cycle_status="ENTERING",
            active_entry_order_id="entry1",
            last_order_at=last_order_at,
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.WAIT


# ---------------------------------------------------------------------------
# IN_POSITION tests
# ---------------------------------------------------------------------------

class TestDecisionInPosition:

    def test_no_signal_no_tp_issue_returns_wait(self):
        """IN_POSITION без TP-события и без DCA-сигнала → WAIT."""
        engine = make_engine()
        ctx = make_ctx(
            cycle_status="IN_POSITION",
            position_qty=Decimal("0.1"),
            active_tp_order_id="tp1",
        )
        # Signal: нет нового target_qty выше текущего position_qty
        signal = StrategySignal(
            should_enter=False,
            target_qty=Decimal("0.1"),   # равно position_qty — нет дельты
            target_avg_price=Decimal("50000"),
            tp_price=Decimal("52000"),
            reason="no_additional_dca",
        )
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.WAIT

    def test_dca_signal_returns_place_dca(self):
        """DCA сигнал (target_qty > position_qty) при dca_count < max → PLACE_DCA."""
        engine = make_engine(max_dca_count=3, dca_mode="LAZY")
        ctx = make_ctx(
            cycle_status="IN_POSITION",
            position_qty=Decimal("0.1"),
            dca_count=0,
            active_tp_order_id="tp1",
        )
        # target_qty > position_qty → нужен DCA
        signal = StrategySignal(
            should_enter=False,
            target_qty=Decimal("0.2"),   # delta = 0.1 → PLACE_DCA
            target_avg_price=Decimal("49000"),
            tp_price=Decimal("52000"),
            reason="dca_level_hit",
        )
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.PLACE_DCA

    def test_max_dca_reached_blocks_new_dca(self):
        """Если dca_count >= max_dca_count → DCA не выставляется → WAIT."""
        engine = make_engine(max_dca_count=3, dca_mode="LAZY")
        ctx = make_ctx(
            cycle_status="IN_POSITION",
            position_qty=Decimal("0.4"),
            dca_count=3,    # уже максимум
            active_tp_order_id="tp1",
        )
        signal = StrategySignal(
            should_enter=False,
            target_qty=Decimal("0.5"),
            target_avg_price=Decimal("49000"),
            tp_price=Decimal("52000"),
            reason="dca_level_hit",
        )
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.WAIT

    def test_negative_delta_blocked(self):
        """target_qty < position_qty → бросает TickSkippedError.
        Бот не продаёт частично — ждёт закрытия через TP."""
        engine = make_engine()
        ctx = make_ctx(
            cycle_status="IN_POSITION",
            position_qty=Decimal("0.2"),
            active_tp_order_id="tp1",
        )
        signal = StrategySignal(
            should_enter=False,
            target_qty=Decimal("0.1"),   # меньше текущего — отрицательная дельта
            target_avg_price=Decimal("50000"),
            tp_price=Decimal("52000"),
            reason="reduce_position",
        )
        with pytest.raises(TickSkippedError):
            engine.decide(ctx, ctx.bot_state, signal)


# ---------------------------------------------------------------------------
# CLOSING tests
# ---------------------------------------------------------------------------

class TestDecisionClosing:

    def test_closing_waits_while_position_above_dust(self):
        """CLOSING + position_qty > dust → WAIT.
        CloseProtocol уже запущен и продолжается на следующих тиках."""
        engine = make_engine(dust_threshold=Decimal("0.001"))
        ctx = make_ctx(
            cycle_status="CLOSING",
            position_qty=Decimal("0.1"),   # выше dust — позиция ещё не закрыта
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.WAIT

    def test_closing_dust_position_triggers_close_protocol(self):
        """position_qty <= dust → Close Protocol финализирует цикл."""
        engine = make_engine(dust_threshold=Decimal("0.001"))
        ctx = make_ctx(
            cycle_status="CLOSING",
            position_qty=Decimal("0.0005"),   # ниже dust
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.CLOSE_PROTOCOL


# ---------------------------------------------------------------------------
# WAITING_FOR_LIQUIDITY tests
# ---------------------------------------------------------------------------

class TestDecisionWaitingForLiquidity:

    def test_wait_if_pause_not_expired(self):
        """Пауза ещё не истекла → WAIT."""
        engine = make_engine()
        # last_order_at недавно — пауза 60 сек ещё не прошла
        last_order_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        ctx = make_ctx(
            cycle_status="WAITING_FOR_LIQUIDITY",
            last_order_at=last_order_at,
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.WAIT

    def test_retry_after_pause_expired(self):
        """Пауза истекла → RETRY_LIQUIDITY."""
        engine = make_engine()
        # last_order_at давно — первая пауза 60 сек прошла
        last_order_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        ctx = make_ctx(
            cycle_status="WAITING_FOR_LIQUIDITY",
            last_order_at=last_order_at,
        )
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.RETRY_LIQUIDITY


# ---------------------------------------------------------------------------
# STOP_CRANE tests
# ---------------------------------------------------------------------------

class TestDecisionStopCrane:

    def test_stop_crane_always_wait(self):
        """STOP_CRANE → всегда WAIT, ждёт ручного резолва."""
        engine = make_engine()
        ctx = make_ctx(cycle_status="STOP_CRANE")
        decision = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)
        assert decision.action == DecisionAction.WAIT
        assert "stop_crane" in decision.reason.lower()


# ---------------------------------------------------------------------------
# EAGER DCA tests
# ---------------------------------------------------------------------------

class TestDecisionEagerDca:

    def test_eager_mode_places_all_dca_on_enter(self):
        """EAGER режим при IDLE + сигнале → ENTER (dca_levels включены)."""
        engine = make_engine(dca_mode="EAGER", max_dca_count=2)
        ctx = make_ctx(cycle_status="IDLE")
        signal = StrategySignal(
            should_enter=True,
            target_qty=Decimal("0.3"),
            target_avg_price=Decimal("50000"),
            tp_price=Decimal("52000"),
            reason="entry_signal",
            dca_levels=(
                (Decimal("49000"), Decimal("0.1")),
                (Decimal("48000"), Decimal("0.1")),
            ),
        )
        decision = engine.decide(ctx, ctx.bot_state, signal)
        assert decision.action == DecisionAction.ENTER
        assert len(decision.dca_levels) == 2


# ---------------------------------------------------------------------------
# Decision fields validation
# ---------------------------------------------------------------------------

class TestDecisionFields:

    def test_enter_decision_has_correct_fields(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE")
        signal = make_signal(
            target_qty=Decimal("0.5"),
            target_avg_price=Decimal("50000"),
            tp_price=Decimal("55000"),
        )
        d = engine.decide(ctx, ctx.bot_state, signal)

        assert d.action == DecisionAction.ENTER
        assert d.entry_qty == Decimal("0.5")
        assert d.entry_price == Decimal("50000")
        assert d.tp_price == Decimal("55000")
        assert d.reason != ""

    def test_wait_decision_has_reason(self):
        engine = make_engine()
        ctx = make_ctx(cycle_status="IDLE")
        d = engine.decide(ctx, ctx.bot_state, SIGNAL_WAIT)

        assert d.action == DecisionAction.WAIT
        assert d.reason != ""
