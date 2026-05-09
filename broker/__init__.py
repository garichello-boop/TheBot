"""
broker/ — подсистема взаимодействия с брокером/биржей (Пункт 4).

Публичный API пакета. Остальные модули системы импортируют только отсюда,
не из конкретных файлов пакета. Исключение — BotLoop, который делает
isinstance(broker, PaperBroker) для вызова process_market_tick().

Типичные импорты:

    from broker import IBroker, OrderRequest, BrokerFactory, BrokerBundle
    from broker import BrokerTimeout, InsufficientFundsError
    from broker import OrderNormalizer, NormalizeResult
"""

# Интерфейс и исключения
from broker.broker import (
    BrokerError,
    BrokerNetworkError,
    BrokerRejected,
    BrokerTimeout,
    IBroker,
    InsufficientFundsError,
    OrderNotFoundError,
)

# Модели данных
from broker.models import (
    Balance,
    BrokerMode,
    MarketInfo,
    NormalizeResult,
    OpenOrder,
    OrderCreated,
    OrderFill,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    SkipReason,
)

# Нормализатор
from broker.normalizer import OrderNormalizer

# Адаптеры бирж
from broker.exchange_adapter import (
    BybitExchangeAdapter,
    ExchangeAdapter,
    NoClientOrderIdAdapter,
)

# Трекер ордеров
from broker.order_tracker import (
    BybitOrderTracker,
    OrderTracker,
    map_bybit_status,
)

# Конкретные брокеры (нужны для isinstance в BotLoop)
from broker.bybit_broker import BybitBroker
from broker.paper_broker import PaperBroker

# Фабрика
from broker.broker_factory import BrokerBundle, BrokerFactory

__all__ = [
    "IBroker",
    "BrokerError",
    "BrokerTimeout",
    "BrokerRejected",
    "BrokerNetworkError",
    "InsufficientFundsError",
    "OrderNotFoundError",
    "OrderRequest",
    "OrderCreated",
    "OrderFill",
    "OpenOrder",
    "Balance",
    "MarketInfo",
    "NormalizeResult",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "BrokerMode",
    "SkipReason",
    "OrderNormalizer",
    "ExchangeAdapter",
    "BybitExchangeAdapter",
    "NoClientOrderIdAdapter",
    "OrderTracker",
    "BybitOrderTracker",
    "map_bybit_status",
    "PaperBroker",
    "BybitBroker",
    "BrokerFactory",
    "BrokerBundle",
]
