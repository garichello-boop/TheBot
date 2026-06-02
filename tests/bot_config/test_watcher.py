from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from bot_config.models import BotConfig, CycleSnapshot
from bot_config.repository import BotConfigNotFoundError
from bot_config.validator import ValidationResult
from bot_config.watcher import ConfigWatcher, WatchResult
from .helpers import make_row


def _make_config(version: int = 1, **overrides) -> BotConfig:
    return BotConfig.from_row(make_row(config_version=version, **overrides))


def _make_repo_mock(reload_config=None, reload_valid=True):
    repo = MagicMock()
    config = reload_config or _make_config(version=2)
    result = ValidationResult.ok() if reload_valid else ValidationResult.fail(["bad param"])
    repo.reload.return_value = (config, result)
    return repo


class TestWatchResult:
    def test_unchanged_flags(self):
        config = _make_config(version=3)
        r = WatchResult.unchanged(config)
        assert r.config_unchanged is True
        assert r.config_changed   is False
        assert r.reload_failed    is False
        assert r.config           is config
        assert r.prev_version     == 3
        assert r.curr_version     == 3
        assert r.errors           == ()

    def test_changed_flags(self):
        old, new = _make_config(version=1), _make_config(version=2)
        r = WatchResult.changed(old, new)
        assert r.config_changed is True
        assert r.config         is new
        assert r.prev_version   == 1
        assert r.curr_version   == 2

    def test_failed_keeps_old_config(self):
        old = _make_config(version=1)
        r = WatchResult.failed(old, new_version=2, result=ValidationResult.fail(["bad param"]))
        assert r.reload_failed  is True
        assert r.config         is old
        assert r.prev_version   == 1
        assert r.curr_version   == 2
        assert "bad param"      in r.errors

    def test_str_unchanged(self):
        assert "unchanged" in str(WatchResult.unchanged(_make_config(version=5)))

    def test_str_changed(self):
        assert "changed" in str(WatchResult.changed(_make_config(1), _make_config(2)))

    def test_str_failed(self):
        r = WatchResult.failed(_make_config(1), 2, ValidationResult.fail(["oops"]))
        assert "reload_failed" in str(r)
        assert "oops" in str(r)


class TestConfigWatcherInit:
    def test_get_config_before_initialize_raises(self):
        with pytest.raises(RuntimeError, match="initialize"):
            ConfigWatcher(MagicMock()).get_config()

    def test_check_before_initialize_raises(self):
        with pytest.raises(RuntimeError, match="initialize"):
            ConfigWatcher(MagicMock()).check_and_reload("alex", "btc_test")

    def test_get_config_after_initialize(self):
        config = _make_config()
        watcher = ConfigWatcher(MagicMock())
        watcher.initialize(config)
        assert watcher.get_config() is config


class TestConfigWatcherUnchanged:
    def test_unchanged_when_version_same(self):
        config = _make_config(version=1)
        watcher = ConfigWatcher(MagicMock())
        watcher.initialize(config)
        with patch.object(watcher, "_fetch_version", return_value=1):
            result = watcher.check_and_reload("alex", "btc_test")
        assert result.config_unchanged is True
        assert result.config is config

    def test_unchanged_does_not_call_repo(self):
        repo = MagicMock()
        watcher = ConfigWatcher(repo)
        watcher.initialize(_make_config(version=5))
        with patch.object(watcher, "_fetch_version", return_value=5):
            watcher.check_and_reload("alex", "btc_test")
        repo.reload.assert_not_called()


class TestConfigWatcherChanged:
    def test_returns_changed_with_new_config(self):
        old = _make_config(version=1)
        new = _make_config(version=2, ticker="ETHUSDT")
        watcher = ConfigWatcher(_make_repo_mock(reload_config=new, reload_valid=True))
        watcher.initialize(old)
        with patch.object(watcher, "_fetch_version", return_value=2):
            result = watcher.check_and_reload("alex", "btc_test")
        assert result.config_changed is True
        assert result.config is new
        assert result.prev_version == 1
        assert result.curr_version == 2

    def test_cached_config_updated_after_reload(self):
        old = _make_config(version=1)
        new = _make_config(version=2)
        watcher = ConfigWatcher(_make_repo_mock(reload_config=new, reload_valid=True))
        watcher.initialize(old)
        with patch.object(watcher, "_fetch_version", return_value=2):
            watcher.check_and_reload("alex", "btc_test")
        assert watcher.get_config() is new

    def test_repo_reload_called_with_correct_args(self):
        old = _make_config(version=1)
        repo = _make_repo_mock(reload_config=_make_config(version=2), reload_valid=True)
        watcher = ConfigWatcher(repo)
        watcher.initialize(old)
        with patch.object(watcher, "_fetch_version", return_value=2):
            watcher.check_and_reload("alex", "btc_test")
        repo.reload.assert_called_once_with("alex", "btc_test")


class TestConfigWatcherReloadFailed:
    def test_soft_fail_keeps_old_config(self):
        old = _make_config(version=1)
        watcher = ConfigWatcher(_make_repo_mock(reload_valid=False))
        watcher.initialize(old)
        with patch.object(watcher, "_fetch_version", return_value=2):
            result = watcher.check_and_reload("alex", "btc_test")
        assert result.reload_failed is True
        assert result.config is old

    def test_cached_config_unchanged_after_failed_reload(self):
        old = _make_config(version=1)
        watcher = ConfigWatcher(_make_repo_mock(reload_valid=False))
        watcher.initialize(old)
        with patch.object(watcher, "_fetch_version", return_value=2):
            watcher.check_and_reload("alex", "btc_test")
        assert watcher.get_config() is old

    def test_failed_result_contains_errors(self):
        old = _make_config(version=1)
        repo = MagicMock()
        repo.reload.return_value = (
            _make_config(version=2),
            ValidationResult.fail(["ma_period required", "tp_pct must be > 0"]),
        )
        watcher = ConfigWatcher(repo)
        watcher.initialize(old)
        with patch.object(watcher, "_fetch_version", return_value=2):
            result = watcher.check_and_reload("alex", "btc_test")
        assert "ma_period required" in result.errors
        assert "tp_pct must be > 0" in result.errors

    def test_does_not_raise_on_invalid_reload(self):
        old = _make_config(version=1)
        watcher = ConfigWatcher(_make_repo_mock(reload_valid=False))
        watcher.initialize(old)
        with patch.object(watcher, "_fetch_version", return_value=2):
            result = watcher.check_and_reload("alex", "btc_test")
        assert result.reload_failed is True


class TestConfigWatcherDBErrors:
    def test_not_found_propagates(self):
        watcher = ConfigWatcher(MagicMock())
        watcher.initialize(_make_config(version=1))
        with patch.object(watcher, "_fetch_version",
                          side_effect=BotConfigNotFoundError("gone")):
            with pytest.raises(BotConfigNotFoundError):
                watcher.check_and_reload("alex", "btc_test")
