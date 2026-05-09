from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bot_config.models import BotConfig, BotStatus, CycleSnapshot
from helpers import make_row


class TestBotStatus:
    def test_all_values_parseable(self):
        for value in ("ACTIVE", "CLOSE_ONLY", "STOPPED", "FORCE_CLOSE"):
            assert BotStatus(value).value == value

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            BotStatus("PAUSED")


class TestBotConfigFromRow:
    def test_valid_row_builds_config(self, valid_row):
        config = BotConfig.from_row(valid_row)
        assert config.user_id        == "alex"
        assert config.bot_id         == "btc_test"
        assert config.ticker         == "BTCUSDT"
        assert config.strategy_name  == "MeanReversion"
        assert config.config_version == 1
        assert config.status         == BotStatus.ACTIVE

    def test_virtual_balance_is_decimal(self, valid_row):
        config = BotConfig.from_row(valid_row)
        assert isinstance(config.virtual_balance, Decimal)
        assert config.virtual_balance == Decimal("1000.00")

    def test_virtual_balance_from_string(self):
        row = make_row(virtual_balance="500.50")
        config = BotConfig.from_row(row)
        assert config.virtual_balance == Decimal("500.50")

    def test_strategy_params_is_copy(self, valid_row):
        config = BotConfig.from_row(valid_row)
        valid_row["strategy_params"]["ma_period"] = 999
        assert config.strategy_params["ma_period"] == 180

    def test_empty_strategy_params(self):
        config = BotConfig.from_row(make_row(strategy_params={}))
        assert config.strategy_params == {}

    def test_none_strategy_params_becomes_empty_dict(self):
        config = BotConfig.from_row(make_row(strategy_params=None))
        assert config.strategy_params == {}

    def test_status_parsed_as_enum(self, valid_row):
        config = BotConfig.from_row(valid_row)
        assert isinstance(config.status, BotStatus)

    def test_all_statuses_parsed(self):
        for status in BotStatus:
            config = BotConfig.from_row(make_row(status=status.value))
            assert config.status == status

    def test_frozen_prevents_mutation(self, valid_row):
        from dataclasses import FrozenInstanceError
        config = BotConfig.from_row(valid_row)
        with pytest.raises(FrozenInstanceError):
            config.ticker = "ETHUSDT"  # type: ignore[misc]


class TestBotConfigHelpers:
    def test_allows_new_cycles_only_for_active(self):
        for status in BotStatus:
            config = BotConfig.from_row(make_row(status=status.value))
            assert config.allows_new_cycles() == (status == BotStatus.ACTIVE)

    def test_is_active(self):
        assert BotConfig.from_row(make_row(status="ACTIVE")).is_active() is True
        assert BotConfig.from_row(make_row(status="STOPPED")).is_active() is False

    def test_repr_contains_key_fields(self, valid_config):
        r = repr(valid_config)
        assert "btc_test" in r
        assert "BTCUSDT" in r
        assert "MeanReversion" in r
        assert "ACTIVE" in r


class TestCycleSnapshot:
    def test_from_config_copies_params(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        assert snapshot.strategy_params == {"ma_period": 180, "tp_pct": 0.035}
        assert snapshot.config_version  == 1

    def test_from_config_is_independent_copy(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        valid_config.strategy_params["ma_period"] = 999
        assert snapshot.strategy_params["ma_period"] == 180

    def test_get_returns_value(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        assert snapshot.get("ma_period") == 180

    def test_get_returns_default_when_missing(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        assert snapshot.get("nonexistent", default=42) == 42
        assert snapshot.get("nonexistent") is None

    def test_require_returns_value(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        assert snapshot.require("ma_period") == 180

    def test_require_raises_on_missing_key(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        with pytest.raises(KeyError, match="missing_param"):
            snapshot.require("missing_param")

    def test_require_error_lists_available_keys(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        with pytest.raises(KeyError) as exc_info:
            snapshot.require("missing_param")
        assert "ma_period" in str(exc_info.value)

    def test_started_at_is_timezone_aware(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        assert snapshot.started_at.tzinfo is not None

    def test_frozen_prevents_mutation(self, valid_config):
        from dataclasses import FrozenInstanceError
        snapshot = CycleSnapshot.from_config(valid_config)
        with pytest.raises(FrozenInstanceError):
            snapshot.config_version = 99  # type: ignore[misc]

    def test_repr_contains_version(self, valid_config):
        snapshot = CycleSnapshot.from_config(valid_config)
        assert "version=1" in repr(snapshot)
