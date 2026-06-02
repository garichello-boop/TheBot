from decimal import Decimal
import pytest

from bot_config.models import BotConfig, BotStatus
from bot_config.validator import ConfigValidator, ValidationResult
from .helpers import make_row


class TestValidationResult:
    def test_ok_is_valid(self):
        r = ValidationResult.ok()
        assert r.is_valid is True
        assert r.errors  == ()

    def test_fail_is_invalid(self):
        r = ValidationResult.fail(["error one", "error two"])
        assert r.is_valid is False
        assert "error one" in r.errors
        assert "error two" in r.errors

    def test_fail_errors_are_tuple(self):
        assert isinstance(ValidationResult.fail(["e"]).errors, tuple)

    def test_str_ok(self):
        assert "ok" in str(ValidationResult.ok())

    def test_str_fail_contains_errors(self):
        assert "bad field" in str(ValidationResult.fail(["bad field"]))


class TestConfigValidatorStructural:
    def setup_method(self):
        self.validator = ConfigValidator()

    def _config(self, **overrides) -> BotConfig:
        return BotConfig.from_row(make_row(**overrides))

    def test_valid_config_passes(self):
        assert self.validator.validate(self._config()).is_valid

    def test_empty_user_id_fails(self):
        result = self.validator.validate(self._config(user_id=""))
        assert not result.is_valid
        assert any("user_id" in e for e in result.errors)

    def test_whitespace_user_id_fails(self):
        assert not self.validator.validate(self._config(user_id="   ")).is_valid

    def test_empty_bot_id_fails(self):
        result = self.validator.validate(self._config(bot_id=""))
        assert not result.is_valid
        assert any("bot_id" in e for e in result.errors)

    def test_empty_ticker_fails(self):
        result = self.validator.validate(self._config(ticker=""))
        assert not result.is_valid
        assert any("ticker" in e for e in result.errors)

    def test_empty_strategy_name_fails(self):
        result = self.validator.validate(self._config(strategy_name=""))
        assert not result.is_valid
        assert any("strategy_name" in e for e in result.errors)

    def test_negative_virtual_balance_fails(self):
        result = self.validator.validate(self._config(virtual_balance=Decimal("-0.01")))
        assert not result.is_valid
        assert any("virtual_balance" in e for e in result.errors)

    def test_zero_virtual_balance_passes(self):
        assert self.validator.validate(self._config(virtual_balance=Decimal("0"))).is_valid

    def test_config_version_zero_fails(self):
        result = self.validator.validate(self._config(config_version=0))
        assert not result.is_valid
        assert any("config_version" in e for e in result.errors)

    def test_multiple_errors_reported_together(self):
        result = self.validator.validate(self._config(user_id="", ticker="", config_version=0))
        assert not result.is_valid
        assert len(result.errors) >= 3


class TestConfigValidatorStrategyPlugins:
    def setup_method(self):
        self.validator = ConfigValidator()

    def _config(self, **overrides) -> BotConfig:
        return BotConfig.from_row(make_row(**overrides))

    def test_no_registered_validator_passes(self):
        config = self._config(strategy_name="UnknownStrategy", strategy_params={"x": 1})
        assert self.validator.validate(config).is_valid

    def test_registered_validator_called(self):
        called_with = {}
        def my_validator(params):
            called_with["params"] = params
            return []
        self.validator.register_strategy("MeanReversion", my_validator)
        self.validator.validate(self._config(strategy_params={"ma_period": 180}))
        assert called_with["params"] == {"ma_period": 180}

    def test_strategy_validator_errors_propagate(self):
        self.validator.register_strategy("MeanReversion",
            lambda p: ["ma_period is required", "tp_pct must be positive"])
        result = self.validator.validate(self._config())
        assert not result.is_valid
        assert "ma_period is required" in result.errors

    def test_strategy_validator_can_pass(self):
        self.validator.register_strategy("MeanReversion",
            lambda p: [] if "ma_period" in p else ["ma_period required"])
        result = self.validator.validate(self._config(strategy_params={"ma_period": 180}))
        assert result.is_valid

    def test_register_overwrites_previous(self):
        self.validator.register_strategy("MeanReversion", lambda p: ["error v1"])
        self.validator.register_strategy("MeanReversion", lambda p: [])
        assert self.validator.validate(self._config()).is_valid
