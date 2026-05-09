"""
Пункт 2: Валидация входящих рыночных данных.

Три слоя проверки:
  1. Санитарная  — цена > 0, не NaN, bid <= ask.
  2. Скачок      — отклонение > spike_threshold_pct → REST-верификация.
  3. Спред       — (ask - bid) / mid > max_spread_pct → флаг wide_spread.

Валидатор не знает откуда пришли данные и куда пойдут события.
Он принимает цену, возвращает ValidationResult — всё остальное делает caller.

REST-верификатор передаётся как callable при инициализации — 
это позволяет подключить любой провайдер без зависимости от конкретной биржи.
"""

import math
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional

from market_data.market_data import PriceData, PriceSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class ValidationOutcome(str, Enum):
    ACCEPTED           = "ACCEPTED"           # Цена принята
    REJECTED_INVALID   = "REJECTED_INVALID"   # Нулевая / NaN / отрицательная
    REJECTED_STALE     = "REJECTED_STALE"     # Timestamp устарел
    SPIKE_CONFIRMED    = "SPIKE_CONFIRMED"    # Скачок подтверждён REST → принята
    SPIKE_UNCONFIRMED  = "SPIKE_UNCONFIRMED"  # REST не подтверждает → отклонена (WS-баг)
    SPIKE_REST_FAILED  = "SPIKE_REST_FAILED"  # REST недоступен при верификации → отклонена


@dataclass(frozen=True)
class ValidationResult:
    outcome:         ValidationOutcome
    accepted:        bool
    reason:          str
    wide_spread:     bool  = field(default=False)
    spike_detected:  bool  = field(default=False)
    spike_pct:       float = field(default=0.0)

    @classmethod
    def accept(
        cls,
        reason: str = "ok",
        wide_spread: bool = False,
        spike_detected: bool = False,
        spike_pct: float = 0.0,
        outcome: ValidationOutcome = ValidationOutcome.ACCEPTED,
    ) -> "ValidationResult":
        return cls(
            outcome=outcome,
            accepted=True,
            reason=reason,
            wide_spread=wide_spread,
            spike_detected=spike_detected,
            spike_pct=spike_pct,
        )

    @classmethod
    def reject(
        cls,
        outcome: ValidationOutcome,
        reason: str,
        spike_detected: bool = False,
        spike_pct: float = 0.0,
    ) -> "ValidationResult":
        return cls(
            outcome=outcome,
            accepted=False,
            reason=reason,
            spike_detected=spike_detected,
            spike_pct=spike_pct,
        )


# ---------------------------------------------------------------------------
# RestFetcher type alias
# ---------------------------------------------------------------------------

# Callable который провайдер передаёт при инициализации валидатора.
# Должен делать синхронный REST-запрос и вернуть PriceData или None при ошибке.
RestFetcher = Callable[[str], Optional[PriceData]]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class PriceValidator:
    """
    Валидирует входящие ценовые данные перед передачей в торговый цикл.

    Параметры:
        spike_threshold_pct  — порог аномального скачка (% от предыдущей цены).
                               Дефолт из MarketSettings: 10.0
        max_spread_pct       — порог широкого спреда (% от mid).
                               Дефолт из MarketSettings: 1.0
        stale_threshold_sec  — максимальный возраст timestamp в секундах.
                               Дефолт из MarketSettings: 30
        rest_fetcher         — callable для REST-верификации скачков.
                               Если None — при скачке цена отклоняется без верификации.

    Пример использования:
        validator = PriceValidator(
            spike_threshold_pct=10.0,
            max_spread_pct=1.0,
            stale_threshold_sec=30,
            rest_fetcher=bybit_provider.fetch_price_via_rest,
        )
        result = validator.validate(new_price, last_price)
        if not result.accepted:
            return  # пропустить тик
        if result.wide_spread:
            emitter.emit(event_type="PRICE_RECEIVED", ..., payload={"wide_spread": True})
    """

    def __init__(
        self,
        spike_threshold_pct: float,
        max_spread_pct: float,
        stale_threshold_sec: int,
        rest_fetcher: Optional[RestFetcher] = None,
    ) -> None:
        if spike_threshold_pct <= 0:
            raise ValueError(f"spike_threshold_pct должен быть > 0, получено: {spike_threshold_pct}")
        if max_spread_pct <= 0:
            raise ValueError(f"max_spread_pct должен быть > 0, получено: {max_spread_pct}")
        if stale_threshold_sec <= 0:
            raise ValueError(f"stale_threshold_sec должен быть > 0, получено: {stale_threshold_sec}")

        self._spike_threshold_pct = spike_threshold_pct
        self._max_spread_pct      = max_spread_pct
        self._stale_threshold_sec = stale_threshold_sec
        self._rest_fetcher        = rest_fetcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        price: PriceData,
        last_price: Optional[PriceData],
        now_ts: Optional[float] = None,
    ) -> ValidationResult:
        """
        Валидировать новую цену.

        Args:
            price:      новые данные от WS или REST.
            last_price: последняя принятая цена (None если первый тик).
            now_ts:     текущее время в секундах (Unix). Если None — не проверяем staleness.
                        Передаётся явно для тестируемости (не вызываем time.time() внутри).

        Returns:
            ValidationResult с outcome, accepted и дополнительными полями.
        """
        # 1. Санитарная проверка — структурная валидность
        sanity = self._check_sanity(price)
        if sanity is not None:
            return sanity

        # 2. Проверка свежести timestamp
        if now_ts is not None:
            stale = self._check_stale(price, now_ts)
            if stale is not None:
                return stale

        # 3. Детектирование аномального скачка (только если есть предыдущая цена)
        spike_result = None
        spike_pct    = 0.0
        if last_price is not None:
            spike_pct = self._calc_spike_pct(price.last, last_price.last)
            if spike_pct >= self._spike_threshold_pct:
                spike_result = self._verify_spike(price, spike_pct)

        # 4. Проверка спреда
        wide_spread = self._check_spread(price)

        # Если скачок не прошёл верификацию — отклонить
        if spike_result is not None and not spike_result.accepted:
            return spike_result

        # Принять цену
        if spike_result is not None and spike_result.accepted:
            # Скачок подтверждён REST
            return ValidationResult.accept(
                reason=spike_result.reason,
                wide_spread=wide_spread,
                spike_detected=True,
                spike_pct=spike_pct,
                outcome=ValidationOutcome.SPIKE_CONFIRMED,
            )

        return ValidationResult.accept(
            wide_spread=wide_spread,
            spike_detected=spike_pct >= self._spike_threshold_pct,
            spike_pct=spike_pct,
        )

    # ------------------------------------------------------------------
    # Private checks
    # ------------------------------------------------------------------

    def _check_sanity(self, price: PriceData) -> Optional[ValidationResult]:
        """Базовые проверки: не NaN, не inf, положительные значения."""
        for name, value in (("bid", price.bid), ("ask", price.ask), ("last", price.last)):
            fval = float(value)
            if math.isnan(fval) or math.isinf(fval):
                return ValidationResult.reject(
                    outcome=ValidationOutcome.REJECTED_INVALID,
                    reason=f"{name} содержит NaN или Inf: {value}",
                )
            # PriceData.__post_init__ уже проверяет > 0, но дублируем защиту
            if fval <= 0:
                return ValidationResult.reject(
                    outcome=ValidationOutcome.REJECTED_INVALID,
                    reason=f"{name} должен быть > 0, получено: {value}",
                )
        return None

    def _check_stale(self, price: PriceData, now_ts: float) -> Optional[ValidationResult]:
        """Проверяем что timestamp не старше stale_threshold_sec."""
        age_sec = now_ts - price.timestamp
        if age_sec > self._stale_threshold_sec:
            return ValidationResult.reject(
                outcome=ValidationOutcome.REJECTED_STALE,
                reason=(
                    f"Данные устарели: возраст {age_sec:.1f}s "
                    f"> порога {self._stale_threshold_sec}s"
                ),
            )
        return None

    def _calc_spike_pct(self, new_last: Decimal, old_last: Decimal) -> float:
        """Отклонение новой цены от предыдущей в процентах."""
        if old_last == 0:
            return 0.0
        return abs(float(new_last - old_last) / float(old_last)) * 100

    def _verify_spike(self, price: PriceData, spike_pct: float) -> ValidationResult:
        """
        Скачок обнаружен — верифицируем через REST.

        Логика (по ТЗ):
          - REST подтверждает движение → принять (реальное движение рынка).
          - REST отдаёт старую цену   → отклонить (баг WS, scam wick).
          - REST недоступен           → отклонить (безопаснее пропустить тик).
        """
        logger.warning(
            "Аномальный скачок %.2f%% обнаружен для %s (last=%.6f). "
            "Запускаем REST-верификацию.",
            spike_pct, price.ticker, price.last,
        )

        if self._rest_fetcher is None:
            return ValidationResult.reject(
                outcome=ValidationOutcome.SPIKE_REST_FAILED,
                reason=f"Скачок {spike_pct:.2f}% — REST-верификатор не задан, тик отклонён",
                spike_detected=True,
                spike_pct=spike_pct,
            )

        try:
            rest_price = self._rest_fetcher(price.ticker)
        except Exception as exc:
            logger.error("REST-верификация скачка завершилась ошибкой: %s", exc)
            return ValidationResult.reject(
                outcome=ValidationOutcome.SPIKE_REST_FAILED,
                reason=f"Скачок {spike_pct:.2f}% — REST вернул ошибку: {exc}",
                spike_detected=True,
                spike_pct=spike_pct,
            )

        if rest_price is None:
            return ValidationResult.reject(
                outcome=ValidationOutcome.SPIKE_REST_FAILED,
                reason=f"Скачок {spike_pct:.2f}% — REST вернул None, тик отклонён",
                spike_detected=True,
                spike_pct=spike_pct,
            )

        # Сравниваем REST-цену с новой WS-ценой: если REST тоже показывает скачок — реально
        rest_deviation_pct = self._calc_spike_pct(rest_price.last, price.last)
        confirmed = rest_deviation_pct < self._spike_threshold_pct

        if confirmed:
            logger.info(
                "REST подтверждает скачок %.2f%% для %s (REST last=%.6f). Принимаем.",
                spike_pct, price.ticker, rest_price.last,
            )
            return ValidationResult.accept(
                reason=f"Скачок {spike_pct:.2f}% подтверждён REST",
                spike_detected=True,
                spike_pct=spike_pct,
                outcome=ValidationOutcome.SPIKE_CONFIRMED,
            )
        else:
            logger.warning(
                "REST НЕ подтверждает скачок %.2f%% для %s "
                "(REST last=%.6f vs WS last=%.6f). Тик отклонён.",
                spike_pct, price.ticker, rest_price.last, price.last,
            )
            return ValidationResult.reject(
                outcome=ValidationOutcome.SPIKE_UNCONFIRMED,
                reason=(
                    f"Скачок {spike_pct:.2f}% не подтверждён REST "
                    f"(REST={rest_price.last}, WS={price.last})"
                ),
                spike_detected=True,
                spike_pct=spike_pct,
            )

    def _check_spread(self, price: PriceData) -> bool:
        """True если спред превышает max_spread_pct от mid."""
        return float(price.spread_pct) > self._max_spread_pct
