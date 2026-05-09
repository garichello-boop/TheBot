"""
tests/broker/test_models_and_adapters.py — smoke-тесты моделей и адаптеров.

Запуск: pytest tests/broker/test_models_and_adapters.py -v
"""
from decimal import Decimal

import pytest

from broker.exchange_adapter import (
    BinanceExchangeAdapter,
    BybitExchangeAdapter,
    NoClientOrderIdAdapter,
    OKXExchangeAdapter,
)
from broker.models import (
    Balance,
    BrokerMode,
    NormalizeResult,
    OrderFill,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    SkipReason,
)
from broker.order_tracker import BYBIT_STATUS_MAP, map_bybit_status


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

class TestBalance:
    def test_total_is_sum_of_free_and_locked(self):
        b = Balance(free=Decimal("700"), locked=Decimal("300"))
        assert b.total == Decimal("1000")

    def test_total_with_zero_locked(self):
        b = Balance(free=Decimal("500"), locked=Decimal("0"))
        assert b.total == Decimal("500")

    def test_total_with_zero_free(self):
        b = Balance(free=Decimal("0"), locked=Decimal("200"))
        assert b.total == Decimal("200")

    def test_frozen_cannot_mutate(self):
        b = Balance(free=Decimal("100"), locked=Decimal("0"))
        with pytest.raises(Exception):
            b.free = Decimal("999")  # type: ignore


# ---------------------------------------------------------------------------
# NormalizeResult: фабричные методы
# ---------------------------------------------------------------------------

class TestNormalizeResultFactories:
    def test_ok_sets_order_not_skip(self):
        order = OrderRequest(
            ticker="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=Decimal("50000"),
            client_order_id="test",
            bot_id="bot",
            cycle_id="cycle",
        )
        result = NormalizeResult.ok(order)

        assert result.is_skip is False
        assert result.order is order
        assert result.skip_reason is None
        assert result.skip_event_type is None

    def test_skip_insufficient_funds(self):
        result = NormalizeResult.skip(SkipReason.INSUFFICIENT_FUNDS)

        assert result.is_skip is True
        assert result.order is None
        assert result.skip_event_type == "INSUFFICIENT_FUNDS"

    def test_skip_below_min_qty(self):
        result = NormalizeResult.skip(SkipReason.BELOW_MIN_QTY)

        assert result.skip_event_type == "ORDER_CREATE_FAILED"

    def test_skip_qty_became_zero(self):
        result = NormalizeResult.skip(SkipReason.QTY_BECAME_ZERO)

        assert result.skip_event_type == "ORDER_CREATE_FAILED"


# ---------------------------------------------------------------------------
# OrderFill: дефолты
# ---------------------------------------------------------------------------

class TestOrderFill:
    def test_is_partial_defaults_to_false(self):
        fill = OrderFill(
            exchange_order_id="ex-001",
            client_order_id="cl-001",
            ticker="BTCUSDT",
            side=OrderSide.BUY,
            filled_qty=Decimal("0.01"),
            avg_price=Decimal("50000"),
            commission=Decimal("0.5"),
            mode=BrokerMode.LIVE,
            timestamp=1700000000.0,
        )
        assert fill.is_partial is False

    def test_is_partial_can_be_true(self):
        fill = OrderFill(
            exchange_order_id="ex-002",
            client_order_id="cl-002",
            ticker="BTCUSDT",
            side=OrderSide.SELL,
            filled_qty=Decimal("0.005"),
            avg_price=Decimal("50000"),
            commission=Decimal("0.25"),
            mode=BrokerMode.PAPER,
            timestamp=1700000001.0,
            is_partial=True,
        )
        assert fill.is_partial is True


# ---------------------------------------------------------------------------
# BybitExchangeAdapter
# ---------------------------------------------------------------------------

class TestBybitExchangeAdapter:
    def setup_method(self):
        self.adapter = BybitExchangeAdapter()

    def test_field_name(self):
        assert self.adapter.field_name == "orderLinkId"

    def test_inject_adds_field(self):
        params = {"symbol": "BTCUSDT", "side": "Buy"}
        result = self.adapter.inject(params, "my-uuid-123")

        assert result["orderLinkId"] == "my-uuid-123"
        assert result["symbol"] == "BTCUSDT"  # остальные поля сохранены

    def test_inject_does_not_mutate_original(self):
        params = {"symbol": "BTCUSDT"}
        self.adapter.inject(params, "my-uuid-123")

        assert "orderLinkId" not in params

    def test_extract_from_result_field(self):
        response = {"retCode": 0, "result": {"orderLinkId": "my-uuid-123"}}
        assert self.adapter.extract(response) == "my-uuid-123"

    def test_extract_from_flat_response(self):
        response = {"orderLinkId": "flat-uuid"}
        assert self.adapter.extract(response) == "flat-uuid"

    def test_extract_returns_none_when_missing(self):
        response = {"retCode": 0, "result": {}}
        assert self.adapter.extract(response) is None

    def test_supports_client_order_id(self):
        assert self.adapter.supports_client_order_id is True


# ---------------------------------------------------------------------------
# BinanceExchangeAdapter
# ---------------------------------------------------------------------------

class TestBinanceExchangeAdapter:
    def setup_method(self):
        self.adapter = BinanceExchangeAdapter()

    def test_field_name(self):
        assert self.adapter.field_name == "newClientOrderId"

    def test_inject_adds_new_client_order_id(self):
        params = {"symbol": "BTCUSDT"}
        result = self.adapter.inject(params, "binance-uuid")

        assert result["newClientOrderId"] == "binance-uuid"

    def test_inject_does_not_mutate_original(self):
        params = {"symbol": "BTCUSDT"}
        self.adapter.inject(params, "binance-uuid")

        assert "newClientOrderId" not in params

    def test_extract_from_client_order_id_field(self):
        # Binance возвращает clientOrderId (без 'new') в ответах
        response = {"clientOrderId": "binance-uuid"}
        assert self.adapter.extract(response) == "binance-uuid"

    def test_extract_returns_none_when_missing(self):
        response = {"orderId": 12345}
        assert self.adapter.extract(response) is None

    def test_supports_client_order_id(self):
        assert self.adapter.supports_client_order_id is True


# ---------------------------------------------------------------------------
# OKXExchangeAdapter
# ---------------------------------------------------------------------------

class TestOKXExchangeAdapter:
    def setup_method(self):
        self.adapter = OKXExchangeAdapter()

    def test_field_name(self):
        assert self.adapter.field_name == "clOrdId"

    def test_inject_truncates_uuid_to_32_chars(self):
        uuid = "12345678-1234-1234-1234-123456789012"  # 36 символов с дефисами
        params = {}
        result = self.adapter.inject(params, uuid)

        # UUID без дефисов = 32 символа — точно влезает
        assert len(result["clOrdId"]) <= 32
        assert "-" not in result["clOrdId"]

    def test_extract_from_data_list(self):
        response = {"data": [{"clOrdId": "okx-order-id"}]}
        assert self.adapter.extract(response) == "okx-order-id"

    def test_extract_from_flat_response(self):
        response = {"clOrdId": "flat-okx-id"}
        assert self.adapter.extract(response) == "flat-okx-id"

    def test_extract_returns_none_when_missing(self):
        response = {"data": [{"ordId": "12345"}]}
        assert self.adapter.extract(response) is None


# ---------------------------------------------------------------------------
# NoClientOrderIdAdapter
# ---------------------------------------------------------------------------

class TestNoClientOrderIdAdapter:
    def setup_method(self):
        self.adapter = NoClientOrderIdAdapter()

    def test_does_not_support_client_order_id(self):
        assert self.adapter.supports_client_order_id is False

    def test_inject_returns_params_unchanged(self):
        params = {"symbol": "BTCUSDT", "side": "Buy"}
        result = self.adapter.inject(params, "some-uuid")

        assert result == params
        assert "orderLinkId" not in result
        assert "newClientOrderId" not in result

    def test_extract_always_returns_none(self):
        response = {"orderId": "12345", "orderLinkId": "leaked-id"}
        assert self.adapter.extract(response) is None


# ---------------------------------------------------------------------------
# map_bybit_status
# ---------------------------------------------------------------------------

class TestMapBybitStatus:
    def test_filled(self):
        assert map_bybit_status("Filled") == OrderStatus.FILLED

    def test_partially_filled(self):
        assert map_bybit_status("PartiallyFilled") == OrderStatus.PARTIALLY_FILLED

    def test_cancelled(self):
        assert map_bybit_status("Cancelled") == OrderStatus.CANCELLED

    def test_partially_filled_canceled(self):
        # Частично исполнен затем отменён — трактуем как PARTIALLY_FILLED
        assert map_bybit_status("PartiallyFilledCanceled") == OrderStatus.PARTIALLY_FILLED

    def test_rejected(self):
        assert map_bybit_status("Rejected") == OrderStatus.REJECTED

    def test_new_is_pending(self):
        assert map_bybit_status("New") == OrderStatus.PENDING

    def test_created_is_pending(self):
        assert map_bybit_status("Created") == OrderStatus.PENDING

    def test_unknown_string_returns_unknown(self):
        assert map_bybit_status("SomeFutureStatus") == OrderStatus.UNKNOWN

    def test_empty_string_returns_unknown(self):
        assert map_bybit_status("") == OrderStatus.UNKNOWN

    def test_all_map_entries_are_valid_statuses(self):
        """Все значения в BYBIT_STATUS_MAP — валидные OrderStatus."""
        valid = set(OrderStatus)
        for bybit_str, status in BYBIT_STATUS_MAP.items():
            assert status in valid, f"{bybit_str!r} → {status!r} не в OrderStatus"
