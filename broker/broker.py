"""
broker/broker.py — абстрактный интерфейс IBroker и исключения подсистемы.

Бот работает ТОЛЬКО с IBroker. Конкретная реализация (PaperBroker /
BybitBroker) создаётся через BrokerFactory и подставляется сюда.
Переключение биржи — одна строка в конфиге, ноль изменений в боте.

Ключевое разделение ответственности:
- create_order()       → возвращает OrderCreated (PENDING), только факт принятия
- get_pending_fills()  → дренирует внутреннюю очередь FillEvent (WS/paper)
- get_fills()          → исторические сделки для reconciliation (startp/Close Protocol)
- Факт исполнения в рабочем режиме приходит через get_pending_fills() каждый тик,
  а не через polling get_order_status().
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional

from broker.models import (
    Balance,
    BrokerMode,
    HistoricalFill,
    MarketInfo,
    OpenOrder,
    OrderCreated,
    OrderRequest,
    OrderStatus,
)

if TYPE_CHECKING:
    # FillEvent живёт в business_logic.types — туда его переместили потому что
    # он несёт order_type (ENTRY/TP/DCA), что является бизнес-логикой, а не
    # брокерской концепцией. Импорт только для аннотаций (from __future__ import
    # annotations делает их строками на рантайме → нет циклического импорта).
    from business_logic.types import FillEvent


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
    - get_pending_fills() дренирует очередь fill-событий каждый тик
    - get_order_status() и get_open_orders() используются ТОЛЬКО при reconciliation
    - get_fills() используется ТОЛЬКО при reconciliation (startup / Close Protocol)
    - В рабочем режиме статусы ордеров приходят через get_pending_fills()
    """

    @abstractmethod
    def create_order(self, order: OrderRequest) -> OrderCreated:
        """
        Выставить ордер на биржу.

        Возвращает OrderCreated со статусом PENDING — только подтверждение
        принятия. Факт исполнения приходит позже через get_pending_fills().

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
        В рабочем режиме статусы приходят через get_pending_fills() —
        не вызывать get_order_status() внутри tick-loop.

        Raises:
            OrderNotFoundError: ордер не найден.
            BrokerError: ошибка запроса.
        """

    @abstractmethod
    def get_balance(self) -> Balance:
        """
        Получить текущий баланс аккаунта.

        Возвращает Balance с полями free и locked — dict[asset, Decimal].
        Торговать можно только из Balance.free[quote_asset].

        Используется в TickContext и BalanceReconciler.

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
    def get_pending_fills(self) -> "List[FillEvent]":
        """
        Дренировать внутреннюю очередь событий исполнения.

        Вызывается в начале каждого тика из TickContext.collect().
        Возвращает все накопившиеся с прошлого тика события и очищает очередь.

        FillEvent (из business_logic.types) несёт order_type (ENTRY/TP/DCA) —
        роль ордера в торговом цикле. Эту роль проставляет OrderManager при
        регистрации ордера: он добавляет (client_order_id → role) в реестр,
        а реализация брокера использует реестр для обогащения fill-событий
        перед помещением в очередь.

        Отличается от get_fills():
          get_pending_fills() — real-time очередь WS-событий / PaperBroker,
                               вызывается каждый тик.
          get_fills()         — исторический запрос к бирже,
                               только для reconciliation на старте.

        Raises:
            BrokerError: ошибка доступа к внутренней очереди (редко).
        """

    @abstractmethod
    def get_fills(
        self,
        ticker: str,
        since_trade_id: Optional[str] = None,
    ) -> List[HistoricalFill]:
        """
        Получить историю исполненных сделок по тикеру.

        since_trade_id — если указан, возвращает только сделки после него
        (инкрементальный reconciliation по last_applied_trade_id из bot_state).
        since_trade_id=None → все доступные сделки по тикеру (для первичного
        reconciliation или после длительного даунтайма).

        Используется ТОЛЬКО при reconciliation:
        - StateRecovery.startup() шаг 4: прочитать fills с last_applied_trade_id
        - Close Protocol шаг 5: перечитать fills после отмены DCA

        В рабочем режиме fills приходят через get_pending_fills() —
        не вызывать get_fills() внутри tick-loop.

        Raises:
            BrokerError: ошибка запроса.
        """

    @abstractmethod
    def get_mode(self) -> BrokerMode:
        """
        Текущий режим брокера: LIVE или PAPER.
        Используется в OrderCreated.mode и для аналитики сделок.
        """
