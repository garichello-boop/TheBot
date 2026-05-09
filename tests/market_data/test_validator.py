"""
Тесты: validator.py — PriceValidator.
"""

import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from market_data.market_data import PriceData, PriceSource
from market_data.mock_provider import make_price
from market_data.validator import (
    PriceValidator,
    ValidationOutcome,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def validator() -> PriceValidator:
    return PriceValidator(
        spike_threshold_pct=10.0,
        max_spread_pct=1.0,
        stale_threshold_sec=30,
        rest_fetcher=None,
    )


def _price(last: float, bid: float = None, ask: float = None, ts: float = None) -> PriceData:
    return make_price("BTCUSDT", last=last, bid=bid, ask=ask, timestamp=ts or time.time())


# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------

class TestValidatorInit:
    def test_negative_spike_threshold_raises(self):
        with pytest.raises(ValueError, match="spike_threshold_pct"):
            PriceValidator(spike_threshold_pct=-1.0, max_spread_pct=1.0, stale_threshold_sec=30)

    def test_zero_spike_threshold_raises(self):
        with pytest.raises(ValueError, match="spike_threshold_pct"):
            PriceValidator(spike_threshold_pct=0.0, max_spread_pct=1.0, stale_threshold_sec=30)

    def test_negative_stale_threshold_raises(self):
        with pytest.raises(ValueError, match="stale_threshold_sec"):
            PriceValidator(spike_threshold_pct=10.0, max_spread_pct=1.0, stale_threshold_sec=-1)


# ---------------------------------------------------------------------------
# Нормальное принятие цены
# ---------------------------------------------------------------------------

class TestAccepted:
    def test_valid_price_accepted(self, validator):
        result = validator.validate(_price(50000), last_price=None)
        assert result.accepted is True
        assert result.outcome  == ValidationOutcome.ACCEPTED

    def test_no_last_price_accepted(self, validator):
        result = validator.validate(_price(50000), last_price=None)
        assert result.accepted is True
        assert result.spike_detected is False

    def test_small_change_accepted(self, validator):
        """5% изменение — меньше порога 10%."""
        result = validator.validate(_price(52500), last_price=_price(50000))
        assert result.accepted is True
        assert result.spike_detected is False

    def test_wide_spread_flag_set(self):
        v = PriceValidator(spike_threshold_pct=10.0, max_spread_pct=0.1, stale_threshold_sec=30)
        p = PriceData(
            ticker="BTCUSDT",
            bid=Decimal("49000"),
            ask=Decimal("51000"),
            last=Decimal("50000"),
            timestamp=time.time(),
            source=PriceSource.REST,
        )
        result = v.validate(p, last_price=None)
        assert result.accepted    is True
        assert result.wide_spread is True

    def test_normal_spread_no_flag(self, validator):
        result = validator.validate(_price(50000), last_price=None)
        assert result.wide_spread is False


# ---------------------------------------------------------------------------
# Санитарные проверки
# ---------------------------------------------------------------------------

class TestSanityRejection:
    """
    PriceData — frozen dataclass со __slots__, mock.patch.object не работает.
    Тестируем _check_sanity напрямую через MagicMock с нужными значениями полей.
    """

    def test_nan_bid_rejected(self, validator):
        mock_price = MagicMock()
        mock_price.bid  = Decimal("NaN")
        mock_price.ask  = Decimal("50100")
        mock_price.last = Decimal("50000")

        result = validator._check_sanity(mock_price)
        assert result is not None
        assert result.accepted is False
        assert result.outcome  == ValidationOutcome.REJECTED_INVALID
        assert "bid" in result.reason

    def test_inf_ask_rejected(self, validator):
        mock_price = MagicMock()
        mock_price.bid  = Decimal("49900")
        mock_price.ask  = Decimal("Infinity")
        mock_price.last = Decimal("50000")

        result = validator._check_sanity(mock_price)
        assert result is not None
        assert result.accepted is False
        assert result.outcome  == ValidationOutcome.REJECTED_INVALID
        assert "ask" in result.reason

    def test_nan_last_rejected(self, validator):
        mock_price = MagicMock()
        mock_price.bid  = Decimal("49900")
        mock_price.ask  = Decimal("50100")
        mock_price.last = Decimal("NaN")

        result = validator._check_sanity(mock_price)
        assert result is not None
        assert result.accepted is False
        assert result.outcome  == ValidationOutcome.REJECTED_INVALID

    def test_valid_price_passes_sanity(self, validator):
        """Корректная цена возвращает None (нет ошибок)."""
        mock_price = MagicMock()
        mock_price.bid  = Decimal("49900")
        mock_price.ask  = Decimal("50100")
        mock_price.last = Decimal("50000")

        result = validator._check_sanity(mock_price)
        assert result is None


# ---------------------------------------------------------------------------
# Проверка свежести (stale)
# ---------------------------------------------------------------------------

class TestStaleRejection:
    def test_stale_timestamp_rejected(self, validator):
        p = _price(50000, ts=time.time() - 60)  # 60 сек назад, порог 30
        result = validator.validate(p, last_price=None, now_ts=time.time())
        assert result.accepted is False
        assert result.outcome  == ValidationOutcome.REJECTED_STALE

    def test_fresh_timestamp_accepted(self, validator):
        now_ts = time.time()
        p      = _price(50000, ts=now_ts - 10)  # 10 сек назад, порог 30
        result = validator.validate(p, last_price=None, now_ts=now_ts)
        assert result.accepted is True

    def test_no_now_ts_skips_stale_check(self, validator):
        p = _price(50000, ts=time.time() - 9999)
        result = validator.validate(p, last_price=None, now_ts=None)
        assert result.accepted is True


# ---------------------------------------------------------------------------
# Скачок — без REST-верификатора
# ---------------------------------------------------------------------------

class TestSpikeNoRestFetcher:
    def test_spike_rejected_without_fetcher(self, validator):
        result = validator.validate(_price(60000), last_price=_price(50000))
        assert result.accepted       is False
        assert result.spike_detected is True
        assert result.outcome        == ValidationOutcome.SPIKE_REST_FAILED

    def test_no_spike_below_threshold(self, validator):
        result = validator.validate(_price(54000), last_price=_price(50000))  # +8%
        assert result.accepted       is True
        assert result.spike_detected is False

    def test_spike_exactly_at_threshold(self, validator):
        result = validator.validate(_price(55000), last_price=_price(50000))  # +10%
        assert result.spike_detected is True

    def test_spike_downward_detected(self, validator):
        result = validator.validate(_price(40000), last_price=_price(50000))  # -20%
        assert result.spike_detected is True


# ---------------------------------------------------------------------------
# Скачок — с REST-верификатором
# ---------------------------------------------------------------------------

class TestSpikeWithRestFetcher:
    def _v(self, rest_return) -> PriceValidator:
        return PriceValidator(
            spike_threshold_pct=10.0,
            max_spread_pct=1.0,
            stale_threshold_sec=30,
            rest_fetcher=MagicMock(return_value=rest_return),
        )

    def test_spike_confirmed_by_rest(self):
        v = self._v(rest_return=_price(60000))
        result = v.validate(_price(60000), last_price=_price(50000))
        assert result.accepted       is True
        assert result.outcome        == ValidationOutcome.SPIKE_CONFIRMED
        assert result.spike_detected is True

    def test_spike_unconfirmed_by_rest(self):
        v = self._v(rest_return=_price(50100))  # REST показывает старую цену
        result = v.validate(_price(60000), last_price=_price(50000))
        assert result.accepted is False
        assert result.outcome  == ValidationOutcome.SPIKE_UNCONFIRMED

    def test_rest_returns_none(self):
        v = self._v(rest_return=None)
        result = v.validate(_price(60000), last_price=_price(50000))
        assert result.accepted is False
        assert result.outcome  == ValidationOutcome.SPIKE_REST_FAILED

    def test_rest_raises_exception(self):
        v = PriceValidator(
            spike_threshold_pct=10.0,
            max_spread_pct=1.0,
            stale_threshold_sec=30,
            rest_fetcher=MagicMock(side_effect=ConnectionError("timeout")),
        )
        result = v.validate(_price(60000), last_price=_price(50000))
        assert result.accepted is False
        assert result.outcome  == ValidationOutcome.SPIKE_REST_FAILED

    def test_spike_pct_in_result(self):
        v = self._v(rest_return=_price(60000))
        result = v.validate(_price(60000), last_price=_price(50000))
        assert abs(result.spike_pct - 20.0) < 0.01
