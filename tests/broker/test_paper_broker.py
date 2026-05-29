"""
tests/broker/test_paper_broker.py — юнит-тесты PaperBroker.

Запуск: pytest tests/broker/test_paper_broker.py -v
"""
from decimal import Decimal

import pytest

from broker.broker import BrokerError, InsufficientFundsError, OrderNotFoundError
from broker.models import (
    BrokerMode,
    MarketInfo,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from broker.paper_broker import PaperBroker


# ---------------------------------------------------------------------------
# Вспомогательные классы и фабрики
# ---------------------------------------------------------------------------

class MockEmitter:
    """Записывает все emit()-вызовы для проверки в тестах."""

    def __init__(self):
        self.events: list = []

    def emit(self, event_type, level, message, payload=None):
        self.events.append({
            "event_type": event_type,
            "level": level,
            "payload": payload or {},
        })

    def emitted(self, event_type: str) -> list:
        return [e for e in self.events if e["event_type"] == event_type]


class MockTradeRepo:
    """Записывает все save_fill()-вызовы."""

    def __init__(self):
        self.saved: list = []

    def save_fill(self, fill, bot_id: str):
        self.saved.append(fill)


def make_broker(
    initial_balance: str = "1000",
    commission_pct: str = "0",
    slippage_pct: str = "0",
) -> tuple[PaperBroker, MockEmitter, MockTradeRepo]:
    emitter = MockEmitter()
    repo = MockTradeRepo()
    broker = PaperBroker(
        initial_balance=Decimal(initial_balance),
        commission_pct=Decimal(commission_pct),
        slippage_pct=Decimal(slippage_pct),
        emitter=emitter,
        trade_repo=repo,
        bot_id="test_bot",
    )
    return broker, emitter, repo


def make_order(
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "0.01",
    price: str | None = None,
    ticker: str = "BTCUSDT",
    client_order_id: str = "test-uuid-001",
) -> OrderRequest:
    return OrderRequest(
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        price=Decimal(price) if price is not None else None,
        client_order_id=client_order_id,
        bot_id="test_bot",
        cycle_id="cycle_001",
    )


# ---------------------------------------------------------------------------
# Начальное состояние
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_initial_balance(self):
        broker, _, _ = make_broker(initial_balance="1000")
        balance = broker.get_balance()

        assert balance.free['USDT'] == Decimal("1000")
        assert balance.locked['USDT'] == Decimal("0")
        assert balance.free['USDT'] + balance.locked['USDT'] == Decimal("1000")

    def test_mode_is_paper(self):
        broker, _, _ = make_broker()
        assert broker.get_mode() == BrokerMode.PAPER

    def test_no_open_orders_initially(self):
        broker, _, _ = make_broker()
        assert broker.get_open_orders() == []

    def test_market_order_without_price_raises(self):
        """MARKET ордер до первого process_market_tick — нет текущей цены."""
        broker, _, _ = make_broker()
        order = make_order(order_type=OrderType.MARKET)

        with pytest.raises(BrokerError):
            broker.create_order(order)


# ---------------------------------------------------------------------------
# MARKET BUY: баланс, исполнение, события
# ---------------------------------------------------------------------------

class TestMarketBuy:
    def test_balance_decreases_by_cost(self):
        broker, _, _ = make_broker(initial_balance="1000")
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        # qty=0.01, ask=50000, no commission → cost=500
        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        balance = broker.get_balance()
        assert balance.free['USDT'] == Decimal("500")
        assert balance.locked['USDT'] == Decimal("0")

    def test_commission_deducted(self):
        broker, _, _ = make_broker(initial_balance="1000", commission_pct="0.001")
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        # qty=0.01, ask=50000 → execution_price=50000
        # commission = 0.01 * 50000 * 0.001 = 0.5
        # total_cost = 500 + 0.5 = 500.5
        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        balance = broker.get_balance()
        assert balance.free['USDT'] == Decimal("499.5")

    def test_slippage_applied_to_ask(self):
        # slippage=0.001 → execution_price = ask * 1.001 = 50000 * 1.001 = 50050
        # cost = 0.01 * 50050 = 500.5
        broker, _, _ = make_broker(initial_balance="1000", slippage_pct="0.001")
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        assert broker.get_balance().free['USDT'] == Decimal("499.5")

    def test_fill_added_to_queue(self):
        broker, _, _ = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        # Fills доступны через get_pending_fills() — process_market_tick не дренирует очередь
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))
        fills = broker.get_pending_fills()
        assert len(fills) == 1
        assert fills[0].filled_qty == Decimal("0.01")

    def test_order_filled_event_emitted(self):
        broker, emitter, _ = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        assert len(emitter.emitted("ORDER_FILLED")) == 1

    def test_trade_repo_called(self):
        broker, _, repo = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        assert len(repo.saved) == 1

    def test_insufficient_funds_raises(self):
        broker, _, _ = make_broker(initial_balance="100")
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        # 0.01 * 50000 = 500 > 100
        order = make_order(side=OrderSide.BUY, quantity="0.01")

        with pytest.raises(InsufficientFundsError):
            broker.create_order(order)

    def test_create_order_returns_pending(self):
        broker, _, _ = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.BUY, quantity="0.01")
        created = broker.create_order(order)

        # Контракт IBroker: create_order всегда возвращает PENDING
        assert created.status == OrderStatus.PENDING
        assert created.mode == BrokerMode.PAPER
        assert created.client_order_id == order.client_order_id


# ---------------------------------------------------------------------------
# MARKET SELL: баланс
# ---------------------------------------------------------------------------

class TestMarketSell:
    def test_balance_increases_by_proceeds(self):
        broker, _, _ = make_broker(initial_balance="1000")
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        # qty=0.01, bid=49000 → proceeds=490
        order = make_order(side=OrderSide.SELL, quantity="0.01")
        broker.create_order(order)

        assert broker.get_balance().free['USDT'] == Decimal("1490")

    def test_slippage_applied_to_bid(self):
        # slippage=0.001 → execution_price = 49000 * (1 - 0.001) = 48951
        # proceeds = 0.01 * 48951 = 489.51
        broker, _, _ = make_broker(initial_balance="1000", slippage_pct="0.001")
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.SELL, quantity="0.01")
        broker.create_order(order)

        assert broker.get_balance().free['USDT'] == Decimal("1489.51")

    def test_fill_is_partial_false(self):
        broker, _, _ = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.SELL, quantity="0.01")
        broker.create_order(order)

        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))
        fills = broker.get_pending_fills()
        assert fills[0].remaining_qty == Decimal("0")


# ---------------------------------------------------------------------------
# LIMIT BUY: размещение и исполнение
# ---------------------------------------------------------------------------

class TestLimitBuy:
    def test_placing_locks_usdt(self):
        broker, _, _ = make_broker(initial_balance="1000")

        # qty=0.01, price=48000 → lock=480
        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        broker.create_order(order)

        balance = broker.get_balance()
        assert balance.free['USDT'] == Decimal("520")
        assert balance.locked['USDT'] == Decimal("480")
        assert balance.free['USDT'] + balance.locked['USDT'] == Decimal("1000")

    def test_appears_in_open_orders(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        broker.create_order(order)

        open_orders = broker.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0].client_order_id == order.client_order_id

    def test_no_fill_when_ask_above_limit(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        broker.create_order(order)

        # ask=50000 > limit=48000 → не исполняется
        fills = broker.process_market_tick(
            bid=Decimal("49990"), ask=Decimal("50000")
        )
        assert fills == []
        assert len(broker.get_open_orders()) == 1

    def test_fills_when_ask_equals_limit(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        broker.create_order(order)

        # ask=48000 == limit=48000 → исполняется
        broker.process_market_tick(bid=Decimal("47990"), ask=Decimal("48000"))
        fills = broker.get_pending_fills()
        assert len(fills) == 1

    def test_fills_when_ask_below_limit(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        broker.create_order(order)

        # ask=47000 < limit=48000 → исполняется
        broker.process_market_tick(bid=Decimal("46990"), ask=Decimal("47000"))
        fills = broker.get_pending_fills()
        assert len(fills) == 1
        assert fills[0].avg_fill_price == Decimal("48000")  # по лимитной цене

    def test_fill_updates_balance(self):
        broker, _, _ = make_broker(initial_balance="1000")

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        broker.create_order(order)

        broker.process_market_tick(bid=Decimal("47990"), ask=Decimal("47000"))

        # После fill: cost = 0.01 * 48000 = 480, free = 1000 - 480 = 520
        balance = broker.get_balance()
        assert balance.free['USDT'] == Decimal("520")
        assert balance.locked['USDT'] == Decimal("0")

    def test_fill_removes_from_open_orders(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        broker.create_order(order)

        broker.process_market_tick(bid=Decimal("47990"), ask=Decimal("47000"))

        assert broker.get_open_orders() == []

    def test_insufficient_funds_raises(self):
        broker, _, _ = make_broker(initial_balance="100")

        # qty=0.01, price=48000 → lock=480 > free=100
        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        with pytest.raises(InsufficientFundsError):
            broker.create_order(order)


# ---------------------------------------------------------------------------
# LIMIT SELL: размещение и исполнение
# ---------------------------------------------------------------------------

class TestLimitSell:
    def test_placing_does_not_lock_usdt(self):
        """SELL LIMIT не блокирует USDT — блокируется базовый актив (вне PaperBroker)."""
        broker, _, _ = make_broker(initial_balance="1000")

        order = make_order(
            side=OrderSide.SELL, order_type=OrderType.LIMIT,
            quantity="0.01", price="52000",
        )
        broker.create_order(order)

        balance = broker.get_balance()
        assert balance.free['USDT'] == Decimal("1000")
        assert balance.locked['USDT'] == Decimal("0")

    def test_fills_when_bid_equals_limit(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.SELL, order_type=OrderType.LIMIT,
            quantity="0.01", price="52000",
        )
        broker.create_order(order)

        # bid=52000 == limit=52000 → исполняется
        broker.process_market_tick(bid=Decimal("52000"), ask=Decimal("52010"))
        fills = broker.get_pending_fills()
        assert len(fills) == 1
        assert fills[0].avg_fill_price == Decimal("52000")

    def test_no_fill_when_bid_below_limit(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.SELL, order_type=OrderType.LIMIT,
            quantity="0.01", price="52000",
        )
        broker.create_order(order)

        # bid=51000 < limit=52000 → не исполняется
        fills = broker.process_market_tick(
            bid=Decimal("51000"), ask=Decimal("51010")
        )
        assert fills == []

    def test_fill_increases_balance(self):
        broker, _, _ = make_broker(initial_balance="1000")

        order = make_order(
            side=OrderSide.SELL, order_type=OrderType.LIMIT,
            quantity="0.01", price="52000",
        )
        broker.create_order(order)

        broker.process_market_tick(bid=Decimal("52000"), ask=Decimal("52010"))

        # proceeds = 0.01 * 52000 = 520, free = 1000 + 520 = 1520
        assert broker.get_balance().free['USDT'] == Decimal("1520")


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:
    def test_cancel_limit_buy_unlocks_usdt(self):
        broker, _, _ = make_broker(initial_balance="1000")

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        created = broker.create_order(order)

        # После размещения: free=520, locked=480
        assert broker.get_balance().free['USDT'] == Decimal("520")

        result = broker.cancel_order(created.exchange_order_id)

        assert result is True
        # После отмены: free=1000, locked=0
        balance = broker.get_balance()
        assert balance.free['USDT'] == Decimal("1000")
        assert balance.locked['USDT'] == Decimal("0")

    def test_cancel_removes_from_open_orders(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        created = broker.create_order(order)

        broker.cancel_order(created.exchange_order_id)

        assert broker.get_open_orders() == []

    def test_cancel_nonexistent_returns_true(self):
        """Idempotent: отмена несуществующего ордера не падает."""
        broker, _, _ = make_broker()

        result = broker.cancel_order("nonexistent-order-id")

        assert result is True

    def test_cancel_already_filled_returns_true(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        created = broker.create_order(order)

        # Заполнить ордер
        broker.process_market_tick(bid=Decimal("47000"), ask=Decimal("47000"))

        # Попытка отменить уже исполненный
        result = broker.cancel_order(created.exchange_order_id)
        assert result is True


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------

class TestGetOrderStatus:
    def test_pending_order_returns_pending(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        created = broker.create_order(order)

        status = broker.get_order_status(created.exchange_order_id)
        assert status == OrderStatus.PENDING

    def test_nonexistent_raises(self):
        broker, _, _ = make_broker()

        with pytest.raises(OrderNotFoundError):
            broker.get_order_status("ghost-order-id")

    def test_filled_order_raises(self):
        broker, _, _ = make_broker()

        order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000",
        )
        created = broker.create_order(order)
        broker.process_market_tick(bid=Decimal("47000"), ask=Decimal("47000"))

        # После fill ордер удалён из pending — OrderNotFoundError
        with pytest.raises(OrderNotFoundError):
            broker.get_order_status(created.exchange_order_id)


# ---------------------------------------------------------------------------
# get_open_orders: фильтрация по тикеру
# ---------------------------------------------------------------------------

class TestGetOpenOrders:
    def test_filter_by_ticker(self):
        broker, _, _ = make_broker(initial_balance="10000")

        btc_order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.01", price="48000", ticker="BTCUSDT",
            client_order_id="btc-order",
        )
        eth_order = make_order(
            side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity="0.1", price="3000", ticker="ETHUSDT",
            client_order_id="eth-order",
        )
        broker.create_order(btc_order)
        broker.create_order(eth_order)

        btc_only = broker.get_open_orders(ticker="BTCUSDT")
        assert len(btc_only) == 1
        assert btc_only[0].ticker == "BTCUSDT"

    def test_no_filter_returns_all(self):
        broker, _, _ = make_broker(initial_balance="10000")

        for i in range(3):
            order = make_order(
                side=OrderSide.SELL, order_type=OrderType.LIMIT,
                quantity="0.01", price="52000",
                client_order_id=f"order-{i}",
            )
            broker.create_order(order)

        assert len(broker.get_open_orders()) == 3


# ---------------------------------------------------------------------------
# process_market_tick: очередь fills
# ---------------------------------------------------------------------------

class TestFillQueue:
    def test_market_fill_returned_on_next_tick(self):
        """MARKET fill из тика N доступен через get_pending_fills() в тике N+1."""
        broker, _, _ = make_broker()

        # Тик N: обновить цену
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        # Тик N: разместить MARKET ордер → fill идёт в очередь
        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        # Тик N+1: обновить цену, дренировать fills
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))
        fills = broker.get_pending_fills()
        assert len(fills) == 1

    def test_queue_cleared_after_pop(self):
        broker, _, _ = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        order = make_order(side=OrderSide.BUY, quantity="0.01")
        broker.create_order(order)

        # Первый drain — забираем fill
        broker.get_pending_fills()

        # Следующий вызов — очередь пустая
        fills = broker.get_pending_fills()
        assert fills == []

    def test_multiple_fills_accumulated(self):
        """Два MARKET ордера за тик → оба в очереди → возвращаются вместе."""
        broker, _, _ = make_broker(initial_balance="10000")
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))

        broker.create_order(make_order(side=OrderSide.BUY, quantity="0.01",
                                       client_order_id="order-1"))
        broker.create_order(make_order(side=OrderSide.SELL, quantity="0.01",
                                       client_order_id="order-2"))

        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))
        fills = broker.get_pending_fills()
        assert len(fills) == 2

    def test_limit_and_market_fills_returned_together(self):
        """LIMIT fill из текущего тика + MARKET fill из предыдущего тика."""
        broker, _, _ = make_broker(initial_balance="10000")

        # Тик 1: разместить MARKET BUY
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))
        broker.create_order(make_order(side=OrderSide.BUY, quantity="0.01",
                                       client_order_id="market-order"))

        # Тик 2: + LIMIT SELL срабатывает → всего 2 fills
        limit_order = make_order(
            side=OrderSide.SELL, order_type=OrderType.LIMIT,
            quantity="0.01", price="49500", client_order_id="limit-order",
        )
        broker.create_order(limit_order)

        # bid=49500 → LIMIT SELL триггерится; market fill из тика 1 тоже в очереди
        broker.process_market_tick(bid=Decimal("49500"), ask=Decimal("49510"))
        fills = broker.get_pending_fills()
        assert len(fills) == 2

    def test_fill_mode_is_paper(self):
        broker, _, _ = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))
        broker.create_order(make_order(side=OrderSide.BUY, quantity="0.01"))

        fills = broker.get_pending_fills()
        assert fills[0].exchange_order_id.startswith("paper_")

    def test_fill_is_not_partial(self):
        broker, _, _ = make_broker()
        broker.process_market_tick(bid=Decimal("49000"), ask=Decimal("50000"))
        broker.create_order(make_order(side=OrderSide.BUY, quantity="0.01"))

        fills = broker.get_pending_fills()
        assert fills[0].remaining_qty == Decimal("0")


# ---------------------------------------------------------------------------
# set_market_info и get_market_info
# ---------------------------------------------------------------------------

class TestMarketInfo:
    def test_default_returned_when_not_set(self):
        broker, _, _ = make_broker()
        info = broker.get_market_info("BTCUSDT")

        assert info.ticker == "BTCUSDT"
        assert info.min_qty > Decimal("0")

    def test_custom_info_returned_after_set(self):
        broker, _, _ = make_broker()
        custom = MarketInfo(
            ticker="BTCUSDT",
            min_qty=Decimal("0.0001"),
            step_size=Decimal("0.0001"),
            min_notional=Decimal("10"),
            price_precision=2,
            tick_size=Decimal("0.01"),
        )
        broker.set_market_info(custom)

        info = broker.get_market_info("BTCUSDT")
        assert info.min_qty == Decimal("0.0001")
        assert info.min_notional == Decimal("10")
