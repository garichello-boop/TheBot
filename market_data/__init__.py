"""
market_data — подсистема получения рыночных данных (Пункт 2).

Публичный API:
    MarketDataProvider  — абстрактный интерфейс (бот работает только с ним)
    PriceData           — структура ценового снапшота
    ProviderStatus      — состояния провайдера
    PriceSource         — источник данных (WS / REST)
    MarketDataUnavailable, TickerNotSubscribed, ProviderNotStarted — исключения

    BybitProvider       — реализация для Bybit (REST MVP)
    MockProvider        — тестовый провайдер
    make_price          — хелпер для создания PriceData в тестах

    ProviderFactory     — фабрика: создаёт провайдер по MarketSettings
"""

from market_data.market_data import (
    MarketDataProvider,
    MarketDataUnavailable,
    PriceData,
    PriceSource,
    ProviderNotStarted,
    ProviderStatus,
    TickerNotSubscribed,
)
from market_data.bybit_provider import BybitProvider
from market_data.mock_provider import ExhaustedBehavior, MockProvider, make_price
from market_data.factory import ProviderFactory

__all__ = [
    # Core abstractions
    "MarketDataProvider",
    "PriceData",
    "ProviderStatus",
    "PriceSource",
    # Exceptions
    "MarketDataUnavailable",
    "TickerNotSubscribed",
    "ProviderNotStarted",
    # Implementations
    "BybitProvider",
    "MockProvider",
    "ExhaustedBehavior",
    "make_price",
    # Factory
    "ProviderFactory",
]
