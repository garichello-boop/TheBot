"""
strategies/martingale_dca.py

Стратегия Martingale/DCA под интерфейс BaseStrategy.

Логика:
  - Нет позиции (IDLE) → всегда входим.
  - Цена упала от текущей avg на DROP_TRIGGER_PCT% → DCA-усреднение.
  - Цена выросла от текущей avg на PROFIT_TARGET_PCT% → TP.

DCA-расписание вычисляется при входе (entry) и хранится в памяти инстанса.
Это позволяет стратегии знать точные уровни триггеров и ожидаемые avg
без доступа к BotState, который не передаётся в evaluate().

Алгоритм расписания:
  level 0 (entry): price=P0, qty=INVEST_AMOUNT/P0, avg0=P0
  level 1 (DCA-1): trigger=avg0*(1-DROP/100), qty1=qty0*MULT, avg1=weighted
  level k (DCA-k): trigger=avg_{k-1}*(1-DROP/100), qty_k=qty_{k-1}*MULT

TP-цена всегда = expected_avg_at_current_level * (1 + PROFIT_TARGET_PCT/100).

Поведение после рестарта бота с открытой позицией:
  Расписание теряется. Стратегия переходит в режим "удержания без DCA":
  TP выставляется как текущая_цена*(1+PROFIT_TARGET_PCT/100).
  Позиция закроется по TP естественным образом.

Параметры из strategy_params (JSONB) — см. MartingaleDCAParams:
  INVEST_AMOUNT      — размер базового ордера в USDT (например 50.0)
  DROP_TRIGGER_PCT   — % падения от avg для DCA (например 3.0 = 3%)
  PROFIT_TARGET_PCT  — % роста от avg для TP (например 5.0 = 5%)
  MAX_DCA_LEVELS     — максимум шагов DCA (1–10, без учёта входа)
  DCA_MULTIPLIER     — множитель объёма каждого следующего DCA-ордера
                       1.0 = плоский DCA (все уровни одинаковы),
                       1.5 = классическая мартингальная прогрессия
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING

from business_logic.strategy import BaseStrategy, StrategySignal, SIGNAL_WAIT

if TYPE_CHECKING:
    from market_data import PriceData
    from bot_config import CycleSnapshot

logger = logging.getLogger(__name__)

# Порог ниже которого qty считается пылью
_DUST_QTY = Decimal("0.000001")
# Допуск при поиске текущего уровня по position_qty (1%)
_QTY_TOLERANCE = Decimal("0.01")


# ---------------------------------------------------------------------------
# Уровень DCA-расписания (неизменяемый)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _DCALevel:
    """Один уровень предвычисленного DCA-расписания."""
    level:         int      # 0 = entry, 1,2,... = DCA-уровни
    trigger_price: Decimal  # цена активации этого уровня
    order_qty:     Decimal  # объём покупки на этом уровне (не кумулятивный)
    cum_qty:       Decimal  # кумулятивный объём после исполнения этого уровня
    expected_avg:  Decimal  # ожидаемая средняя цена после исполнения уровня


# ---------------------------------------------------------------------------
# Стратегия
# ---------------------------------------------------------------------------

class MartingaleDCAStrategy(BaseStrategy):
    """
    Martingale/DCA стратегия.

    Всегда входит при IDLE (частота контролируется статусом бота:
    ACTIVE / CLOSE_ONLY / STOPPED — это зона ответственности бот-конфига,
    а не стратегии).

    Состояние инстанса:
      _schedule       — DCA-расписание, строится при входе
      _prev_qty       — position_qty предыдущего тика (для детектирования
                        конца цикла и нового входа)
    """

    def __init__(self) -> None:
        self._schedule: list[_DCALevel] = []
        self._prev_qty: Decimal = Decimal("0")

    # ------------------------------------------------------------------
    # Интерфейс BaseStrategy
    # ------------------------------------------------------------------

    def evaluate(
        self,
        price_data: "PriceData",
        snapshot:   "CycleSnapshot",
        position_qty: Decimal,
    ) -> StrategySignal:
        """
        Вычислить сигнал для текущего тика.

        IDLE  (position_qty == 0): вернуть сигнал входа, построить расписание.
        IN_POSITION (position_qty > 0): вернуть DCA или удержание.
        """
        from bot_config.strategy_schemas import MartingaleDCAParams  # lazy import

        params = MartingaleDCAParams.model_validate(dict(snapshot.strategy_params))
        price  = Decimal(str(price_data.last))

        # --- Конец цикла: позиция только что закрылась -----------------
        if position_qty == Decimal("0") and self._prev_qty > Decimal("0"):
            logger.info(
                "MartingaleDCA: позиция закрыта (было qty=%s), "
                "сброс расписания DCA",
                self._prev_qty,
            )
            self._reset()

        self._prev_qty = position_qty

        # --- IDLE → Entry ----------------------------------------------
        if position_qty == Decimal("0"):
            return self._signal_enter(price, params)

        # --- IN_POSITION → DCA или ожидание ----------------------------
        return self._signal_in_position(price, params, position_qty)

    def name(self) -> str:
        return "MartingaleDCA"

    # ------------------------------------------------------------------
    # Вход
    # ------------------------------------------------------------------

    def _signal_enter(
        self,
        price:  Decimal,
        params,
    ) -> StrategySignal:
        """
        Построить DCA-расписание и вернуть сигнал входа.

        Объём базового ордера: qty = INVEST_AMOUNT / current_price.
        Это фиксированный USDT-объём независимо от текущего баланса.
        Баланс при этом не проверяется — это зона PaperBroker/BybitBroker.
        """
        invest    = Decimal(str(params.INVEST_AMOUNT))
        base_qty  = (invest / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)

        if base_qty <= _DUST_QTY:
            logger.warning(
                "MartingaleDCA: base_qty=%s <= dust при цене=%s и INVEST_AMOUNT=%s, "
                "пропускаем вход",
                base_qty, price, params.INVEST_AMOUNT,
            )
            return SIGNAL_WAIT

        # Строим расписание
        self._schedule = _build_schedule(
            entry_price = price,
            base_qty    = base_qty,
            drop_pct    = Decimal(str(params.DROP_TRIGGER_PCT)),
            max_levels  = params.MAX_DCA_LEVELS,
            multiplier  = Decimal(str(params.DCA_MULTIPLIER)),
        )

        entry_level = self._schedule[0]
        tp_price    = _calc_tp(entry_level.expected_avg, params.PROFIT_TARGET_PCT)

        logger.info(
            "MartingaleDCA: вход price=%.4f qty=%s tp=%.4f "
            "DCA-уровней=%d drop=%.1f%% mult=%.1f",
            price, base_qty, tp_price,
            len(self._schedule) - 1,
            params.DROP_TRIGGER_PCT,
            params.DCA_MULTIPLIER,
        )
        for lvl in self._schedule[1:]:
            logger.debug(
                "  DCA-%d: trigger=%.4f qty=%s cum_qty=%s expected_avg=%.4f",
                lvl.level, lvl.trigger_price, lvl.order_qty,
                lvl.cum_qty, lvl.expected_avg,
            )

        return StrategySignal(
            should_enter     = True,
            target_qty       = entry_level.cum_qty,
            target_avg_price = price,
            tp_price         = tp_price,
            reason           = f"dca_enter: price={price:.4f}",
        )

    # ------------------------------------------------------------------
    # В позиции
    # ------------------------------------------------------------------

    def _signal_in_position(
        self,
        price:        Decimal,
        params,
        position_qty: Decimal,
    ) -> StrategySignal:
        """
        DCA-сигнал или удержание позиции.

        Алгоритм:
          1. Определить текущий уровень расписания по position_qty.
          2. Вычислить tp_price = expected_avg_of_level * (1 + PROFIT_TARGET_PCT%).
          3. Если следующий уровень существует и цена <= его trigger_price
             → вернуть target_qty = next_level.cum_qty (LAZY DCA).
          4. Иначе → удерживать (target_qty = position_qty).

        Режим рестарта (расписание отсутствует):
          Удерживаем без DCA. TP = текущая_цена * (1 + PROFIT_TARGET_PCT%).
        """
        profit_pct = params.PROFIT_TARGET_PCT

        # --- Рестарт: расписание потеряно ------------------------------
        if not self._schedule:
            tp_price = _calc_tp(price, profit_pct)
            logger.debug(
                "MartingaleDCA: расписание отсутствует (рестарт?). "
                "Удерживаем позицию qty=%s, TP=%.4f",
                position_qty, tp_price,
            )
            return StrategySignal(
                should_enter     = False,
                target_qty       = position_qty,
                target_avg_price = None,
                tp_price         = tp_price,
                reason           = "hold_no_schedule: restart_recovery",
            )

        # --- Определить текущий уровень расписания ---------------------
        current = _find_current_level(self._schedule, position_qty)
        tp_price = _calc_tp(current.expected_avg, profit_pct)

        # --- Проверить следующий DCA-уровень ---------------------------
        next_idx = current.level + 1
        if next_idx < len(self._schedule):
            next_lvl = self._schedule[next_idx]
            if price <= next_lvl.trigger_price:
                logger.info(
                    "MartingaleDCA: DCA-%d сработал: price=%.4f <= trigger=%.4f | "
                    "delta_qty=%s, новый avg≈%.4f, tp≈%.4f",
                    next_idx,
                    price,
                    next_lvl.trigger_price,
                    next_lvl.order_qty,
                    next_lvl.expected_avg,
                    _calc_tp(next_lvl.expected_avg, profit_pct),
                )
                return StrategySignal(
                    should_enter     = False,
                    target_qty       = next_lvl.cum_qty,
                    target_avg_price = None,
                    tp_price         = tp_price,   # TP по текущему avg до исполнения
                    reason           = (
                        f"dca_l{next_idx}: "
                        f"price={price:.4f} <= trigger={next_lvl.trigger_price:.4f}"
                    ),
                )

        # --- Удерживаем ------------------------------------------------
        logger.debug(
            "MartingaleDCA: hold уровень=%d avg≈%.4f tp=%.4f",
            current.level, current.expected_avg, tp_price,
        )
        return StrategySignal(
            should_enter     = False,
            target_qty       = position_qty,
            target_avg_price = None,
            tp_price         = tp_price,
            reason           = (
                f"hold_l{current.level}: "
                f"avg≈{current.expected_avg:.4f} "
                f"tp={tp_price:.4f}"
            ),
        )

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        """Сбросить внутреннее состояние при закрытии цикла."""
        self._schedule = []
        self._prev_qty = Decimal("0")


# ---------------------------------------------------------------------------
# Построение DCA-расписания (чистая функция, тестируемая отдельно)
# ---------------------------------------------------------------------------

def _build_schedule(
    entry_price: Decimal,
    base_qty:    Decimal,
    drop_pct:    Decimal,
    max_levels:  int,
    multiplier:  Decimal,
) -> list[_DCALevel]:
    """
    Предвычислить полное DCA-расписание от цены входа.

    Уровень 0: вход на entry_price с объёмом base_qty.
    Уровень k (k>=1): триггер = avg_{k-1} * (1 - drop_pct/100),
                      qty_k = qty_{k-1} * multiplier.

    Args:
        entry_price: цена первого входа.
        base_qty:    объём первого входа (USDT / entry_price).
        drop_pct:    % падения от текущей avg для активации следующего уровня.
        max_levels:  количество DCA-уровней (не считая входа).
        multiplier:  мультипликатор объёма на каждом следующем уровне.

    Returns:
        Список _DCALevel длиной max_levels+1 (включая вход).
    """
    schedule: list[_DCALevel] = []

    cum_qty:  Decimal = Decimal("0")
    cum_cost: Decimal = Decimal("0")
    qty:      Decimal = base_qty

    for level in range(max_levels + 1):   # 0 = entry, 1..max_levels = DCA
        if level == 0:
            trigger = entry_price
        else:
            prev_avg = schedule[-1].expected_avg
            trigger  = prev_avg * (Decimal("1") - drop_pct / Decimal("100"))
            trigger  = trigger.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            qty      = (
                schedule[-1].order_qty * multiplier
            ).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)

        if qty <= _DUST_QTY:
            # Объём стал слишком маленьким — расписание обрывается
            logger.warning(
                "_build_schedule: qty=%s на уровне %d <= dust, расписание обрезано",
                qty, level,
            )
            break

        cum_qty  += qty
        cum_cost += trigger * qty
        avg       = cum_cost / cum_qty

        schedule.append(_DCALevel(
            level         = level,
            trigger_price = trigger,
            order_qty     = qty,
            cum_qty       = cum_qty,
            expected_avg  = avg,
        ))

    return schedule


# ---------------------------------------------------------------------------
# Поиск текущего уровня по position_qty
# ---------------------------------------------------------------------------

def _find_current_level(
    schedule:     list[_DCALevel],
    position_qty: Decimal,
) -> _DCALevel:
    """
    Определить на каком уровне расписания сейчас находится позиция.

    Ищет уровень где cum_qty наиболее близок к position_qty сверху.
    Допуск 1% на проскальзывание и частичные исполнения.

    Fallback: уровень 0 (вход).
    """
    tolerance = _QTY_TOLERANCE

    # Идём от последнего уровня к первому
    for lvl in reversed(schedule):
        lower_bound = lvl.cum_qty * (Decimal("1") - tolerance)
        if position_qty >= lower_bound:
            return lvl

    return schedule[0]


# ---------------------------------------------------------------------------
# Расчёт TP-цены
# ---------------------------------------------------------------------------

def _calc_tp(avg_price: Decimal, profit_pct: float) -> Decimal:
    """TP = avg_price * (1 + profit_pct / 100), округлено до 4 знаков."""
    return (
        avg_price * (Decimal("1") + Decimal(str(profit_pct)) / Decimal("100"))
    ).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
