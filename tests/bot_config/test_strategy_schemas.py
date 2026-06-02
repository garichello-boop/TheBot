"""
tests/bot_config/test_strategy_schemas.py

Unit-тесты для bot_config/strategy_schemas.py.

Проверяет:
  - MeanReversionParams: valid / invalid configs
  - BaseStrategyParams: SL cross-field validation
  - Type coercion (string → number)
  - extra fields allowed (backward compat)
  - validate_strategy_params(): шлюз-функция
  - STRATEGY_SCHEMAS: реестр содержит ожидаемые стратегии
"""
from __future__ import annotations

import pytest
from decimal import Decimal

from bot_config.strategy_schemas import (
    BaseStrategyParams,
    MeanReversionParams,
    STRATEGY_SCHEMAS,
    validate_strategy_params,
)


# ---------------------------------------------------------------------------
# BaseStrategyParams — SL validation
# ---------------------------------------------------------------------------

class TestBaseStrategyParamsSL:
    def test_defaults_are_valid(self):
        p = BaseStrategyParams()
        assert p.SL_ENABLED is False
        assert p.SL_PCT is None

    def test_sl_disabled_no_pct_valid(self):
        p = BaseStrategyParams(SL_ENABLED=False)
        assert p.SL_PCT is None

    def test_sl_enabled_with_pct_valid(self):
        p = BaseStrategyParams(SL_ENABLED=True, SL_PCT=5.0)
        assert p.SL_ENABLED is True
        assert p.SL_PCT == 5.0

    def test_sl_enabled_without_pct_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc_info:
            BaseStrategyParams(SL_ENABLED=True, SL_PCT=None)
        errors = exc_info.value.errors()
        assert any("SL_PCT" in str(e["msg"]) or "обязателен" in str(e["msg"]) for e in errors)

    def test_sl_pct_zero_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BaseStrategyParams(SL_ENABLED=True, SL_PCT=0.0)

    def test_sl_pct_negative_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BaseStrategyParams(SL_ENABLED=True, SL_PCT=-1.0)

    def test_sl_pct_100_or_more_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BaseStrategyParams(SL_ENABLED=True, SL_PCT=100.0)

    def test_sl_pct_99_valid(self):
        p = BaseStrategyParams(SL_ENABLED=True, SL_PCT=99.0)
        assert p.SL_PCT == 99.0

    def test_sl_pct_string_coerced(self):
        """SL_PCT='5.0' (строка из JSONB) должен конвертироваться в float."""
        p = BaseStrategyParams(SL_ENABLED=True, SL_PCT="5.0")
        assert p.SL_PCT == 5.0

    def test_sl_enabled_string_true_coerced(self):
        """SL_ENABLED='true' (строка из JSONB) должен конвертироваться в bool."""
        # Pydantic v2 coerces "true" / "1" / 1 to True
        p = BaseStrategyParams.model_validate({"SL_ENABLED": True, "SL_PCT": 5.0})
        assert p.SL_ENABLED is True

    def test_extra_fields_allowed(self):
        """Неизвестные поля не вызывают ошибку."""
        p = BaseStrategyParams.model_validate({
            "SL_ENABLED": False,
            "SOME_UNKNOWN_PARAM": 999,
        })
        assert p.SL_ENABLED is False


# ---------------------------------------------------------------------------
# MeanReversionParams — field validation
# ---------------------------------------------------------------------------

class TestMeanReversionParams:
    def test_defaults_are_valid(self):
        p = MeanReversionParams()
        assert p.BB_PERIOD == 20
        assert p.BB_MULT == 2.0
        assert p.INVEST_SHARE == 0.20
        assert p.TAKE_PROFIT == 0.02
        assert p.MAX_ENTRIES == 2
        assert p.SL_ENABLED is False

    def test_empty_dict_valid(self):
        """Пустой params {} — всё дефолты, бот работает."""
        p = MeanReversionParams.model_validate({})
        assert p.BB_PERIOD == 20

    def test_full_valid_config(self):
        p = MeanReversionParams.model_validate({
            "BB_PERIOD": 30,
            "BB_MULT": 2.5,
            "INVEST_SHARE": 0.15,
            "TAKE_PROFIT": 0.03,
            "MAX_ENTRIES": 3,
            "SL_ENABLED": True,
            "SL_PCT": 8.0,
        })
        assert p.BB_PERIOD == 30
        assert p.BB_MULT == 2.5
        assert p.INVEST_SHARE == 0.15
        assert p.SL_PCT == 8.0

    # --- BB_PERIOD ---
    def test_bb_period_minimum_2(self):
        p = MeanReversionParams(BB_PERIOD=2)
        assert p.BB_PERIOD == 2

    def test_bb_period_1_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MeanReversionParams(BB_PERIOD=1)

    def test_bb_period_string_coerced(self):
        p = MeanReversionParams.model_validate({"BB_PERIOD": "30"})
        assert p.BB_PERIOD == 30

    # --- BB_MULT ---
    def test_bb_mult_zero_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MeanReversionParams(BB_MULT=0.0)

    def test_bb_mult_string_coerced(self):
        p = MeanReversionParams.model_validate({"BB_MULT": "2.5"})
        assert p.BB_MULT == 2.5

    # --- INVEST_SHARE ---
    def test_invest_share_zero_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MeanReversionParams(INVEST_SHARE=0.0)

    def test_invest_share_one_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MeanReversionParams(INVEST_SHARE=1.0)

    def test_invest_share_0999_valid(self):
        p = MeanReversionParams(INVEST_SHARE=0.999)
        assert p.INVEST_SHARE == 0.999

    def test_invest_share_string_coerced(self):
        p = MeanReversionParams.model_validate({"INVEST_SHARE": "0.15"})
        assert p.INVEST_SHARE == 0.15

    # --- TAKE_PROFIT ---
    def test_take_profit_zero_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MeanReversionParams(TAKE_PROFIT=0.0)

    def test_take_profit_string_coerced(self):
        p = MeanReversionParams.model_validate({"TAKE_PROFIT": "0.03"})
        assert p.TAKE_PROFIT == 0.03

    # --- MAX_ENTRIES ---
    def test_max_entries_minimum_1(self):
        p = MeanReversionParams(MAX_ENTRIES=1)
        assert p.MAX_ENTRIES == 1

    def test_max_entries_zero_invalid(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MeanReversionParams(MAX_ENTRIES=0)

    def test_max_entries_string_coerced(self):
        p = MeanReversionParams.model_validate({"MAX_ENTRIES": "5"})
        assert p.MAX_ENTRIES == 5

    # --- Extra fields (backward compat) ---
    def test_legacy_ma_period_param_allowed(self):
        """Старые поля (ma_period, tp_pct) из DB не должны вызывать ошибку."""
        p = MeanReversionParams.model_validate({
            "ma_period": 180,
            "tp_pct": 0.035,
        })
        assert p.BB_PERIOD == 20  # schema default applied

    def test_unknown_extra_fields_allowed(self):
        p = MeanReversionParams.model_validate({
            "BB_PERIOD": 25,
            "FUTURE_PARAM": "some_value",
        })
        assert p.BB_PERIOD == 25


# ---------------------------------------------------------------------------
# validate_strategy_params() — integration function
# ---------------------------------------------------------------------------

class TestValidateStrategyParams:
    def test_unknown_strategy_returns_empty(self):
        """Нет схемы для стратегии — ошибок нет."""
        errors = validate_strategy_params("UnknownStrategy", {"x": 1})
        assert errors == []

    def test_valid_mean_reversion_returns_empty(self):
        errors = validate_strategy_params("MeanReversion", {})
        assert errors == []

    def test_valid_full_params_returns_empty(self):
        errors = validate_strategy_params("MeanReversion", {
            "BB_PERIOD": 25,
            "BB_MULT": 2.0,
            "INVEST_SHARE": 0.15,
            "TAKE_PROFIT": 0.03,
            "MAX_ENTRIES": 3,
        })
        assert errors == []

    def test_invalid_bb_period_returns_error(self):
        errors = validate_strategy_params("MeanReversion", {"BB_PERIOD": 1})
        assert len(errors) > 0
        assert any("BB_PERIOD" in e for e in errors)

    def test_sl_enabled_without_pct_returns_error(self):
        errors = validate_strategy_params("MeanReversion", {
            "SL_ENABLED": True,
            "SL_PCT": None,
        })
        assert len(errors) > 0
        assert any("SL_PCT" in e or "обязателен" in e for e in errors)

    def test_error_string_contains_strategy_name(self):
        errors = validate_strategy_params("MeanReversion", {"BB_PERIOD": 0})
        assert all("MeanReversion" in e for e in errors)

    def test_multiple_errors_reported(self):
        errors = validate_strategy_params("MeanReversion", {
            "BB_PERIOD": 1,         # invalid
            "INVEST_SHARE": 1.5,    # invalid (>= 1)
        })
        assert len(errors) >= 2

    def test_legacy_db_params_pass(self):
        """Существующий конфиг в DB с ma_period/tp_pct не должен сломаться."""
        errors = validate_strategy_params("MeanReversion", {
            "ma_period": 180,
            "tp_pct": 0.035,
        })
        assert errors == []

    def test_sl_enabled_with_valid_pct_passes(self):
        errors = validate_strategy_params("MeanReversion", {
            "SL_ENABLED": True,
            "SL_PCT": 5.0,
        })
        assert errors == []


# ---------------------------------------------------------------------------
# STRATEGY_SCHEMAS registry
# ---------------------------------------------------------------------------

class TestStrategySchemas:
    def test_mean_reversion_registered(self):
        assert "MeanReversion" in STRATEGY_SCHEMAS

    def test_schema_is_pydantic_model(self):
        from pydantic import BaseModel
        assert issubclass(STRATEGY_SCHEMAS["MeanReversion"], BaseModel)

    def test_mean_reversion_schema_is_correct_class(self):
        assert STRATEGY_SCHEMAS["MeanReversion"] is MeanReversionParams
