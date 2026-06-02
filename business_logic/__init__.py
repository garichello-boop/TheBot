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
from .strategy import BaseStrategy, StrategySignal
from .types import (
    CancelResult,
    Decision,
    DecisionAction,
    FillEvent,
    OrderStatus,
    OrderType,
)
from strategies.mean_reversion import MeanReversionStrategy

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

def create_strategy(strategy_name: str, params: dict = None):
    """
    Фабрика стратегий.
    Связывает название из БД с реальным Python-классом.
    """
    if params is None:
        params = {}

    # Реестр доступных стратегий
    strategies = {
        "MeanReversion": MeanReversionStrategy,
    }

    if strategy_name not in strategies:
        available = ", ".join(strategies.keys()) if strategies else "ни одна"
        raise ValueError(f"Стратегия '{strategy_name}' не зарегистрирована. Доступные: {available}")

    # Создаем экземпляр стратегии
    return strategies[strategy_name](**params)