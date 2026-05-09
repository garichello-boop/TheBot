"""
Тесты: market_data.py — PriceData, ProviderStatus, исключения.
"""

import time
from decimal import Decimal

import pytest

from market_data.market_data import (
    MarketDataUnavailable,
    PriceData,
    PriceSource,
    ProviderNotStarted,
    ProviderStatus,
    TickerNotSubscribed,
)


# ---------------------------------------------------------------------------
# PriceData — создание
# ---------------------------------------------------------------------------

class TestPriceDataCreation:
    def _valid(self, **kwargs) -> PriceData:
        defaults = dict(
            ticker="BTCUSDT",
            bid=Decimal("49900"),
            ask=Decimal("50100"),
            last=Decimal("50000"),
            timestamp=time.time(),
            source=PriceSource.REST,
        )
        defaults.update(kwargs)
        return PriceData(**defaults)

    def test_valid_price_created(self):
        p = self._valid()
        assert p.ticker == "BTCUSDT"
        assert p.bid    == Decimal("49900")
        assert p.ask    == Decimal("50100")
        assert p.last   == Decimal("50000")
        assert p.source == PriceSource.REST
        assert p.wide_spread is False

    def test_wide_spread_default_false(self):
        p = self._valid()
        assert p.wide_spread is False

    def test_wide_spread_can_be_set(self):
        p = self._valid(wide_spread=True)
        assert p.wide_spread is True

    def test_frozen_immutable(self):
        p = self._valid()
        with pytest.raises((AttributeError, TypeError)):
            p.last = Decimal("99999")  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Validation errors
    # ------------------------------------------------------------------

    def test_zero_bid_raises(self):
        with pytest.raises(ValueError, match="bid"):
            self._valid(bid=Decimal("0"))

    def test_negative_bid_raises(self):
        with pytest.raises(ValueError, match="bid"):
            self._valid(bid=Decimal("-1"))

    def test_zero_ask_raises(self):
        with pytest.raises(ValueError, match="ask"):
            self._valid(ask=Decimal("0"))

    def test_zero_last_raises(self):
        with pytest.raises(ValueError, match="last"):
            self._valid(last=Decimal("0"))

    def test_ask_less_than_bid_raises(self):
        with pytest.raises(ValueError, match="ask"):
            self._valid(bid=Decimal("50100"), ask=Decimal("49900"))

    def test_zero_timestamp_raises(self):
        with pytest.raises(ValueError, match="timestamp"):
            self._valid(timestamp=0.0)

    def test_negative_timestamp_raises(self):
        with pytest.raises(ValueError, match="timestamp"):
            self._valid(timestamp=-1.0)


# ---------------------------------------------------------------------------
# PriceData — вычисляемые свойства
# ---------------------------------------------------------------------------

class TestPriceDataProperties:
    def _price(self, bid: float, ask: float, last: float) -> PriceData:
        return PriceData(
            ticker="BTCUSDT",
            bid=Decimal(str(bid)),
            ask=Decimal(str(ask)),
            last=Decimal(str(last)),
            timestamp=time.time(),
            source=PriceSource.WEBSOCKET,
        )

    def test_mid_is_average_of_bid_ask(self):
        p = self._price(bid=49000, ask=51000, last=50000)
        assert p.mid == Decimal("50000")

    def test_spread_pct_typical(self):
        # bid=49900, ask=50100, mid=50000, spread=200
        # spread_pct = 200 / 50000 * 100 = 0.4%
        p = self._price(bid=49900, ask=50100, last=50000)
        assert abs(float(p.spread_pct) - 0.4) < 0.001

    def test_spread_pct_zero_when_bid_equals_ask(self):
        p = self._price(bid=50000, ask=50000, last=50000)
        assert p.spread_pct == Decimal("0")


# ---------------------------------------------------------------------------
# ProviderStatus
# ---------------------------------------------------------------------------

class TestProviderStatus:
    def test_all_statuses_exist(self):
        statuses = {s.value for s in ProviderStatus}
        assert statuses == {"CONNECTED", "STALE", "RECONNECTING", "FALLBACK_REST", "FAILED"}

    def test_status_is_string(self):
        assert ProviderStatus.CONNECTED == "CONNECTED"


# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------

class TestExceptions:
    def test_market_data_unavailable_message(self):
        exc = MarketDataUnavailable(
            ticker="BTCUSDT",
            status=ProviderStatus.FAILED,
            reason="тест",
        )
        assert "BTCUSDT" in str(exc)
        assert "FAILED"  in str(exc)
        assert "тест"    in str(exc)
        assert exc.ticker  == "BTCUSDT"
        assert exc.status  == ProviderStatus.FAILED

    def test_market_data_unavailable_no_reason(self):
        exc = MarketDataUnavailable(ticker="ETH", status=ProviderStatus.STALE)
        assert "ETH" in str(exc)
        assert exc.reason == ""

    def test_ticker_not_subscribed_message(self):
        exc = TickerNotSubscribed("ETHUSDT")
        assert "ETHUSDT" in str(exc)
        assert exc.ticker == "ETHUSDT"

    def test_provider_not_started_is_exception(self):
        exc = ProviderNotStarted("не запущен")
        assert isinstance(exc, Exception)
