"""
broker/broker.py — абстрактный интерфейс IBroker и исключения подсистемы.

Бот работает ТОЛЬКО с IBroker. Конкретная реализация (PaperBroker /
BybitBroker) создаётся через BrokerFactory и подставляется сюда.
Переключение биржи — одна строка в конфиге, ноль изменений в боте.

Ключевое разделение ответственности:
- create_order()  → возвращает OrderCreated (PENDING), только факт принятия
- Факт исполнения → приходит отдельно: PaperBroker эмитит синхронно,
                    BybitBroker — через OrderTracker по приватному WS
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from broker.models import (
    Balance,
    BrokerMode,
    MarketInfo,
    OpenOrder,
    OrderCreated,
    OrderRequest,
    OrderStatus,
)


# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------

class BrokerError(Exception):
    """Базовое исключение брокера. Все остальные наследуют от него."""


class BrokerTimeout(BrokerError):
    """
    Таймаут запроса create_order (BROKER_REQUEST_TIMEOUT_SEC).

    Критически важно: исход ордера НЕИЗВЕСТЕН. Биржа могла принять
    ордер до того как соединение упало. Повтор create_order без
    предварительного reconciliation может создать дублирующий ордер.

    Правильная реакция: немедленный STOP-CRANE. Торговля возобновляется
    только после ручного reconciliation и резолва оператором.
    """


class BrokerRejected(BrokerError):
    """
    Биржа явно отклонила ордер с кодом ошибки в ответе.
    Emit ORDER_REJECTED. Исход известен — ордер не создан.
    Retry в большинстве случаев бессмысленен без изменения параметров.
    """


class InsufficientFundsError(BrokerError):
    """
    Биржа отклонила ордер из-за нехватки средств на счёте.
    Отличается от BrokerRejected — требует другой реакции в FSM:
    FSM → WAITING_FOR_LIQUIDITY, retry по расписанию 1/5/15/60/900 сек.

    Не путать с OrderNormalizer.INSUFFICIENT_FUNDS — тот срабатывает
    до отправки на биржу (виртуальный баланс). Этот — после отправки
    (реальный баланс на бирже).
    """


class OrderNotFoundError(BrokerError):
    """
    Ордер не найден на бирже при запросе статуса.
    Используется при reconciliation — требует анализа: это нормально
    (ордер был CANCELLED) или аномалия (ордер исчез без CANCELLED → STOP-CRANE).
    """


class BrokerNetworkError(BrokerError):
    """
    Явная сетевая ошибка (connection reset, DNS failure и т.п.).
    В отличие от BrokerTimeout — исход known: запрос не дошёл до биржи.
    Допускает экспоненциальный retry с jitter для cancel/status операций.
    Для create_order — только если успели убедиться что запрос не ушёл.
    """


# ---------------------------------------------------------------------------
# Абстрактный интерфейс
# ---------------------------------------------------------------------------

class IBroker(ABC):
    """
    Абстрактный интерфейс брокера.

    Контракт:
    - create_order() гарантирует либо OrderCreated(PENDING), либо исключение
    - BrokerTimeout означает неизвестный исход → STOP-CRANE, без retry
    - get_order_status() и get_open_orders() используются ТОЛЬКО при reconciliation
    - В рабочем режиме статусы ордеров приходят через OrderTracker (WS)
    """

    @abstractmethod
    def create_order(self, order: OrderRequest) -> OrderCreated:
        """
        Выставить ордер на биржу.

        Возвращает OrderCreated со статусом PENDING — только подтверждение
        принятия. Факт исполнения приходит позже через OrderFill.

        Raises:
            BrokerTimeout: истёк BROKER_REQUEST_TIMEOUT_SEC.
                          Исход неизвестен → STOP-CRANE немедленно.
            InsufficientFundsError: нехватка средств на бирже.
                                    FSM → WAITING_FOR_LIQUIDITY.
            BrokerRejected: биржа явно отклонила ордер.
                            Emit ORDER_REJECTED.
            BrokerNetworkError: сетевая ошибка ДО отправки запроса.
            BrokerError: прочие ошибки брокера.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        Отменить ордер по exchange_order_id.

        Возвращает True если ордер отменён или уже не активен (idempotent).
        Retry при сетевых ошибках допускается — отмена идемпотентна.
        При исчерпании CANCEL_MAX_RETRIES → STOP-CRANE (Close Protocol, шаг 4).

        Raises:
            BrokerError: ошибка, не связанная с сетью.
        """

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderStatus:
        """
        Разовая проверка статуса ордера.

        Используется ТОЛЬКО при reconciliation на старте.
        В рабочем режиме статусы ордеров приходят через OrderTracker (WS) —
        не вызывать get_order_status() внутри tick-loop.

        Raises:
            OrderNotFoundError: ордер не найден (возможно уже исполнен/отменён).
            BrokerError: ошибка запроса.
        """

    @abstractmethod
    def get_balance(self) -> Balance:
        """
        Получить текущий баланс аккаунта в котируемой валюте (USDT).

        Используется в TickContext и BalanceReconciler.
        Торговать можно только из Balance.free.

        Raises:
            BrokerError: ошибка запроса.
        """

    @abstractmethod
    def get_market_info(self, ticker: str) -> MarketInfo:
        """
        Получить торговые ограничения инструмента.

        Используется OrderNormalizer (до создания ордера) и Close Protocol
        (dust_threshold для определения закрытия позиции).
        Реализации кэшируют результат — ограничения меняются редко.

        Raises:
            BrokerError: инструмент не найден или ошибка запроса.
        """

    @abstractmethod
    def get_open_orders(self, ticker: Optional[str] = None) -> List[OpenOrder]:
        """
        Список активных ордеров.

        ticker=None → все ордера аккаунта.
        ticker='BTCUSDT' → только по этому инструменту.

        Используется при reconciliation на старте: бот запрашивает ВСЕ
        активные ордера по тикеру и сопоставляет с persisted_state.

        Raises:
            BrokerError: ошибка запроса.
        """

    @abstractmethod
    def get_mode(self) -> BrokerMode:
        """
        Текущий режим брокера: LIVE или PAPER.
        Используется в OrderCreated.mode и для аналитики сделок.
        """
