"""
Пункт 2: Фабрика провайдеров рыночных данных.

Единственная точка где bot.py знает о конкретных реализациях провайдеров.
Вся остальная бизнес-логика работает только с MarketDataProvider (ABC).

Поддерживаемые значения MARKET_PROVIDER:
  bybit  — BybitProvider (REST MVP, WS в доработках)
  mock   — MockProvider  (для тестов и разработки без биржи)
"""

import logging
from typing import Optional

from config.settings import MarketSettings
from market_data.bybit_provider import BybitProvider
from market_data.market_data import MarketDataProvider
from market_data.mock_provider import ExhaustedBehavior, MockProvider, make_price

logger = logging.getLogger(__name__)

# Реестр поддерживаемых провайдеров
_SUPPORTED = ("bybit", "mock")


class ProviderFactory:
    """
    Фабрика провайдеров рыночных данных.

    Использование в bot.py:
        settings = AppSettings()
        provider = ProviderFactory.create(settings.market)
        provider.start()
        provider.subscribe(settings.market.ticker)
    """

    @staticmethod
    def create(
        settings: MarketSettings,
        mock_price: Optional[float] = None,
    ) -> MarketDataProvider:
        """
        Создать провайдер по конфигурации.

        Args:
            settings:    MarketSettings из AppSettings.
            mock_price:  Начальная цена для MockProvider (опционально).
                         Если не задана — MockProvider стартует с пустой очередью.

        Returns:
            Экземпляр MarketDataProvider. Не запущен — вызови start() самостоятельно.

        Raises:
            ValueError: неизвестный провайдер в MARKET_PROVIDER.
        """
        provider_name = settings.provider.lower().strip()

        if provider_name == "bybit":
            return ProviderFactory._create_bybit(settings)

        if provider_name == "mock":
            return ProviderFactory._create_mock(settings, mock_price)

        raise ValueError(
            f"Неизвестный провайдер: {settings.provider!r}. "
            f"Допустимые значения: {_SUPPORTED}."
        )

    # ------------------------------------------------------------------
    # Private builders
    # ------------------------------------------------------------------

    @staticmethod
    def _create_bybit(settings: MarketSettings) -> BybitProvider:
        logger.info(
            "ProviderFactory: создаём BybitProvider "
            "(ticker=%s, poll_interval=%ds, REST-only MVP).",
            settings.ticker, settings.poll_interval_sec,
        )
        return BybitProvider(
            poll_interval_sec=settings.poll_interval_sec,
            stale_threshold_sec=settings.stale_threshold_sec,
            reconnect_delay_sec=float(settings.reconnect_delay_sec),
            max_reconnect_sec=float(settings.max_reconnect_sec),
            spike_threshold_pct=float(settings.spike_threshold_pct),
            max_spread_pct=float(settings.max_spread_pct),
        )

    @staticmethod
    def _create_mock(
        settings: MarketSettings,
        initial_price: Optional[float],
    ) -> MockProvider:
        logger.info(
            "ProviderFactory: создаём MockProvider (ticker=%s).",
            settings.ticker,
        )
        provider = MockProvider(
            exhausted_behavior=ExhaustedBehavior.LAST,
            auto_subscribe=True,
        )
        if initial_price is not None:
            price = make_price(settings.ticker, last=initial_price)
            provider.set_price(settings.ticker, price)
            logger.info(
                "ProviderFactory: MockProvider инициализирован с ценой %.6f.",
                initial_price,
            )
        return provider
