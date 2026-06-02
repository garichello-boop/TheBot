from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from bot_config.models import BotConfig, BotStatus
from bot_config.repository import (
    ConfigRepository, BotConfigNotFoundError,
    BotAlreadyRunningError, BotConfigInvalidError, _advisory_key,
)
from bot_config.validator import ConfigValidator
from .helpers import make_row


def _make_transaction_mock(row):
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = row
    @contextmanager
    def _transaction():
        yield cursor_mock
    return _transaction, cursor_mock


def _make_lock_conn_mock(acquired: bool):
    cur_mock = MagicMock()
    cur_mock.__enter__ = MagicMock(return_value=cur_mock)
    cur_mock.__exit__ = MagicMock(return_value=False)
    cur_mock.fetchone.return_value = [acquired]
    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cur_mock
    conn_mock.dsn = "postgresql://postgres:@localhost:5432/thebot"
    return conn_mock


def _make_pool_conn_mock():
    conn_mock = MagicMock()
    conn_mock.dsn = "postgresql://postgres:@localhost:5432/thebot"
    @contextmanager
    def _get_connection():
        yield conn_mock
    return _get_connection


class TestAdvisoryKey:
    def test_deterministic(self):
        assert _advisory_key("alex", "bot1") == _advisory_key("alex", "bot1")

    def test_different_pairs_differ(self):
        assert _advisory_key("alex", "bot1") != _advisory_key("alex", "bot2")
        assert _advisory_key("alex", "bot1") != _advisory_key("bob",  "bot1")

    def test_returns_int(self):
        assert isinstance(_advisory_key("u", "b"), int)

    def test_fits_int64(self):
        key = _advisory_key("u", "b")
        assert -(2**63) <= key <= (2**63 - 1)


class TestConfigRepositoryLoad:
    def _make_repo(self):
        return ConfigRepository(db_pool=MagicMock(), validator=ConfigValidator())

    def test_load_returns_bot_config(self):
        transaction_mock, _ = _make_transaction_mock(make_row())
        lock_conn = _make_lock_conn_mock(acquired=True)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.transaction", transaction_mock),
              patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            config = repo.load("alex", "btc_test")
        assert isinstance(config, BotConfig)
        assert config.bot_id == "btc_test"

    def test_load_acquires_lock(self):
        transaction_mock, _ = _make_transaction_mock(make_row())
        lock_conn = _make_lock_conn_mock(acquired=True)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.transaction", transaction_mock),
              patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            repo.load("alex", "btc_test")
        assert repo._lock_conn is not None

    def test_load_releases_lock_on_release(self):
        transaction_mock, _ = _make_transaction_mock(make_row())
        lock_conn = _make_lock_conn_mock(acquired=True)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.transaction", transaction_mock),
              patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            repo.load("alex", "btc_test")
            repo.release("alex", "btc_test")
        lock_conn.close.assert_called_once()
        assert repo._lock_conn is None


class TestConfigRepositoryLoadFailures:
    def _make_repo(self):
        return ConfigRepository(db_pool=MagicMock(), validator=ConfigValidator())

    def test_raises_already_running_when_lock_not_acquired(self):
        lock_conn = _make_lock_conn_mock(acquired=False)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            with pytest.raises(BotAlreadyRunningError, match="already running"):
                repo.load("alex", "btc_test")

    def test_lock_conn_none_after_already_running(self):
        lock_conn = _make_lock_conn_mock(acquired=False)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            with pytest.raises(BotAlreadyRunningError):
                repo.load("alex", "btc_test")
        assert repo._lock_conn is None

    def test_raises_not_found_when_row_missing(self):
        transaction_mock, _ = _make_transaction_mock(row=None)
        lock_conn = _make_lock_conn_mock(acquired=True)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.transaction", transaction_mock),
              patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            with pytest.raises(BotConfigNotFoundError):
                repo.load("alex", "btc_test")

    def test_lock_released_when_row_not_found(self):
        transaction_mock, _ = _make_transaction_mock(row=None)
        lock_conn = _make_lock_conn_mock(acquired=True)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.transaction", transaction_mock),
              patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            with pytest.raises(BotConfigNotFoundError):
                repo.load("alex", "btc_test")
        assert repo._lock_conn is None

    def test_raises_invalid_when_validation_fails(self):
        transaction_mock, _ = _make_transaction_mock(make_row(virtual_balance=Decimal("-100")))
        lock_conn = _make_lock_conn_mock(acquired=True)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.transaction", transaction_mock),
              patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            with pytest.raises(BotConfigInvalidError):
                repo.load("alex", "btc_test")

    def test_lock_released_when_validation_fails(self):
        transaction_mock, _ = _make_transaction_mock(make_row(virtual_balance=Decimal("-100")))
        lock_conn = _make_lock_conn_mock(acquired=True)
        get_conn = _make_pool_conn_mock()
        repo = self._make_repo()
        with (patch("bot_config.repository.transaction", transaction_mock),
              patch("bot_config.repository.get_connection", get_conn),
              patch("bot_config.repository.psycopg2.connect", return_value=lock_conn)):
            with pytest.raises(BotConfigInvalidError):
                repo.load("alex", "btc_test")
        assert repo._lock_conn is None


class TestConfigRepositoryReload:
    def _make_repo(self):
        return ConfigRepository(db_pool=MagicMock(), validator=ConfigValidator())

    def test_reload_returns_config_and_result(self):
        transaction_mock, _ = _make_transaction_mock(make_row())
        repo = self._make_repo()
        with patch("bot_config.repository.transaction", transaction_mock):
            config, result = repo.reload("alex", "btc_test")
        assert isinstance(config, BotConfig)
        assert result.is_valid

    def test_reload_invalid_returns_failed_result(self):
        transaction_mock, _ = _make_transaction_mock(make_row(virtual_balance=Decimal("-1")))
        repo = self._make_repo()
        with patch("bot_config.repository.transaction", transaction_mock):
            config, result = repo.reload("alex", "btc_test")
        assert not result.is_valid
        assert isinstance(config, BotConfig)

    def test_reload_raises_not_found_when_row_gone(self):
        transaction_mock, _ = _make_transaction_mock(row=None)
        repo = self._make_repo()
        with patch("bot_config.repository.transaction", transaction_mock):
            with pytest.raises(BotConfigNotFoundError):
                repo.reload("alex", "btc_test")
