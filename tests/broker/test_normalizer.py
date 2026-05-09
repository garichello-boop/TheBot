"""
tests/broker/test_normalizer.py — юнит-тесты OrderNormalizer.

Запуск: pytest tests/broker/test_normalizer.py -v
"""
from decimal import Decimal

import pytest

from broker.models import (
    BrokerMode,
    MarketInfo,
    NormalizeResult,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    SkipReason,
)
from broker.normalizer import OrderNormalizer


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

def make_market_info(
    ticker="BTCUSDT",
    min_qty="0.001",
    step_size="0.001",
    min_notional="5",
    price_precision=2,
    tick_size="0.01",
) -> MarketInfo:
    return MarketInfo(
        ticker=ticker,
        min_qty=Decimal(min_qty),
        step_size=Decimal(step_size),
        min_notional=Decimal(min_notional),
        price_precision=price_precision,
        tick_size=Decimal(tick_size),
    )


def make_order(
    ticker="BTCUSDT",
    side=OrderSide.BUY,
    order_type=OrderType.LIMIT,
    quantity="0.01",
    price="50000",
    client_order_id="test-uuid-001",
    bot_id="test_bot",
    cycle_id="cycle_001",
) -> OrderRequest:
    return OrderRequest(
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        price=Decimal(price) if price is not None else None,
        client_order_id=client_order_id,
        bot_id=bot_id,
        cycle_id=cycle_id,
    )


# ---------------------------------------------------------------------------
# _floor_to_step: квантование вниз
# ---------------------------------------------------------------------------

class TestFloorToStep:
    def test_exact_multiple(self):
        result = OrderNormalizer._floor_to_step(Decimal("3.0"), Decimal("1.0"))
        assert result == Decimal("3.0")

    def test_rounds_down_integer_step(self):
        result = OrderNormalizer._floor_to_step(Decimal("3.7"), Decimal("1.0"))
        assert result == Decimal("3.0")

    def test_rounds_down_decimal_step(self):
        # 0.00375 с шагом 0.001 → 0.003
        result = OrderNormalizer._floor_to_step(Decimal("0.00375"), Decimal("0.001"))
        assert result == Decimal("0.003")

    def test_price_tick_size(self):
        # 3200.1234 с tick_size 0.01 → 3200.12
        result = OrderNormalizer._floor_to_step(Decimal("3200.1234"), Decimal("0.01"))
        assert result == Decimal("3200.12")

    def test_already_quantized(self):
        result = OrderNormalizer._floor_to_step(Decimal("0.003"), Decimal("0.001"))
        assert result == Decimal("0.003")

    def test_very_small_step(self):
        result = OrderNormalizer._floor_to_step(Decimal("1.23456789"), Decimal("0.00000001"))
        assert result == Decimal("1.23456789")


# ---------------------------------------------------------------------------
# Happy path: ордер проходит нормализацию
# ---------------------------------------------------------------------------

class TestNormalizeHappyPath:
    def test_limit_order_passes(self):
        info = make_market_info()
        order = make_order(quantity="0.01", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip
        assert result.order is not None
        assert result.order.quantity == Decimal("0.01")
        assert result.order.price == Decimal("50000.00")  # квантовано по tick_size 0.01

    def test_market_order_passes_without_price(self):
        info = make_market_info()
        order = make_order(order_type=OrderType.MARKET, price=None, quantity="0.01")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip
        assert result.order.price is None

    def test_preserves_client_order_id(self):
        info = make_market_info()
        order = make_order(client_order_id="my-unique-id-42")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip
        assert result.order.client_order_id == "my-unique-id-42"

    def test_preserves_cycle_id_and_bot_id(self):
        info = make_market_info()
        order = make_order(bot_id="mybot", cycle_id="cycle_99")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.order.bot_id == "mybot"
        assert result.order.cycle_id == "cycle_99"

    def test_qty_quantized_down(self):
        # 0.0037 с step_size 0.001 → 0.003
        info = make_market_info(step_size="0.001")
        order = make_order(quantity="0.0037", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip
        assert result.order.quantity == Decimal("0.003")

    def test_price_quantized_down(self):
        # 49999.999 с tick_size 0.01 → 49999.99
        info = make_market_info(tick_size="0.01")
        order = make_order(quantity="0.01", price="49999.999")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip
        assert result.order.price == Decimal("49999.99")

    def test_sell_order_passes(self):
        info = make_market_info()
        order = make_order(side=OrderSide.SELL, quantity="0.01", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip


# ---------------------------------------------------------------------------
# SKIP: QTY_BECAME_ZERO
# ---------------------------------------------------------------------------

class TestSkipQtyBecameZero:
    def test_tiny_qty_rounds_to_zero(self):
        # 0.0003 с step_size 0.001 → floor = 0
        info = make_market_info(step_size="0.001", min_qty="0.001")
        order = make_order(quantity="0.0003", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.is_skip
        assert result.skip_reason == SkipReason.QTY_BECAME_ZERO
        assert result.skip_event_type == "ORDER_CREATE_FAILED"

    def test_qty_less_than_step_rounds_to_zero(self):
        info = make_market_info(step_size="1.0", min_qty="1.0")
        order = make_order(quantity="0.9", price="100")
        free = Decimal("10000")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.is_skip
        assert result.skip_reason == SkipReason.QTY_BECAME_ZERO

    def test_skip_result_has_no_order(self):
        info = make_market_info(step_size="0.001", min_qty="0.001")
        order = make_order(quantity="0.0003")
        result = OrderNormalizer.normalize(order, info, Decimal("1000"))

        assert result.order is None


# ---------------------------------------------------------------------------
# SKIP: BELOW_MIN_QTY
# ---------------------------------------------------------------------------

class TestSkipBelowMinQty:
    def test_qty_below_min_after_quantization(self):
        # qty=0.0012, step=0.001 → quantized=0.001, min_qty=0.005 → SKIP
        info = make_market_info(step_size="0.001", min_qty="0.005")
        order = make_order(quantity="0.0012", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.is_skip
        assert result.skip_reason == SkipReason.BELOW_MIN_QTY
        assert result.skip_event_type == "ORDER_CREATE_FAILED"

    def test_qty_exactly_at_min_passes(self):
        info = make_market_info(step_size="0.001", min_qty="0.001")
        order = make_order(quantity="0.001", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip

    def test_qty_just_above_min_passes(self):
        info = make_market_info(step_size="0.001", min_qty="0.001")
        order = make_order(quantity="0.002", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip


# ---------------------------------------------------------------------------
# SKIP: INSUFFICIENT_FUNDS
# ---------------------------------------------------------------------------

class TestSkipInsufficientFunds:
    def test_notional_exceeds_free_balance(self):
        # qty=0.01, price=50000 → notional=500 > free=100
        info = make_market_info()
        order = make_order(quantity="0.01", price="50000")
        free = Decimal("100")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.is_skip
        assert result.skip_reason == SkipReason.INSUFFICIENT_FUNDS
        assert result.skip_event_type == "INSUFFICIENT_FUNDS"

    def test_notional_exactly_equals_free_passes(self):
        # qty=0.01, price=50000 → notional=500 == free=500
        info = make_market_info()
        order = make_order(quantity="0.01", price="50000")
        free = Decimal("500")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip

    def test_market_order_with_estimated_price_checks_balance(self):
        info = make_market_info()
        order = make_order(order_type=OrderType.MARKET, price=None, quantity="0.01")
        free = Decimal("100")  # 0.01 * 50000 = 500 > 100

        result = OrderNormalizer.normalize(
            order, info, free, estimated_price=Decimal("50000")
        )

        assert result.is_skip
        assert result.skip_reason == SkipReason.INSUFFICIENT_FUNDS

    def test_market_order_without_estimated_price_skips_balance_check(self):
        # Нет estimated_price → проверка баланса не выполняется
        info = make_market_info()
        order = make_order(order_type=OrderType.MARKET, price=None, quantity="0.01")
        free = Decimal("0.01")  # Явно недостаточно, но проверка не выполняется

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip


# ---------------------------------------------------------------------------
# SKIP: BELOW_MIN_NOTIONAL (маппится в BELOW_MIN_QTY)
# ---------------------------------------------------------------------------

class TestSkipBelowMinNotional:
    def test_notional_below_minimum(self):
        # qty=0.001, price=1.0 → notional=0.001 < min_notional=5
        info = make_market_info(min_notional="5", min_qty="0.001", step_size="0.001")
        order = make_order(quantity="0.001", price="1.0")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.is_skip
        assert result.skip_reason == SkipReason.BELOW_MIN_QTY
        assert result.skip_event_type == "ORDER_CREATE_FAILED"

    def test_notional_exactly_at_minimum_passes(self):
        # qty=0.001, price=5000 → notional=5.0 == min_notional=5
        info = make_market_info(min_notional="5", min_qty="0.001", step_size="0.001")
        order = make_order(quantity="0.001", price="5000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip

    def test_market_order_with_estimated_price_checks_notional(self):
        info = make_market_info(min_notional="5", min_qty="0.001", step_size="0.001")
        order = make_order(order_type=OrderType.MARKET, price=None, quantity="0.001")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(
            order, info, free, estimated_price=Decimal("1.0")
        )

        assert result.is_skip
        assert result.skip_reason == SkipReason.BELOW_MIN_QTY


# ---------------------------------------------------------------------------
# NormalizeResult: фабричные методы
# ---------------------------------------------------------------------------

class TestNormalizeResult:
    def test_ok_factory(self):
        order = make_order()
        result = NormalizeResult.ok(order)

        assert not result.is_skip
        assert result.order is order
        assert result.skip_reason is None
        assert result.skip_event_type is None

    def test_skip_insufficient_funds_event_type(self):
        result = NormalizeResult.skip(SkipReason.INSUFFICIENT_FUNDS)

        assert result.is_skip
        assert result.skip_event_type == "INSUFFICIENT_FUNDS"
        assert result.order is None

    def test_skip_below_min_qty_event_type(self):
        result = NormalizeResult.skip(SkipReason.BELOW_MIN_QTY)

        assert result.is_skip
        assert result.skip_event_type == "ORDER_CREATE_FAILED"

    def test_skip_qty_became_zero_event_type(self):
        result = NormalizeResult.skip(SkipReason.QTY_BECAME_ZERO)

        assert result.is_skip
        assert result.skip_event_type == "ORDER_CREATE_FAILED"


# ---------------------------------------------------------------------------
# Граничные кейсы
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_qty_quantization_then_below_min(self):
        """
        qty=0.0019 с step=0.001 квантуется до 0.001.
        min_qty=0.002 → после квантования ниже минимума → BELOW_MIN_QTY.
        Важно: проверка min_qty идёт ПОСЛЕ квантования.
        """
        info = make_market_info(step_size="0.001", min_qty="0.002")
        order = make_order(quantity="0.0019", price="50000")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.is_skip
        assert result.skip_reason == SkipReason.BELOW_MIN_QTY

    def test_large_step_size(self):
        # Шаг 10 единиц, qty=25 → квантуется до 20
        info = make_market_info(step_size="10", min_qty="10", min_notional="1")
        order = make_order(quantity="25", price="1")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip
        assert result.order.quantity == Decimal("20")

    def test_price_not_quantized_for_market_order(self):
        """MARKET ордера не имеют price → price в результате None."""
        info = make_market_info()
        order = make_order(order_type=OrderType.MARKET, price=None, quantity="0.01")
        free = Decimal("1000")

        result = OrderNormalizer.normalize(order, info, free)

        assert result.order.price is None

    def test_sell_order_does_not_check_free_balance_without_price(self):
        """SELL MARKET без estimated_price — не проверяем баланс."""
        info = make_market_info()
        order = make_order(
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            price=None,
            quantity="0.01",
        )
        free = Decimal("0")

        result = OrderNormalizer.normalize(order, info, free)

        assert not result.is_skip

    def test_multiple_skips_priority_qty_zero_first(self):
        """
        qty настолько мала что обнуляется при квантовании.
        Ожидаем QTY_BECAME_ZERO, а не INSUFFICIENT_FUNDS.
        """
        info = make_market_info(step_size="1.0", min_qty="1.0")
        order = make_order(quantity="0.5", price="100")
        free = Decimal("0.01")  # и баланс тоже недостаточен

        result = OrderNormalizer.normalize(order, info, free)

        # Первая проверка: квантование → 0 → QTY_BECAME_ZERO
        assert result.skip_reason == SkipReason.QTY_BECAME_ZERO
