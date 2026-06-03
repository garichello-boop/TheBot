"""
bot_loop — бизнес-логика торгового бота (Пункт 7).

Публичный API пакета. Всё что нужно для запуска бота:
  BotLoop       — главный оркестратор.
  BotLoopError  — базовый класс ошибок (для внешних обработчиков).

Иерархия исключений экспортируется полностью для типизации
в стартовом скрипте и мониторинге.
"""

from .bot_loop import BotLoop
from .errors import (
    BotError,
    CriticalError,
    InsufficientFundsError,
    KillSwitchError,
    ReconciliationError,
    RecoverableError,
    StopCraneError,
    TickSkippedError,
)
from .strategy import BaseStrategy, StrategySignal, create_strategy
from .types import (
    CancelResult,
    Decision,
    DecisionAction,
    FillEvent,
    OrderStatus,
    OrderType,
)

__all__ = [
    # Главный класс
    "BotLoop",
    # Ошибки
    "BotError",
    "CriticalError",
    "RecoverableError",
    "StopCraneError",
    "KillSwitchError",
    "InsufficientFundsError",
    "ReconciliationError",
    "TickSkippedError",
    # Стратегии
    "BaseStrategy",
    "StrategySignal",
    "create_strategy",
    # Типы
    "OrderType",
    "OrderStatus",
    "FillEvent",
    "CancelResult",
    "DecisionAction",
    "Decision",
]
