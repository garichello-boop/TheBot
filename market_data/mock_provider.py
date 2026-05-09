"""
Пункт 2: Тестовый провайдер рыночных данных.

MockProvider не подключается ни к какой бирже.
Используется в тестах и при разработке бизнес-логики без реального рынка.

Два режима работы:
  1. Фиксированная цена   — set_price(ticker, price_data). get_price() всегда
                            возвращает одно и то же. Удобно для простых юнит-тестов.

  2. Очередь цен          — feed(ticker, [price1, price2, ...]). get_price()
                            каждый раз отдаёт следующую цену из очереди.
                            При пустой очереди — поведение определяется режимом:
                              - вернуть последнюю (loop=True или exhausted_behavior=LAST)
                              - вернуть дефолт   (exhausted_behavior=DEFAULT)
                              - поднять исключение (exhausted_behavior=RAISE)

Вспомогательные методы:
  set_status()   — симулировать смену статуса провайдера (для тестов ошибок).
  clear()        — сбросить все цены и очереди.
  call_count()   — сколько раз вызывался get_price() для тикера.
"""

import logging
import threading
from collections import deque
from decimal import Decimal
from enum import Enum
from typing import Deque, Dict, List, Optional

from market_data.market_data import (
    MarketDataProvider,
    MarketDataUnavailable,
    PriceData,
    PriceSource,
    ProviderNotStarted,
    ProviderStatus,
    TickerNotSubscribed,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Поведение при исчерпании очереди
# ---------------------------------------------------------------------------

class ExhaustedBehavior(str, Enum):
    LAST    = "LAST"    # вернуть последнюю полученную цену
    DEFAULT = "DEFAULT" # вернуть дефолтную цену (если задана)
    RAISE   = "RAISE"   # поднять MarketDataUnavailable


# ---------------------------------------------------------------------------
# Фабричный метод для быстрого создания PriceData в тестах
# ---------------------------------------------------------------------------

def make_price(
    ticker:      str,
    last:        float,
    bid:         Optional[float] = None,
    ask:         Optional[float] = None,
    wide_spread: bool = False,
    source:      PriceSource = PriceSource.REST,
    timestamp:   Optional[float] = None,
) -> PriceData:
    """
    Удобный конструктор PriceData для тестов.

    Если bid/ask не заданы — рассчитываются из last с типичным спредом 0.01%.

    Пример:
        price = make_price("BTCUSDT", last=50000.0)
        price = make_price("BTCUSDT", last=50000.0, bid=49990.0, ask=50010.0)
    """
    import time as _time

    last_d = Decimal(str(last))

    if bid is None and ask is None:
        spread = last_d * Decimal("0.0001")  # 0.01%
        bid_d  = last_d - spread / 2
        ask_d  = last_d + spread / 2
    else:
        bid_d = Decimal(str(bid)) if bid is not None else last_d
        ask_d = Decimal(str(ask)) if ask is not None else last_d

    return PriceData(
        ticker=ticker.upper(),
        bid=bid_d,
        ask=ask_d,
        last=last_d,
        timestamp=timestamp if timestamp is not None else _time.time(),
        source=source,
        wide_spread=wide_spread,
    )


# ---------------------------------------------------------------------------
# MockProvider
# ---------------------------------------------------------------------------

class MockProvider(MarketDataProvider):
    """
    Тестовый провайдер рыночных данных.

    Параметры:
        exhausted_behavior — что делать когда очередь цен исчерпана.
                             Дефолт: LAST (вернуть последнюю цену).
        auto_subscribe     — автоматически добавлять тикер в подписки
                             при вызове set_price() / feed(). Удобно в тестах.
                             Дефолт: True.

    Пример — фиксированная цена:
        provider = MockProvider()
        provider.set_price("BTCUSDT", make_price("BTCUSDT", last=50000.0))
        provider.start()
        provider.subscribe("BTCUSDT")

        price = provider.get_price("BTCUSDT")
        assert price.last == Decimal("50000.0")

    Пример — последовательность цен:
        prices = [
            make_price("BTCUSDT", last=50000.0),
            make_price("BTCUSDT", last=50100.0),
            make_price("BTCUSDT", last=49900.0),
        ]
        provider = MockProvider()
        provider.feed("BTCUSDT", prices)
        provider.start()
        provider.subscribe("BTCUSDT")

        p1 = provider.get_price("BTCUSDT")  # 50000
        p2 = provider.get_price("BTCUSDT")  # 50100
        p3 = provider.get_price("BTCUSDT")  # 49900
        p4 = provider.get_price("BTCUSDT")  # 49900 (LAST behavior)

    Пример — симуляция ошибки:
        provider = MockProvider(exhausted_behavior=ExhaustedBehavior.RAISE)
        provider.start()
        provider.subscribe("BTCUSDT")
        # Очередь пуста → get_price() поднимает MarketDataUnavailable
    """

    def __init__(
        self,
        exhausted_behavior: ExhaustedBehavior = ExhaustedBehavior.LAST,
        auto_subscribe: bool = True,
    ) -> None:
        self._exhausted_behavior = exhausted_behavior
        self._auto_subscribe     = auto_subscribe

        self._lock = threading.Lock()

        # Очереди цен: ticker → deque[PriceData]
        self._queues:   Dict[str, Deque[PriceData]] = {}
        # Последние отданные цены (для LAST behavior)
        self._last:     Dict[str, PriceData]         = {}
        # Дефолтные цены (для DEFAULT behavior или когда очередь пуста)
        self._defaults: Dict[str, PriceData]         = {}
        # Подписки
        self._subscriptions: set = set()
        # Счётчики вызовов get_price()
        self._call_counts: Dict[str, int] = {}

        self._status  = ProviderStatus.CONNECTED
        self._started = False

    # ------------------------------------------------------------------
    # Test helpers — вызываются ДО start() или в процессе теста
    # ------------------------------------------------------------------

    def set_price(self, ticker: str, price: PriceData) -> None:
        """
        Установить фиксированную цену для тикера.
        Возвращается при каждом get_price() когда очередь исчерпана.
        """
        ticker = ticker.upper()
        with self._lock:
            self._defaults[ticker] = price
            if ticker not in self._last:
                self._last[ticker] = price
            if self._auto_subscribe:
                self._subscriptions.add(ticker)

    def feed(self, ticker: str, prices: List[PriceData]) -> None:
        """
        Добавить последовательность цен в очередь тикера.
        get_price() будет отдавать их по одной в порядке добавления.
        Можно вызывать несколько раз — цены добавляются в конец.
        """
        ticker = ticker.upper()
        with self._lock:
            if ticker not in self._queues:
                self._queues[ticker] = deque()
            self._queues[ticker].extend(prices)
            if self._auto_subscribe:
                self._subscriptions.add(ticker)

    def set_status(self, status: ProviderStatus) -> None:
        """Симулировать смену статуса провайдера (для тестов ошибок)."""
        with self._lock:
            old = self._status
            self._status = status
        logger.debug("MockProvider: статус %s → %s.", old.value, status.value)

    def clear(self, ticker: Optional[str] = None) -> None:
        """
        Сбросить очереди и кэши.
        Если ticker задан — только для него. Иначе — все.
        """
        with self._lock:
            if ticker is not None:
                t = ticker.upper()
                self._queues.pop(t, None)
                self._last.pop(t, None)
                self._defaults.pop(t, None)
                self._call_counts.pop(t, None)
            else:
                self._queues.clear()
                self._last.clear()
                self._defaults.clear()
                self._call_counts.clear()

    def call_count(self, ticker: str) -> int:
        """Сколько раз get_price() вызывался для тикера."""
        with self._lock:
            return self._call_counts.get(ticker.upper(), 0)

    def queue_size(self, ticker: str) -> int:
        """Сколько цен осталось в очереди для тикера."""
        ticker = ticker.upper()
        with self._lock:
            q = self._queues.get(ticker)
            return len(q) if q else 0

    # ------------------------------------------------------------------
    # MarketDataProvider interface
    # ------------------------------------------------------------------

    def subscribe(self, ticker: str) -> None:
        if not self._started:
            raise ProviderNotStarted("Вызовите start() перед subscribe().")
        with self._lock:
            self._subscriptions.add(ticker.upper())

    def unsubscribe(self, ticker: str) -> None:
        ticker = ticker.upper()
        with self._lock:
            self._subscriptions.discard(ticker)

    def get_price(self, ticker: str) -> PriceData:
        """
        Вернуть следующую цену из очереди или дефолтную.

        Raises:
            ProviderNotStarted:    start() не вызывался.
            TickerNotSubscribed:   тикер не подписан.
            MarketDataUnavailable: очередь пуста и exhausted_behavior=RAISE,
                                   или провайдер в статусе FAILED.
        """
        if not self._started:
            raise ProviderNotStarted("Вызовите start() перед get_price().")

        ticker = ticker.upper()

        with self._lock:
            if ticker not in self._subscriptions:
                raise TickerNotSubscribed(ticker)

            status = self._status
            if status == ProviderStatus.FAILED:
                raise MarketDataUnavailable(
                    ticker=ticker,
                    status=status,
                    reason="MockProvider симулирует FAILED.",
                )

            # Инкрементировать счётчик
            self._call_counts[ticker] = self._call_counts.get(ticker, 0) + 1

            # Попытаться взять из очереди
            queue = self._queues.get(ticker)
            if queue:
                price = queue.popleft()
                self._last[ticker] = price
                return price

            # Очередь исчерпана — применяем exhausted_behavior
            return self._handle_exhausted(ticker)

    def get_status(self) -> ProviderStatus:
        with self._lock:
            return self._status

    def start(self) -> None:
        """Пометить провайдер как запущенный. Никаких потоков не создаётся."""
        if self._started:
            return
        self._started = True
        logger.debug("MockProvider: запущен.")

    def stop(self) -> None:
        """Остановить провайдер."""
        self._started = False
        logger.debug("MockProvider: остановлен.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_exhausted(self, ticker: str) -> PriceData:
        """
        Очередь пуста — применить exhausted_behavior.
        Вызывается внутри self._lock.
        """
        behavior = self._exhausted_behavior

        if behavior == ExhaustedBehavior.LAST:
            price = self._last.get(ticker)
            if price is not None:
                return price
            # Нет даже последней — попробуем дефолт
            price = self._defaults.get(ticker)
            if price is not None:
                return price

        elif behavior == ExhaustedBehavior.DEFAULT:
            price = self._defaults.get(ticker)
            if price is not None:
                return price

        # RAISE или нечего вернуть
        raise MarketDataUnavailable(
            ticker=ticker,
            status=self._status,
            reason=(
                f"MockProvider: очередь цен для {ticker} исчерпана "
                f"(behavior={behavior.value})."
            ),
        )
