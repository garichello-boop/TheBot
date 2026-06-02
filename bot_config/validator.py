"""
bot_config/validator.py

Validates BotConfig loaded from PostgreSQL.

Design: validator returns a ValidationResult — it never raises.
The caller (ConfigWatcher / ConfigRepository) decides what to do:
  - on startup failure: abort bot launch
  - on hot-reload failure: keep old config, emit CONFIG_ERROR alert

Strategy-specific param validation is pluggable via register_strategy().
Pydantic schemas per strategy are a planned improvement (Point 5 backlog).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from .models import BotConfig, BotStatus

logger = logging.getLogger(__name__)

# Type alias: a strategy validator receives strategy_params dict
# and returns a list of error strings (empty = valid).
StrategyValidator = Callable[[dict[str, Any]], list[str]]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationResult:
    """
    Returned by ConfigValidator.validate().
    Never raises — caller handles errors explicitly.
    """
    is_valid: bool
    errors:   tuple[str, ...]   # frozen: consistent with immutable design

    @classmethod
    def ok(cls) -> ValidationResult:
        return cls(is_valid=True, errors=())

    @classmethod
    def fail(cls, errors: list[str]) -> ValidationResult:
        return cls(is_valid=False, errors=tuple(errors))

    def __str__(self) -> str:
        if self.is_valid:
            return "ValidationResult(ok)"
        return "ValidationResult(errors=[" + "; ".join(self.errors) + "])"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ConfigValidator:
    """
    Validates BotConfig before it is used by the bot.

    Two layers of checks:
      1. Structural — fields that must always be valid regardless of strategy.
      2. Strategy-specific — registered per strategy_name via register_strategy().

    Usage:
        validator = ConfigValidator()
        validator.register_strategy("MeanReversion", _validate_mean_reversion)

        result = validator.validate(config)
        if not result.is_valid:
            # soft-fail: keep old config, alert
            ...
    """

    def __init__(self) -> None:
        self._strategy_validators: dict[str, StrategyValidator] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_strategy(self, strategy_name: str, validator: StrategyValidator) -> None:
        """
        Register a param validator for a specific strategy.
        The validator receives strategy_params dict and returns a list of
        error strings. Empty list means valid.
        """
        self._strategy_validators[strategy_name] = validator
        logger.debug("ConfigValidator: registered validator for %r.", strategy_name)

    # ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    def validate(self, config: BotConfig) -> ValidationResult:
        """
        Run all checks. Returns ValidationResult without raising.
        Logs a warning for every error found.
        """
        errors: list[str] = []

        self._check_identifiers(config, errors)
        self._check_market(config, errors)
        self._check_balance(config, errors)
        self._check_status(config, errors)
        self._check_version(config, errors)
        has_schema = self._check_strategy_params(config, errors)
        if not has_schema:
            self._check_sl_params(config, errors)

        if errors:
            for e in errors:
                logger.warning(
                    "ConfigValidator [%s/%s]: %s", config.user_id, config.bot_id, e
                )
            return ValidationResult.fail(errors)

        return ValidationResult.ok()

    # ------------------------------------------------------------------
    # Structural checks
    # ------------------------------------------------------------------

    def _check_identifiers(self, config: BotConfig, errors: list[str]) -> None:
        if not config.user_id or not config.user_id.strip():
            errors.append("user_id is empty.")
        if not config.bot_id or not config.bot_id.strip():
            errors.append("bot_id is empty.")

    def _check_market(self, config: BotConfig, errors: list[str]) -> None:
        if not config.ticker or not config.ticker.strip():
            errors.append("ticker is empty.")
        if not config.strategy_name or not config.strategy_name.strip():
            errors.append("strategy_name is empty.")

    def _check_balance(self, config: BotConfig, errors: list[str]) -> None:
        if not isinstance(config.virtual_balance, Decimal):
            errors.append(
                f"virtual_balance must be Decimal, got {type(config.virtual_balance).__name__}."
            )
        elif config.virtual_balance < Decimal("0"):
            errors.append(
                f"virtual_balance must be >= 0, got {config.virtual_balance}."
            )

    def _check_status(self, config: BotConfig, errors: list[str]) -> None:
        try:
            BotStatus(config.status.value)
        except ValueError:
            errors.append(
                f"status {config.status!r} is not a valid BotStatus. "
                f"Valid values: {[s.value for s in BotStatus]}."
            )

    def _check_version(self, config: BotConfig, errors: list[str]) -> None:
        if config.config_version < 1:
            errors.append(
                f"config_version must be >= 1, got {config.config_version}."
            )

    def _check_strategy_params(self, config: BotConfig, errors: list[str]) -> bool:
        """
        Validate strategy_params.

        Returns True if a Pydantic schema was found and used for this strategy
        (signals to the caller that SL params are already covered by the schema).
        Returns False if only legacy callback validation (or no validation) ran.
        """
        if not isinstance(config.strategy_params, dict):
            errors.append(
                f"strategy_params must be a dict, got "
                f"{type(config.strategy_params).__name__}."
            )
            return False

        has_schema = False

        # Layer 1: Pydantic schema validation (if registered for this strategy)
        try:
            from bot_config.strategy_schemas import (  # noqa: PLC0415
                validate_strategy_params,
                STRATEGY_SCHEMAS,
            )
            if config.strategy_name in STRATEGY_SCHEMAS:
                schema_errors = validate_strategy_params(
                    config.strategy_name, config.strategy_params
                )
                errors.extend(schema_errors)
                has_schema = True
        except ImportError:
            pass  # schemas module not yet available

        # Layer 2: legacy callback validators (run additionally if registered)
        strategy_validator = self._strategy_validators.get(config.strategy_name)
        if strategy_validator is not None:
            callback_errors = strategy_validator(config.strategy_params)
            errors.extend(callback_errors)
        elif not has_schema:
            logger.debug(
                "ConfigValidator: no strategy validator registered for %r, skipping.",
                config.strategy_name,
            )

        return has_schema

    def _check_sl_params(self, config: BotConfig, errors: list[str]) -> None:
        """
        Проверить параметры Stop-Loss в strategy_params (ТЗ-7 StopLoss §13 шаг 2).

        Правила:
          - SL_ENABLED необязателен; дефолт False (SL выключен).
          - Если SL_ENABLED=true — SL_PCT обязателен и должен быть > 0.
          - Если SL_ENABLED=true и SL_PCT не задан — WARNING при горячей
            перезагрузке, ошибка при первом запуске.

        Проверка выполняется для всех стратегий — SL это риск-менеджмент,
        не стратегическая логика.
        """
        if not isinstance(config.strategy_params, dict):
            return  # уже поймано в _check_strategy_params

        sl_enabled = config.strategy_params.get("SL_ENABLED", False)

        if not sl_enabled:
            return  # SL выключен — параметры не требуются

        sl_pct = config.strategy_params.get("SL_PCT")

        if sl_pct is None:
            errors.append(
                "SL_ENABLED=true но SL_PCT не задан в strategy_params. "
                "Укажите SL_PCT > 0 или установите SL_ENABLED=false."
            )
            return

        try:
            sl_pct_float = float(sl_pct)
        except (TypeError, ValueError):
            errors.append(
                f"SL_PCT должен быть числом > 0, получено: {sl_pct!r}."
            )
            return

        if sl_pct_float <= 0:
            errors.append(
                f"SL_PCT должен быть > 0 при SL_ENABLED=true, получено: {sl_pct_float}."
            )

        if sl_pct_float >= 100:
            errors.append(
                f"SL_PCT={sl_pct_float} выглядит некорректно (>= 100%). "
                f"Укажите значение в процентах, например 5.0 для 5%."
            )
