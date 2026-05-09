"""
Пункт 2: Получение рыночных данных
Абстрактный интерфейс, структуры данных, исключения.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ProviderStatus(str, Enum):
    """Состояние провайдера рыночных данных."""
    CONNECTED     = "CONNECTED"      # WS активен, данные свежие
    STALE         = "STALE"          # Данных нет дольше stale_threshold_sec
    RECONNECTING  = "RECONNECTING"   # WS упал, идёт переподключение
    FALLBACK_REST = "FALLBACK_REST"  # Работаем через REST
    FAILED        = "FAILED"         # Оба источника недоступны


class PriceSource(str, Enum):
    WEBSOCKET = "WEBSOCKET"
    REST      = "REST"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceData:
    """
    Снапшот цены инструмента.

    Три значения необходимы для корректного исполнения:
      bid  — по которой можно продать (лучший покупатель)
      ask  — по которой можно купить  (лучший продавец)
      last — цена последней сделки

    wide_spread выставляется провайдером когда (ask - bid) / mid > max_spread_pct.
    Используется DecisionEngine как сигнал осторожности, но не блокирует торговлю.
    """
    ticker:      str
    bid:         Decimal
    ask:         Decimal
    last:        Decimal
    timestamp:   float       # Unix timestamp получения (секунды)
    source:      PriceSource
    wide_spread: bool = field(default=False)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> Decimal:
        """Спред как процент от mid-цены."""
        if self.mid == 0:
            return Decimal("0")
        return (self.ask - self.bid) / self.mid * 100

    def __post_init__(self) -> None:
        if self.bid <= 0:
            raise ValueError(f"bid должен быть > 0, получено: {self.bid}")
        if self.ask <= 0:
            raise ValueError(f"ask должен быть > 0, получено: {self.ask}")
        if self.last <= 0:
            raise ValueError(f"last должен быть > 0, получено: {self.last}")
        if self.ask < self.bid:
            raise ValueError(f"ask ({self.ask}) не может быть меньше bid ({self.bid})")
        if self.timestamp <= 0:
            raise ValueError(f"timestamp должен быть > 0, получено: {self.timestamp}")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MarketDataUnavailable(Exception):
    """
    Выбрасывается get_price() когда оба источника (WS и REST) недоступны.
    Бот должен пропустить тик и дождаться восстановления.
    """
    def __init__(self, ticker: str, status: ProviderStatus, reason: str = ""):
        self.ticker = ticker
        self.status = status
        self.reason = reason
        super().__init__(
            f"Рыночные данные недоступны для {ticker} "
            f"(статус: {status.value}){': ' + reason if reason else ''}"
        )


class ProviderNotStarted(Exception):
    """Провайдер не был запущен через start() перед использованием."""
    pass


class TickerNotSubscribed(Exception):
    """Попытка получить цену по тикеру без предварительной подписки."""
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"Тикер {ticker!r} не подписан. Вызовите subscribe() сначала.")


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class MarketDataProvider(ABC):
    """
    Абстрактный интерфейс провайдера рыночных данных.

    Бот работает только с этим интерфейсом — конкретная реализация
    (Bybit, OKX, Mock) скрыта за ним. Переключение биржи = одна строка в конфиге.

    Порядок использования:
        provider.start()
        provider.subscribe("BTCUSDT")
        price = provider.get_price("BTCUSDT")   # в торговом цикле
        provider.stop()
    """

    @abstractmethod
    def subscribe(self, ticker: str) -> None:
        """
        Подписаться на обновления тикера.
        Должна вызываться до get_price().
        """

    @abstractmethod
    def unsubscribe(self, ticker: str) -> None:
        """
        Отписаться от тикера. Вызывать при смене инструмента или остановке.
        """

    @abstractmethod
    def get_price(self, ticker: str) -> PriceData:
        """
        Вернуть актуальную цену тикера.

        Raises:
            MarketDataUnavailable: оба источника (WS и REST) недоступны.
            TickerNotSubscribed:   тикер не был подписан.
            ProviderNotStarted:    start() не вызывался.
        """

    @abstractmethod
    def get_status(self) -> ProviderStatus:
        """Текущее состояние провайдера."""

    @abstractmethod
    def start(self) -> None:
        """
        Запустить провайдер: открыть WS-соединение, запустить Watchdog.
        Идемпотентен — повторный вызов не создаёт дублирующих соединений.
        """

    @abstractmethod
    def stop(self) -> None:
        """
        Корректно остановить провайдер: закрыть WS, остановить Watchdog.
        Идемпотентен.
        """

    def is_healthy(self) -> bool:
        """Провайдер в рабочем состоянии (данные доступны)."""
        return self.get_status() in (
            ProviderStatus.CONNECTED,
            ProviderStatus.FALLBACK_REST,
        )
