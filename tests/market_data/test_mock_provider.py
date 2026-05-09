"""
Тесты: mock_provider.py — MockProvider, make_price, ExhaustedBehavior.
"""

import time
from decimal import Decimal

import pytest

from market_data.market_data import (
    MarketDataUnavailable,
    PriceSource,
    ProviderNotStarted,
    ProviderStatus,
    TickerNotSubscribed,
)
from market_data.mock_provider import ExhaustedBehavior, MockProvider, make_price


# ---------------------------------------------------------------------------
# make_price helper
# ---------------------------------------------------------------------------

class TestMakePrice:
    def test_last_set_correctly(self):
        p = make_price("BTCUSDT", last=50000.0)
        assert p.last == Decimal("50000.0")
        assert p.ticker == "BTCUSDT"

    def test_bid_ask_auto_calculated(self):
        """Без явного bid/ask — рассчитываются из last с маленьким спредом."""
        p = make_price("BTCUSDT", last=50000.0)
        assert p.bid < p.last
        assert p.ask > p.last
        assert p.bid > Decimal("0")

    def test_explicit_bid_ask(self):
        p = make_price("BTCUSDT", last=50000.0, bid=49900.0, ask=50100.0)
        assert p.bid == Decimal("49900.0")
        assert p.ask == Decimal("50100.0")

    def test_ticker_uppercased(self):
        p = make_price("btcusdt", last=100.0)
        assert p.ticker == "BTCUSDT"

    def test_wide_spread_flag(self):
        p = make_price("BTCUSDT", last=50000.0, wide_spread=True)
        assert p.wide_spread is True

    def test_source_default_rest(self):
        p = make_price("BTCUSDT", last=50000.0)
        assert p.source == PriceSource.REST

    def test_custom_timestamp(self):
        ts = time.time() - 100
        p  = make_price("BTCUSDT", last=50000.0, timestamp=ts)
        assert abs(p.timestamp - ts) < 0.001


# ---------------------------------------------------------------------------
# MockProvider — жизненный цикл
# ---------------------------------------------------------------------------

class TestMockProviderLifecycle:
    def test_not_started_raises_on_subscribe(self):
        p = MockProvider()
        with pytest.raises(ProviderNotStarted):
            p.subscribe("BTCUSDT")

    def test_not_started_raises_on_get_price(self):
        p = MockProvider()
        with pytest.raises(ProviderNotStarted):
            p.get_price("BTCUSDT")

    def test_start_stop(self):
        p = MockProvider()
        p.start()
        p.stop()  # не должно падать

    def test_start_idempotent(self):
        p = MockProvider()
        p.start()
        p.start()
        p.stop()

    def test_stop_idempotent(self):
        p = MockProvider()
        p.start()
        p.stop()
        p.stop()


# ---------------------------------------------------------------------------
# MockProvider — фиксированная цена
# ---------------------------------------------------------------------------

class TestFixedPrice:
    def setup_method(self):
        self.provider = MockProvider()
        self.provider.start()
        self.price = make_price("BTCUSDT", last=50000.0)
        self.provider.set_price("BTCUSDT", self.price)

    def teardown_method(self):
        self.provider.stop()

    def test_get_price_returns_set_price(self):
        result = self.provider.get_price("BTCUSDT")
        assert result.last == Decimal("50000.0")

    def test_repeated_calls_return_same_price(self):
        p1 = self.provider.get_price("BTCUSDT")
        p2 = self.provider.get_price("BTCUSDT")
        assert p1 == p2

    def test_auto_subscribe_on_set_price(self):
        """set_price с auto_subscribe=True автоматически подписывает тикер."""
        # subscribe уже произошёл в set_price — get_price не бросает TickerNotSubscribed
        result = self.provider.get_price("BTCUSDT")
        assert result is not None

    def test_ticker_not_subscribed_raises(self):
        p = MockProvider(auto_subscribe=False)
        p.start()
        p.subscribe("BTCUSDT")
        try:
            with pytest.raises(TickerNotSubscribed):
                p.get_price("ETHUSDT")
        finally:
            p.stop()


# ---------------------------------------------------------------------------
# MockProvider — очередь цен
# ---------------------------------------------------------------------------

class TestPriceQueue:
    def test_feed_returns_prices_in_order(self):
        prices = [
            make_price("BTCUSDT", last=50000.0),
            make_price("BTCUSDT", last=51000.0),
            make_price("BTCUSDT", last=49000.0),
        ]
        p = MockProvider()
        p.feed("BTCUSDT", prices)
        p.start()
        try:
            assert p.get_price("BTCUSDT").last == Decimal("50000.0")
            assert p.get_price("BTCUSDT").last == Decimal("51000.0")
            assert p.get_price("BTCUSDT").last == Decimal("49000.0")
        finally:
            p.stop()

    def test_feed_multiple_times_appends(self):
        p = MockProvider()
        p.feed("BTCUSDT", [make_price("BTCUSDT", last=100.0)])
        p.feed("BTCUSDT", [make_price("BTCUSDT", last=200.0)])
        p.start()
        try:
            assert p.get_price("BTCUSDT").last == Decimal("100.0")
            assert p.get_price("BTCUSDT").last == Decimal("200.0")
        finally:
            p.stop()

    def test_queue_size_decreases(self):
        prices = [make_price("BTCUSDT", last=float(i)) for i in range(1, 4)]
        p = MockProvider()
        p.feed("BTCUSDT", prices)
        p.start()
        try:
            assert p.queue_size("BTCUSDT") == 3
            p.get_price("BTCUSDT")
            assert p.queue_size("BTCUSDT") == 2
            p.get_price("BTCUSDT")
            assert p.queue_size("BTCUSDT") == 1
        finally:
            p.stop()


# ---------------------------------------------------------------------------
# ExhaustedBehavior
# ---------------------------------------------------------------------------

class TestExhaustedBehavior:
    def _provider(self, behavior: ExhaustedBehavior) -> MockProvider:
        p = MockProvider(exhausted_behavior=behavior)
        p.feed("BTCUSDT", [make_price("BTCUSDT", last=50000.0)])
        p.start()
        return p

    def test_last_behavior_returns_last_price(self):
        p = self._provider(ExhaustedBehavior.LAST)
        try:
            p.get_price("BTCUSDT")         # из очереди (50000)
            result = p.get_price("BTCUSDT") # очередь пуста → вернуть последнюю
            assert result.last == Decimal("50000.0")
        finally:
            p.stop()

    def test_raise_behavior_raises_when_empty(self):
        p = self._provider(ExhaustedBehavior.RAISE)
        try:
            p.get_price("BTCUSDT")  # из очереди
            with pytest.raises(MarketDataUnavailable):
                p.get_price("BTCUSDT")  # очередь пуста → raise
        finally:
            p.stop()

    def test_default_behavior_returns_default(self):
        default = make_price("BTCUSDT", last=99999.0)
        p = MockProvider(exhausted_behavior=ExhaustedBehavior.DEFAULT)
        p.feed("BTCUSDT", [make_price("BTCUSDT", last=50000.0)])
        p.set_price("BTCUSDT", default)
        p.start()
        try:
            p.get_price("BTCUSDT")          # из очереди
            result = p.get_price("BTCUSDT") # пусто → дефолт
            assert result.last == Decimal("99999.0")
        finally:
            p.stop()

    def test_raise_behavior_no_prices_at_all(self):
        """Если цен не было вообще и RAISE — сразу исключение."""
        p = MockProvider(exhausted_behavior=ExhaustedBehavior.RAISE, auto_subscribe=True)
        p.set_price("BTCUSDT", make_price("BTCUSDT", last=1.0))
        # Сбрасываем через clear и убираем дефолт вручную
        p = MockProvider(exhausted_behavior=ExhaustedBehavior.RAISE)
        p.start()
        p.subscribe("BTCUSDT")
        try:
            with pytest.raises(MarketDataUnavailable):
                p.get_price("BTCUSDT")
        finally:
            p.stop()


# ---------------------------------------------------------------------------
# Симуляция FAILED статуса
# ---------------------------------------------------------------------------

class TestFailedStatus:
    def test_failed_status_raises_market_data_unavailable(self):
        p = MockProvider()
        p.set_price("BTCUSDT", make_price("BTCUSDT", last=50000.0))
        p.start()
        p.set_status(ProviderStatus.FAILED)
        try:
            with pytest.raises(MarketDataUnavailable) as exc_info:
                p.get_price("BTCUSDT")
            assert exc_info.value.status == ProviderStatus.FAILED
        finally:
            p.stop()

    def test_status_recovery(self):
        """После восстановления статуса get_price снова работает."""
        p = MockProvider()
        p.set_price("BTCUSDT", make_price("BTCUSDT", last=50000.0))
        p.start()
        p.set_status(ProviderStatus.FAILED)
        p.set_status(ProviderStatus.CONNECTED)
        try:
            result = p.get_price("BTCUSDT")
            assert result.last == Decimal("50000.0")
        finally:
            p.stop()


# ---------------------------------------------------------------------------
# Счётчики и утилиты
# ---------------------------------------------------------------------------

class TestCountersAndUtils:
    def test_call_count_increments(self):
        p = MockProvider()
        p.set_price("BTCUSDT", make_price("BTCUSDT", last=50000.0))
        p.start()
        try:
            assert p.call_count("BTCUSDT") == 0
            p.get_price("BTCUSDT")
            assert p.call_count("BTCUSDT") == 1
            p.get_price("BTCUSDT")
            assert p.call_count("BTCUSDT") == 2
        finally:
            p.stop()

    def test_clear_single_ticker(self):
        p = MockProvider()
        p.feed("BTCUSDT", [make_price("BTCUSDT", last=1.0)])
        p.feed("ETHUSDT", [make_price("ETHUSDT", last=2.0)])
        p.start()
        p.clear("BTCUSDT")
        assert p.queue_size("BTCUSDT") == 0
        assert p.queue_size("ETHUSDT") == 1
        p.stop()

    def test_clear_all(self):
        p = MockProvider()
        p.feed("BTCUSDT", [make_price("BTCUSDT", last=1.0)])
        p.feed("ETHUSDT", [make_price("ETHUSDT", last=2.0)])
        p.start()
        p.clear()
        assert p.queue_size("BTCUSDT") == 0
        assert p.queue_size("ETHUSDT") == 0
        p.stop()

    def test_get_status_returns_current(self):
        p = MockProvider()
        p.start()
        assert p.get_status() == ProviderStatus.CONNECTED
        p.set_status(ProviderStatus.STALE)
        assert p.get_status() == ProviderStatus.STALE
        p.stop()

    def test_unsubscribe_removes_ticker(self):
        p = MockProvider(auto_subscribe=False)
        p.set_price("BTCUSDT", make_price("BTCUSDT", last=50000.0))
        p.start()
        p.subscribe("BTCUSDT")
        p.unsubscribe("BTCUSDT")
        try:
            with pytest.raises(TickerNotSubscribed):
                p.get_price("BTCUSDT")
        finally:
            p.stop()
