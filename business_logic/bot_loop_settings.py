"""
BotLoopSettings — параметры бизнес-логики бота (Пункт 7).

Добавить в config/settings.py как вложенный класс AppSettings
или как отдельную модель с env_prefix="BOT_LOOP_".

Пример интеграции в AppSettings (Pydantic v2):

    from config.bot_loop_settings import BotLoopSettings

    class AppSettings(BaseSettings):
        ...
        bot_loop: BotLoopSettings = BotLoopSettings()

Все параметры имеют разумные дефолты для paper trading.
Для production перед деплоем проверить:
  - DUST_THRESHOLD (зависит от тикера / биржи)
  - MAX_ENTRY_SLIPPAGE_PCT
  - CLOSE_REMAINDER_MODE

Переменные среды (с префиксом BOT_LOOP_):
  BOT_LOOP_TICK_INTERVAL_SEC=60
  BOT_LOOP_DCA_MODE=LAZY
  ...
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DCAMode(str, Enum):
    EAGER = "EAGER"
    LAZY  = "LAZY"


class CloseRemainderMode(str, Enum):
    KEEP_TP           = "KEEP_TP"
    LIMIT_WITH_TIMEOUT = "LIMIT_WITH_TIMEOUT"
    MARKET            = "MARKET"


class BotLoopSettings(BaseSettings):
    """
    Параметры tick-loop и торговой логики.

    Иерархия источников (Pydantic v2 + pydantic-settings):
      ENV-переменные > .env файл > дефолты ниже.
    Все числа: строки в env → Decimal конвертируется в BotLoop.__init__().
    """

    model_config = SettingsConfigDict(
        env_prefix="BOT_LOOP_",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # Тайминги
    # ------------------------------------------------------------------

    tick_interval_sec: int = Field(
        default=60,
        ge=1,
        description="Интервал между тиками в секундах.",
    )
    tick_max_duration_sec: int = Field(
        default=30,
        ge=1,
        description=(
            "Порог длительности тика. При превышении → WARNING в логах. "
            "Не останавливает бота."
        ),
    )
    heartbeat_interval_ticks: int = Field(
        default=5,
        ge=1,
        description="Обновлять heartbeat в bot_registry каждые N тиков.",
    )

    # ------------------------------------------------------------------
    # Торговая логика
    # ------------------------------------------------------------------

    dca_mode: DCAMode = Field(
        default=DCAMode.LAZY,
        description=(
            "LAZY  — DCA-ордера выставляются по одному при пробое уровня. "
            "EAGER — все DCA-ордера выставляются сразу при входе."
        ),
    )
    max_dca_count: int = Field(
        default=3,
        ge=0,
        description="Максимальное количество DCA-уровней за цикл.",
    )
    entry_order_timeout_sec: int = Field(
        default=3600,
        ge=0,
        description=(
            "Ордер на вход не исполнился за N секунд → отмена → IDLE. "
            "0 = не ограничено."
        ),
    )
    max_position_days: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Максимальное время удержания позиции в днях. "
            "None = не ограничено. "
            "При превышении → WARNING (и FORCE_CLOSE если включено)."
        ),
    )
    force_close_on_timeout: bool = Field(
        default=False,
        description=(
            "True = закрыть позицию market-ордером при превышении "
            "max_position_days. False = только WARNING."
        ),
    )

    # ------------------------------------------------------------------
    # Пороги
    # ------------------------------------------------------------------

    max_entry_slippage_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        description=(
            "Максимальное допустимое проскальзывание при входе (%). "
            "Если цена ушла дальше — вход пропускается (TickSkippedError)."
        ),
    )
    partial_fill_threshold_pct: float = Field(
        default=80.0,
        ge=1.0,
        le=100.0,
        description=(
            "Порог для частичного entry/DCA (%). "
            "Если исполнено >= N% — принять позицию и отменить остаток. "
            "Если < N% — отменить полностью и вернуться в IDLE."
        ),
    )
    tp_partial_close_threshold_pct: float = Field(
        default=80.0,
        ge=1.0,
        le=100.0,
        description=(
            "Порог для частичного TP (%). "
            "Если TP исполнен >= N% — запустить Close Protocol. "
            "Если < N% — обновить qty, оставаться в IN_POSITION."
        ),
    )
    dust_threshold: float = Field(
        default=0.001,
        ge=0.0,
        description=(
            "Минимальный остаток позиции считается 'пылью' и игнорируется. "
            "Зависит от тикера: BTCUSDT ≈ 0.0001, ETHUSDT ≈ 0.001."
        ),
    )

    # ------------------------------------------------------------------
    # Cooldown и задержки
    # ------------------------------------------------------------------

    cooldown_sec: int = Field(
        default=0,
        ge=0,
        description=(
            "Пауза после закрытия цикла перед открытием следующего (сек). "
            "0 = без паузы."
        ),
    )
    balance_drift_pct: float = Field(
        default=5.0,
        ge=0.0,
        le=100.0,
        description=(
            "Порог расхождения виртуального и реального баланса (%). "
            "При превышении → WARNING."
        ),
    )

    # ------------------------------------------------------------------
    # Закрытие остатка
    # ------------------------------------------------------------------

    close_remainder_mode: CloseRemainderMode = Field(
        default=CloseRemainderMode.KEEP_TP,
        description=(
            "Политика закрытия остатка позиции если TP исполнился частично. "
            "KEEP_TP            — TP остаётся на бирже (рекомендуется). "
            "LIMIT_WITH_TIMEOUT — лимит с таймаутом, затем MARKET. "
            "MARKET             — немедленное рыночное закрытие."
        ),
    )
    close_remainder_timeout_sec: int = Field(
        default=3600,
        ge=0,
        description=(
            "Таймаут для LIMIT_WITH_TIMEOUT (сек). "
            "По истечении → MARKET."
        ),
    )
    max_market_close_slippage_pct: float = Field(
        default=0.5,
        ge=0.0,
        le=100.0,
        description=(
            "Максимальное допустимое проскальзывание при рыночном закрытии (%). "
            "Если хуже — ордер откладывается."
        ),
    )

    # ------------------------------------------------------------------
    # Надёжность
    # ------------------------------------------------------------------

    critical_error_threshold: int = Field(
        default=5,
        ge=1,
        description=(
            "Количество последовательных RecoverableError после которых "
            "срабатывает KillSwitchError."
        ),
    )
    cancel_max_retries: int = Field(
        default=5,
        ge=1,
        description=(
            "Максимальное количество попыток cancel_order. "
            "После превышения → StopCraneError."
        ),
    )

    # ------------------------------------------------------------------
    # Параметры брокера
    # ------------------------------------------------------------------

    broker_request_timeout_sec: float = Field(
        default=5.0,
        ge=1.0,
        description=(
            "Таймаут create_order в секундах. "
            "При превышении → StopCraneError (без retry — исход неизвестен)."
        ),
    )
    broker_retry_delay_sec: float = Field(
        default=1.0,
        ge=0.1,
        description="Базовая задержка retry при сетевых ошибках (сек, exp backoff).",
    )
    broker_max_retries: int = Field(
        default=3,
        ge=0,
        description="Максимальное количество retry при сетевых ошибках create_order.",
    )

    # ------------------------------------------------------------------
    # Валидация
    # ------------------------------------------------------------------

    @field_validator("close_remainder_mode", mode="before")
    @classmethod
    def parse_close_remainder_mode(cls, v: str | CloseRemainderMode) -> CloseRemainderMode:
        if isinstance(v, str):
            try:
                return CloseRemainderMode(v.upper())
            except ValueError:
                raise ValueError(
                    f"Неверный close_remainder_mode: '{v}'. "
                    f"Допустимые: {[m.value for m in CloseRemainderMode]}"
                )
        return v

    @field_validator("dca_mode", mode="before")
    @classmethod
    def parse_dca_mode(cls, v: str | DCAMode) -> DCAMode:
        if isinstance(v, str):
            try:
                return DCAMode(v.upper())
            except ValueError:
                raise ValueError(
                    f"Неверный dca_mode: '{v}'. "
                    f"Допустимые: {[m.value for m in DCAMode]}"
                )
        return v
