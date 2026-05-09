"""
Strategy — абстрактный интерфейс торговой стратегии.

Ключевые принципы из ТЗ:
  - Strategy stateless: не хранит состояние между вызовами.
  - Получает цену и параметры → возвращает целевое состояние позиции.
  - Не знает о реальных ордерах, сети, комиссиях — только математика.
  - DecisionEngine сравнивает StrategySignal.target_qty с текущим
    position_qty и принимает необходимые действия.

API Strategy (из ТЗ 7):
  signal = strategy.evaluate(price_data, cycle_snapshot, position_qty)
  delta_qty = signal.target_qty - state.position_qty
  if delta_qty > 0:
      # выставить BUY на delta_qty
  elif delta_qty < 0:
      # заблокировать — ждём закрытия через TP

Конкретные стратегии (MeanReversion, DCA) — в пакете strategies/.
Этот модуль содержит только StrategySignal и BaseStrategy.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from market_data import PriceData
    from bot_config import CycleSnapshot


# ---------------------------------------------------------------------------
# Сигнал стратегии — целевое состояние позиции
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategySignal:
    """
    Целевое состояние позиции при текущей цене.

    Поля:
      should_enter      — открывать ли новый цикл (только при IDLE).
      target_qty        — суммарный целевой объём позиции при текущей цене.
                          None если стратегия не рассчитывает позицию
                          (например, нет активного цикла и вход не нужен).
      target_avg_price  — целевая средняя цена для DCA-расчётов.
                          None в стратегиях без явного расчёта avg.
      tp_price          — уровень take-profit. None если TP не рассчитан.
      dca_levels        — уровни DCA в порядке от текущей цены вниз:
                          (price → cumulative_qty).
                          Используется DCAScheduler в EAGER-режиме
                          (размещение всех ордеров при входе).
                          В LAZY-режиме DecisionEngine вычисляет delta_qty
                          на основе target_qty на каждом тике.
      reason            — причина сигнала для логов. Обязательно.

    Инвариант: если should_enter=True, то target_qty не None.
    Если should_enter=False и target_qty не None, это DCA-сигнал
    для открытой позиции.
    """

    should_enter:     bool
    target_qty:       Decimal | None
    target_avg_price: Decimal | None
    tp_price:         Decimal | None
    reason:           str

    # DCA-уровни: ((price1, cumulative_qty1), (price2, cumulative_qty2), ...)
    # Отсортированы по убыванию цены (первый уровень — ближайший к текущей).
    dca_levels: tuple[tuple[Decimal, Decimal], ...] = ()

    def __post_init__(self) -> None:
        if self.should_enter and self.target_qty is None:
            raise ValueError(
                "StrategySignal: should_enter=True требует target_qty != None"
            )
        if self.target_qty is not None and self.target_qty < 0:
            raise ValueError(
                f"StrategySignal: target_qty не может быть отрицательным, "
                f"получено {self.target_qty}"
            )


# ---------------------------------------------------------------------------
# Сигнал «ничего не делать»
# ---------------------------------------------------------------------------


SIGNAL_WAIT = StrategySignal(
    should_enter=False,
    target_qty=None,
    target_avg_price=None,
    tp_price=None,
    reason="no_signal",
)


# ---------------------------------------------------------------------------
# Абстрактный базовый класс
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """
    Абстрактный интерфейс торговой стратегии.

    Все конкретные стратегии наследуют этот класс и реализуют evaluate().
    Стратегия не хранит состояние — evaluate() идемпотентен при одинаковых
    входных данных.

    Конкретные реализации: strategies/mean_reversion.py, strategies/dca.py
    """

    @abstractmethod
    def evaluate(
        self,
        price_data: "PriceData",
        snapshot: "CycleSnapshot",
        position_qty: Decimal,
    ) -> StrategySignal:
        """
        Рассчитать сигнал для текущего тика.

        Args:
          price_data    — текущая цена (bid/ask/last) из TickContext.
          snapshot      — снапшот параметров текущего цикла (strategy_params,
                          config_version, started_at). Параметры фиксированы
                          на весь цикл.
          position_qty  — текущий объём позиции (из bot_state). Strategy
                          использует его чтобы понять сколько ещё нужно
                          докупить для достижения target_qty.

        Returns:
          StrategySignal с целевым состоянием позиции.

        Стратегия НЕ должна:
          - Обращаться к бирже, БД или внешним источникам.
          - Хранить состояние между вызовами.
          - Знать про комиссии, нормализацию объёмов, типы ордеров.
        """

    def name(self) -> str:
        """Имя стратегии для логов. По умолчанию — имя класса."""
        return self.__class__.__name__


# ---------------------------------------------------------------------------
# Фабрика стратегий
# ---------------------------------------------------------------------------


def create_strategy(strategy_name: str) -> BaseStrategy:
    """
    Создать стратегию по имени из bot_configs.strategy_name.

    Новые стратегии регистрируются здесь. Список пополняется по мере
    реализации конкретных стратегий в strategies/.

    Raises:
      ValueError: стратегия с таким именем не зарегистрирована.
    """
    # Lazy imports чтобы не тянуть все стратегии при импорте пакета
    registry: dict[str, type[BaseStrategy]] = {}

    try:
        from strategies.mean_reversion import MeanReversionStrategy  # noqa: PLC0415
        registry["MeanReversion"] = MeanReversionStrategy
    except ImportError:
        pass

    try:
        from strategies.dca import DCAStrategy  # noqa: PLC0415
        registry["DCA"] = DCAStrategy
    except ImportError:
        pass

    if strategy_name not in registry:
        available = ", ".join(registry.keys()) or "ни одна"
        raise ValueError(
            f"Стратегия '{strategy_name}' не зарегистрирована. "
            f"Доступные: {available}. "
            f"Добавьте реализацию в strategies/ и зарегистрируйте в create_strategy()."
        )

    return registry[strategy_name]()
