"""
bot_config/strategy_schemas.py

Pydantic-схемы для валидации strategy_params из bot_configs.strategy_params (JSONB).

Архитектура:
  - BaseStrategyParams — общие поля присутствующие в params любой стратегии
    (SL_ENABLED, SL_PCT). Содержит cross-field validator: SL_ENABLED=True
    требует SL_PCT.
  - Конкретные схемы (MeanReversionParams, ...) наследуют BaseStrategyParams
    и добавляют специфичные для стратегии поля.
  - STRATEGY_SCHEMAS — реестр {strategy_name: schema_class}.
  - validate_strategy_params() — публичная функция-шлюз для ConfigValidator.

Поведение при валидации:
  - extra="allow": неизвестные ключи в params не вызывают ошибок.
    Это обеспечивает обратную совместимость: старые configs с нестандартными
    именами полей (ma_period, tp_pct и т.п.) проходят, пока все
    обязательные поля имеют дефолты.
  - Все поля в текущих схемах имеют дефолты → пустой {} config валиден.
    Это позволяет постепенно населять params без немедленного слома бота.
  - Коэрция типов включена (Pydantic v2 default):
    "5.0" → 5.0, "20" → 20, "true" → True.

Добавление новой стратегии:
    class MyStrategyParams(BaseStrategyParams):
        MY_PARAM: int = Field(default=10, ge=1)

    STRATEGY_SCHEMAS["MyStrategy"] = MyStrategyParams
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Базовая схема — SL-поля присутствуют в каждой стратегии
# ---------------------------------------------------------------------------

class BaseStrategyParams(BaseModel):
    """
    Общие параметры для всех стратегий.

    SL_ENABLED / SL_PCT — риск-менеджмент, независимый от стратегической
    логики. Присутствует в каждой стратегии как внешний ограничитель потерь.
    """

    model_config = {
        "extra": "allow",            # неизвестные ключи — не ошибка
        "populate_by_name": True,    # обращение и по alias, и по имени
        "coerce_numbers_to_str": False,
    }

    SL_ENABLED: bool = Field(
        default=False,
        description="Включить Stop-Loss. False = SL отключён.",
    )
    SL_PCT: Optional[float] = Field(
        default=None,
        description=(
            "Stop-Loss в процентах от цены входа (например 5.0 = 5%). "
            "Обязателен когда SL_ENABLED=True."
        ),
    )

    @field_validator("SL_PCT", mode="before")
    @classmethod
    def coerce_sl_pct(cls, v: Any) -> Any:
        """Принять SL_PCT как строку ('5.0') или число (5.0)."""
        if v is None:
            return v
        try:
            return float(v)
        except (TypeError, ValueError):
            return v  # Pydantic выдаст ошибку типа сам

    @model_validator(mode="after")
    def validate_sl_consistency(self) -> "BaseStrategyParams":
        """SL_ENABLED=True требует SL_PCT > 0 и < 100."""
        if not self.SL_ENABLED:
            return self

        if self.SL_PCT is None:
            raise ValueError(
                "SL_PCT обязателен когда SL_ENABLED=True. "
                "Укажите SL_PCT > 0 (например 5.0 для 5%) "
                "или установите SL_ENABLED=False."
            )
        if self.SL_PCT <= 0:
            raise ValueError(
                f"SL_PCT должен быть > 0, получено {self.SL_PCT}."
            )
        if self.SL_PCT >= 100:
            raise ValueError(
                f"SL_PCT={self.SL_PCT} выглядит некорректно (>= 100%). "
                "Укажите значение в процентах, например 5.0 для 5%."
            )
        return self


# ---------------------------------------------------------------------------
# MeanReversion
# ---------------------------------------------------------------------------

class MeanReversionParams(BaseStrategyParams):
    """
    Параметры стратегии Mean Reversion (возврат к среднему).

    Использует полосы Боллинджера для определения точек входа и выхода.
    Вход: цена касается нижней полосы (BB_MULT стандартных отклонений от MA).
    Выход (TP): цена возвращается к средней линии (MA).

    Все поля имеют дефолты → пустой strategy_params {} валиден.
    Бот использует дефолты до тех пор, пока оператор не задаст явные значения.

    Trailing TP (TRAILING_TP_ENABLED / TRAILING_TP_PCT):
      Когда включён, бот отслеживает максимум цены после входа.
      После того как цена впервые превысила уровень обычного TP (активация),
      начинает работать трейлинг: TP поднимается вслед за максимумом.
      Позиция закрывается когда текущая цена откатывается на TRAILING_TP_PCT%
      от достигнутого максимума. Статичный TP-ордер остаётся на бирже как
      подстраховка на случай гэпа вверх.

      Ограничение (paper trading): максимум хранится в памяти и сбрасывается
      при рестарте бота. При рестарте трейлинг начинается с текущей цены.
    """

    BB_PERIOD: int = Field(
        default=20,
        ge=2,
        description="Период расчёта полос Боллинджера (количество свечей).",
    )
    BB_MULT: float = Field(
        default=2.0,
        gt=0,
        description="Множитель стандартного отклонения для ширины полос.",
    )
    INVEST_SHARE: float = Field(
        default=0.20,
        gt=0,
        lt=1,
        description=(
            "Доля свободного баланса на один вход (0.20 = 20%). "
            "Должна быть > 0 и < 1."
        ),
    )
    TAKE_PROFIT: float = Field(
        default=0.02,
        gt=0,
        description=(
            "Take-profit как десятичная доля от цены входа "
            "(0.02 = 2%). Используется как ориентир для TP-ордера."
        ),
    )
    MAX_ENTRIES: int = Field(
        default=2,
        ge=1,
        description="Максимальное количество DCA-усреднений в одном цикле.",
    )

    # ------------------------------------------------------------------
    # Trailing TP
    # ------------------------------------------------------------------

    TRAILING_TP_ENABLED: bool = Field(
        default=False,
        description=(
            "Включить скользящий take-profit. "
            "Когда True, TP следует за максимумом цены после активации."
        ),
    )
    TRAILING_TP_PCT: float = Field(
        default=1.0,
        gt=0,
        description=(
            "Откат от максимума для срабатывания trailing TP, в процентах "
            "(например 1.0 = 1%). Обязателен когда TRAILING_TP_ENABLED=True."
        ),
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("BB_MULT", "INVEST_SHARE", "TAKE_PROFIT", mode="before")
    @classmethod
    def coerce_float_fields(cls, v: Any) -> Any:
        """Принять строковые значения ('2.0', '0.20') из JSONB."""
        if v is None:
            return v
        try:
            return float(v)
        except (TypeError, ValueError):
            return v

    @field_validator("BB_PERIOD", "MAX_ENTRIES", mode="before")
    @classmethod
    def coerce_int_fields(cls, v: Any) -> Any:
        """Принять строковые значения ('20', '2') из JSONB."""
        if v is None:
            return v
        try:
            return int(v)
        except (TypeError, ValueError):
            return v

    @field_validator("TRAILING_TP_ENABLED", mode="before")
    @classmethod
    def coerce_trailing_tp_enabled(cls, v: Any) -> Any:
        """Принять 'true'/'false' строки из JSONB."""
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        return v

    @field_validator("TRAILING_TP_PCT", mode="before")
    @classmethod
    def coerce_trailing_tp_pct(cls, v: Any) -> Any:
        """Принять строковые значения ('1.0') из JSONB."""
        if v is None:
            return v
        try:
            return float(v)
        except (TypeError, ValueError):
            return v

    @model_validator(mode="after")
    def validate_trailing_tp_consistency(self) -> "MeanReversionParams":
        """TRAILING_TP_ENABLED=True требует TRAILING_TP_PCT > 0."""
        if not self.TRAILING_TP_ENABLED:
            return self
        if self.TRAILING_TP_PCT is None or self.TRAILING_TP_PCT <= 0:
            raise ValueError(
                "TRAILING_TP_PCT должен быть > 0 когда TRAILING_TP_ENABLED=True. "
                "Укажите TRAILING_TP_PCT в процентах, например 1.0 для 1%."
            )
        return self


# ---------------------------------------------------------------------------
# MartingaleDCA
# ---------------------------------------------------------------------------

class MartingaleDCAParams(BaseStrategyParams):
    """
    Параметры стратегии Martingale/DCA.

    Всегда входит при IDLE. При падении цены на DROP_TRIGGER_PCT% от
    текущей средней — усредняется (DCA). При росте на PROFIT_TARGET_PCT%
    от средней — закрывает позицию (TP).

    Объём каждого следующего DCA-ордера умножается на DCA_MULTIPLIER.
    DCA_MULTIPLIER=1.0 — плоская пирамида (равные объёмы на каждом уровне).
    DCA_MULTIPLIER=1.5 — классическая мартингальная прогрессия.

    Все поля имеют дефолты → пустой {} config валиден.
    """

    INVEST_AMOUNT: float = Field(
        default=50.0,
        gt=0,
        description=(
            "Размер базового ордера в USDT (например 50.0). "
            "Объём entry-ордера = INVEST_AMOUNT / текущая_цена. "
            "Не зависит от баланса — ответственность оператора."
        ),
    )
    DROP_TRIGGER_PCT: float = Field(
        default=3.0,
        gt=0,
        lt=50,
        description=(
            "Порог падения от текущей средней для DCA-усреднения, в процентах "
            "(например 3.0 = 3%). Каждый следующий уровень рассчитывается "
            "от avg предыдущего уровня."
        ),
    )
    PROFIT_TARGET_PCT: float = Field(
        default=5.0,
        gt=0,
        lt=100,
        description=(
            "Цель take-profit — рост от текущей средней, в процентах "
            "(например 5.0 = 5%). TP = avg_price * (1 + PROFIT_TARGET_PCT / 100)."
        ),
    )
    MAX_DCA_LEVELS: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Максимальное количество DCA-усреднений в одном цикле (не считая входа). "
            "Итого позиций: 1 вход + MAX_DCA_LEVELS усреднений."
        ),
    )
    DCA_MULTIPLIER: float = Field(
        default=1.5,
        ge=1.0,
        le=5.0,
        description=(
            "Мультипликатор объёма каждого следующего DCA-ордера. "
            "1.0 = все уровни одинакового размера (плоский DCA). "
            "1.5 = объём каждого следующего уровня в 1.5 раза больше предыдущего."
        ),
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator(
        "INVEST_AMOUNT", "DROP_TRIGGER_PCT", "PROFIT_TARGET_PCT", "DCA_MULTIPLIER",
        mode="before",
    )
    @classmethod
    def coerce_float_fields(cls, v: Any) -> Any:
        """Принять строковые значения из JSONB."""
        if v is None:
            return v
        try:
            return float(v)
        except (TypeError, ValueError):
            return v

    @field_validator("MAX_DCA_LEVELS", mode="before")
    @classmethod
    def coerce_int_fields(cls, v: Any) -> Any:
        """Принять строковые значения из JSONB."""
        if v is None:
            return v
        try:
            return int(v)
        except (TypeError, ValueError):
            return v

    @model_validator(mode="after")
    def validate_dca_consistency(self) -> "MartingaleDCAParams":
        """
        Проверить что DROP_TRIGGER_PCT и PROFIT_TARGET_PCT образуют
        рабочее соотношение риск/доходность.

        Мягкое предупреждение: PROFIT_TARGET_PCT должен быть больше
        DROP_TRIGGER_PCT (иначе TP может срабатывать раньше первого DCA).
        Не является ошибкой — оператор может хотеть скальпинг.
        """
        if self.PROFIT_TARGET_PCT < self.DROP_TRIGGER_PCT:
            logger.warning(
                "MartingaleDCA: PROFIT_TARGET_PCT=%.1f < DROP_TRIGGER_PCT=%.1f. "
                "TP может сработать до первого DCA-усреднения. "
                "Это допустимо, но убедитесь что это намеренно.",
                self.PROFIT_TARGET_PCT,
                self.DROP_TRIGGER_PCT,
            )
        return self


# ---------------------------------------------------------------------------
# Реестр схем и публичная функция валидации
# ---------------------------------------------------------------------------

STRATEGY_SCHEMAS: dict[str, type[BaseStrategyParams]] = {
    "MeanReversion":  MeanReversionParams,
    "MartingaleDCA":  MartingaleDCAParams,
}
"""
Реестр схем по strategy_name.

Добавить новую схему:
    STRATEGY_SCHEMAS["MyStrategy"] = MyStrategyParams
"""


def validate_strategy_params(
    strategy_name: str,
    params: dict[str, Any],
) -> list[str]:
    """
    Валидировать strategy_params против зарегистрированной Pydantic-схемы.

    Возвращает список строк ошибок (пустой = валидно).
    Возвращает пустой список если для стратегии нет схемы.

    Args:
        strategy_name: значение из bot_configs.strategy_name.
        params:        словарь из bot_configs.strategy_params (JSONB).

    Используется ConfigValidator._check_strategy_params().
    """
    from pydantic import ValidationError  # локальный импорт, не засоряет namespace

    schema_cls = STRATEGY_SCHEMAS.get(strategy_name)
    if schema_cls is None:
        return []

    try:
        schema_cls.model_validate(params)
        return []
    except ValidationError as exc:
        errors: list[str] = []
        for e in exc.errors():
            # Формат: "MeanReversion.BB_PERIOD: ..."
            loc_parts = [str(x) for x in e["loc"]] if e["loc"] else []
            loc = ".".join(loc_parts) if loc_parts else "config"
            msg = e["msg"]
            errors.append(f"{strategy_name}.{loc}: {msg}")
        return errors
