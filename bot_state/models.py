"""
broker/models.py — модели данных подсистемы брокера.

Все структуры заморожены (frozen=True): создаются один раз, не мутируют.
Это делает их безопасными для передачи между компонентами без риска
случайного изменения состояния.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Перечисления
# ---------------------------------------------------------------------------

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"            # Принят биржей, ожидает исполнения
    FILLED = "FILLED"              # Исполнен полностью
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # Исполнен частично
    CANCELLED = "CANCELLED"        # Отменён (явно биржей или пользователем)
    REJECTED = "REJECTED"          # Отклонён биржей
    UNKNOWN = "UNKNOWN"            # Статус неизвестен — требует reconciliation


class BrokerMode(str, Enum):
    LIVE = "LIVE"
    PAPER = "PAPER"


class SkipReason(str, Enum):
    """Причина пропуска ордера в OrderNormalizer (Strict Mode)."""
    BELOW_MIN_QTY = "BELOW_MIN_QTY"          # Расчётный объём < min_qty
    QTY_BECAME_ZERO = "QTY_BECAME_ZERO"      # После округления вниз qty = 0
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS" # Баланса не хватает на расчётный объём


# ---------------------------------------------------------------------------
# Запрос и подтверждение создания ордера
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderRequest:
    """
    Запрос на создание ордера. Поступает в IBroker уже нормализованным
    через OrderNormalizer. Бот никогда не отправляет ненормализованный запрос.
    """
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal          # Объём после нормализации (округлён вниз)
    client_order_id: str       # UUID, генерируется ДО отправки на биржу
    bot_id: str
    cycle_id: str
    price: Optional[Decimal] = None  # None для MARKET-ордеров


@dataclass(frozen=True)
class OrderCreated:
    """
    Подтверждение принятия ордера биржей.
    Возвращается из create_order() — только факт создания, статус PENDING.
    Детали исполнения (filled_qty, avg_price, commission) приходят позже
    через OrderFill от OrderTracker или PaperBroker.
    """
    exchange_order_id: str
    client_order_id: str
    status: OrderStatus   # Всегда PENDING при создании
    mode: BrokerMode


@dataclass(frozen=True)
class OrderFill:
    """
    Детали исполнения ордера.
    PaperBroker: эмитируется синхронно внутри create_order().
    BybitBroker: приходит асинхронно через OrderTracker (приватный WS).
    """
    exchange_order_id: str
    client_order_id: str
    ticker: str
    side: OrderSide
    filled_qty: Decimal
    avg_price: Decimal
    commission: Decimal        # В котируемой валюте (USDT)
    mode: BrokerMode
    timestamp: float           # Unix timestamp момента исполнения
    is_partial: bool = False   # True если исполнен частично (filled_qty < запрошенного)


# ---------------------------------------------------------------------------
# Баланс и рыночная информация
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Balance:
    """
    Баланс по активам аккаунта.

    free   — {asset: amount} доступно для новых ордеров.
    locked — {asset: amount} зарезервировано под открытые ордера.

    Пример:
        balance = Balance(
            free={"USDT": Decimal("950.00")},
            locked={"USDT": Decimal("50.00")},
        )
        usdt_free = balance.free.get("USDT", Decimal(0))
        usdt_total = balance.total("USDT")

    Поля — изменяемые dict (frozen=True предотвращает замену поля, но не
    мутацию содержимого). Не мутировать dict после создания.
    Торговать можно только из free[asset].
    """
    free:   dict[str, Decimal]   # {asset: amount}, доступно для торговли
    locked: dict[str, Decimal]   # {asset: amount}, зарезервировано

    def total(self, asset: str) -> Decimal:
        """Суммарный баланс по активу (free + locked)."""
        return (
            self.free.get(asset, Decimal(0))
            + self.locked.get(asset, Decimal(0))
        )


@dataclass(frozen=True)
class MarketInfo:
    """
    Торговые ограничения инструмента с биржи.
    Используется OrderNormalizer и Close Protocol для проверки dust_threshold.
    """
    ticker: str
    min_qty: Decimal        # Минимальный объём ордера в базовой валюте
    step_size: Decimal      # Шаг лота — объём квантуется кратно step_size
    min_notional: Decimal   # Минимальная сумма ордера в котируемой валюте (qty * price)
    price_precision: int    # Знаков после запятой для цены
    tick_size: Decimal      # Минимальный шаг цены


# ---------------------------------------------------------------------------
# Открытые ордера (для reconciliation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenOrder:
    """
    Ордер активный на бирже в данный момент.
    Используется StateRecovery при reconciliation на старте.
    """
    exchange_order_id: str
    client_order_id: str       # Может быть пустым если биржа не поддерживает
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal          # Изначальный объём ордера
    filled_qty: Decimal        # Исполненный объём (0 если не тронут)
    price: Optional[Decimal]   # None для MARKET
    status: OrderStatus
    mode: BrokerMode


# ---------------------------------------------------------------------------
# Исторические fills (для reconciliation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HistoricalFill:
    """
    Исполненная сделка из истории биржи.

    Используется StateRecovery при reconciliation на старте (шаг 4)
    и Close Protocol (шаг 5): перечитать fills с момента last_applied_trade_id.

    Отличается от OrderFill: содержит trade_id — уникальный ID сделки
    на бирже, необходимый для инкрементального reconciliation через
    last_applied_trade_id из bot_state. OrderFill — для real-time уведомлений
    от OrderTracker и PaperBroker; HistoricalFill — для запросов истории.
    """
    trade_id: str              # Уникальный ID сделки на бирже (ключ для last_applied_trade_id)
    exchange_order_id: str
    client_order_id: str       # Может быть пустым если биржа не поддерживает
    ticker: str
    side: OrderSide
    filled_qty: Decimal
    avg_price: Decimal
    commission: Decimal        # В котируемой валюте (USDT)
    timestamp: float           # Unix timestamp момента исполнения
    mode: BrokerMode


# ---------------------------------------------------------------------------
# Результат нормализации
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizeResult:
    """
    Результат работы OrderNormalizer.

    Два возможных исхода:
    - is_skip=False: order содержит нормализованный OrderRequest, готовый к отправке
    - is_skip=True: ордер пропускается, skip_reason и skip_event_type объясняют почему

    skip_event_type указывает caller'у какое событие эмитировать:
    - 'INSUFFICIENT_FUNDS'   → при SkipReason.INSUFFICIENT_FUNDS
    - 'ORDER_CREATE_FAILED'  → при остальных причинах пропуска
    """
    is_skip: bool
    order: Optional[OrderRequest] = None
    skip_reason: Optional[SkipReason] = None
    skip_event_type: Optional[str] = None  # 'INSUFFICIENT_FUNDS' | 'ORDER_CREATE_FAILED'

    @classmethod
    def ok(cls, order: OrderRequest) -> NormalizeResult:
        """Фабричный метод для успешной нормализации."""
        return cls(is_skip=False, order=order)

    @classmethod
    def skip(cls, reason: SkipReason) -> NormalizeResult:
        """Фабричный метод для пропуска ордера."""
        event_type = (
            "INSUFFICIENT_FUNDS"
            if reason == SkipReason.INSUFFICIENT_FUNDS
            else "ORDER_CREATE_FAILED"
        )
        return cls(is_skip=True, skip_reason=reason, skip_event_type=event_type)
