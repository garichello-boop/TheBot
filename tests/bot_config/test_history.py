"""
tests/bot_config/test_history.py

Tests for:
    ConfigHistoryRow        — model parsing (models.py)
    ConfigRepository.get_history()  — audit trail read
    ConfigRepository.rollback()     — strategy_params restore
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest

from bot_config.models import BotConfig, BotStatus, ConfigHistoryRow
from bot_config.repository import (
    BotConfigNotFoundError,
    ConfigHistoryNotFoundError,
    ConfigRepository,
)
from .helpers import make_row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_history_row(**overrides) -> dict:
    """Build a valid bot_configs_history row dict."""
    row = {
        "id":              1,
        "user_id":         "alex",
        "bot_id":          "btc_test",
        "config_version":  2,
        "ticker":          "BTCUSDT",
        "exchange":        "bybit",
        "strategy_name":   "MeanReversion",
        "strategy_params": {"ma_period": 120, "tp_pct": 0.02},
        "virtual_balance": Decimal("1000.00"),
        "status":          "ACTIVE",
        "changed_by":      "wfo",
        "changed_at":      datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


def _make_repo():
    return ConfigRepository(db_pool=MagicMock())


def _single_fetchone(row):
    """transaction() mock: one fetchone() call."""
    cur = MagicMock()
    cur.fetchone.return_value = row

    @contextmanager
    def _tx():
        yield cur

    return _tx, cur


def _multi_fetchone(rows: list):
    """transaction() mock: successive fetchone() calls return rows in order."""
    cur = MagicMock()
    cur.fetchone.side_effect = rows

    @contextmanager
    def _tx():
        yield cur

    return _tx, cur


def _fetchall(rows):
    """transaction() mock: fetchall() returns rows."""
    cur = MagicMock()
    cur.fetchall.return_value = rows

    @contextmanager
    def _tx():
        yield cur

    return _tx, cur


# ---------------------------------------------------------------------------
# ConfigHistoryRow — model parsing
# ---------------------------------------------------------------------------

class TestConfigHistoryRow:
    def test_from_row_builds_dataclass(self):
        h = ConfigHistoryRow.from_row(make_history_row())
        assert h.id == 1
        assert h.user_id == "alex"
        assert h.bot_id == "btc_test"
        assert h.config_version == 2
        assert h.ticker == "BTCUSDT"
        assert h.strategy_name == "MeanReversion"
        assert h.strategy_params == {"ma_period": 120, "tp_pct": 0.02}
        assert h.changed_by == "wfo"
        assert isinstance(h.status, BotStatus)

    def test_from_row_converts_virtual_balance_string_to_decimal(self):
        h = ConfigHistoryRow.from_row(make_history_row(virtual_balance="500.50"))
        assert isinstance(h.virtual_balance, Decimal)
        assert h.virtual_balance == Decimal("500.50")

    def test_from_row_keeps_decimal_virtual_balance(self):
        h = ConfigHistoryRow.from_row(make_history_row(virtual_balance=Decimal("750")))
        assert isinstance(h.virtual_balance, Decimal)
        assert h.virtual_balance == Decimal("750")

    def test_from_row_parses_status_enum(self):
        h = ConfigHistoryRow.from_row(make_history_row(status="STOPPED"))
        assert h.status == BotStatus.STOPPED

    def test_from_row_strategy_params_is_a_copy(self):
        src = {"ma_period": 90}
        h = ConfigHistoryRow.from_row(make_history_row(strategy_params=src))
        src["ma_period"] = 999
        assert h.strategy_params["ma_period"] == 90

    def test_from_row_handles_none_strategy_params(self):
        h = ConfigHistoryRow.from_row(make_history_row(strategy_params=None))
        assert h.strategy_params == {}

    def test_repr_contains_bot_id_version_changed_by(self):
        h = ConfigHistoryRow.from_row(make_history_row())
        r = repr(h)
        assert "btc_test" in r
        assert "version=2" in r
        assert "changed_by='wfo'" in r

    def test_frozen_dataclass_raises_on_mutation(self):
        h = ConfigHistoryRow.from_row(make_history_row())
        with pytest.raises((AttributeError, TypeError)):
            h.changed_by = "hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------

class TestGetHistory:
    def test_returns_list_of_history_rows(self):
        rows = [
            make_history_row(id=3, config_version=3, changed_by="wfo"),
            make_history_row(id=2, config_version=2, changed_by="operator"),
            make_history_row(id=1, config_version=1, changed_by="unknown"),
        ]
        tx, _ = _fetchall(rows)
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            history = repo.get_history("alex", "btc_test")
        assert len(history) == 3
        assert all(isinstance(h, ConfigHistoryRow) for h in history)

    def test_empty_list_when_no_history(self):
        tx, _ = _fetchall([])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            assert repo.get_history("alex", "btc_test") == []

    def test_none_fetchall_treated_as_empty(self):
        tx, _ = _fetchall(None)
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            assert repo.get_history("alex", "btc_test") == []

    def test_changed_by_values_preserved(self):
        rows = [
            make_history_row(config_version=2, changed_by="wfo"),
            make_history_row(config_version=1, changed_by="unknown"),
        ]
        tx, _ = _fetchall(rows)
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            history = repo.get_history("alex", "btc_test")
        assert history[0].changed_by == "wfo"
        assert history[1].changed_by == "unknown"

    def test_limit_passed_to_query(self):
        tx, cur = _fetchall([])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.get_history("alex", "btc_test", limit=5)
        params = cur.execute.call_args[0][1]
        assert 5 in params

    def test_default_limit_is_20(self):
        tx, cur = _fetchall([])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.get_history("alex", "btc_test")
        params = cur.execute.call_args[0][1]
        assert 20 in params

    def test_user_id_and_bot_id_passed_to_query(self):
        tx, cur = _fetchall([])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.get_history("igor", "btc_paper_01")
        params = cur.execute.call_args[0][1]
        assert "igor" in params
        assert "btc_paper_01" in params


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

class TestRollback:
    """
    rollback() executes 3 execute() calls in one transaction:
        [0]  SET LOCAL app.changed_by = %s
        [1]  SELECT strategy_params FROM bot_configs_history ...
        [2]  UPDATE bot_configs ... RETURNING ...

    fetchone() is called twice:
        [0]  after SELECT — returns history row or None
        [1]  after UPDATE — returns updated bot_configs row or None
    """

    def test_returns_bot_config_on_success(self):
        hist    = make_history_row(config_version=2)
        updated = make_row(config_version=6)
        tx, _   = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            config = repo.rollback("alex", "btc_test", to_version=2)
        assert isinstance(config, BotConfig)
        assert config.config_version == 6

    def test_raises_history_not_found_when_no_entry(self):
        tx, _ = _multi_fetchone([None, None])
        repo  = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            with pytest.raises(ConfigHistoryNotFoundError, match="config_version=99"):
                repo.rollback("alex", "btc_test", to_version=99)

    def test_raises_bot_config_not_found_when_update_no_rows(self):
        hist = make_history_row(config_version=2)
        tx, _ = _multi_fetchone([hist, None])
        repo  = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            with pytest.raises(BotConfigNotFoundError):
                repo.rollback("alex", "btc_test", to_version=2)

    def test_set_local_is_first_execute_call(self):
        hist    = make_history_row(config_version=3)
        updated = make_row(config_version=7)
        tx, cur = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.rollback("alex", "btc_test", to_version=3)
        first_sql = cur.execute.call_args_list[0][0][0]
        assert "SET LOCAL" in first_sql
        assert "app.changed_by" in first_sql

    def test_guc_encodes_to_version_and_changed_by(self):
        hist    = make_history_row(config_version=3)
        updated = make_row(config_version=7)
        tx, cur = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.rollback("alex", "btc_test", to_version=3, changed_by="wfo_script")
        guc_value = cur.execute.call_args_list[0][0][1][0]
        assert "3" in guc_value
        assert "wfo_script" in guc_value

    def test_guc_default_changed_by_is_operator(self):
        hist    = make_history_row(config_version=2)
        updated = make_row(config_version=5)
        tx, cur = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.rollback("alex", "btc_test", to_version=2)
        guc_value = cur.execute.call_args_list[0][0][1][0]
        assert "operator" in guc_value

    def test_history_params_passed_to_update(self):
        target_params = {"ma_period": 99, "tp_pct": 0.07}
        hist    = make_history_row(config_version=2, strategy_params=target_params)
        updated = make_row(config_version=6)
        tx, cur = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with (patch("bot_config.repository.transaction", tx),
              patch("bot_config.repository.psycopg2.extras.Json") as mock_json):
            repo.rollback("alex", "btc_test", to_version=2)
        # Json must be called exactly once with the strategy_params from history
        mock_json.assert_called_once_with(target_params)

    def test_three_execute_calls_total(self):
        hist    = make_history_row(config_version=2)
        updated = make_row(config_version=5)
        tx, cur = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.rollback("alex", "btc_test", to_version=2)
        assert cur.execute.call_count == 3

    def test_update_includes_returning_clause(self):
        hist    = make_history_row(config_version=2)
        updated = make_row(config_version=5)
        tx, cur = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.rollback("alex", "btc_test", to_version=2)
        update_sql = cur.execute.call_args_list[2][0][0]
        assert "RETURNING" in update_sql.upper()

    def test_history_not_found_does_not_execute_update(self):
        """No UPDATE must run if history entry is missing."""
        tx, cur = _multi_fetchone([None])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            with pytest.raises(ConfigHistoryNotFoundError):
                repo.rollback("alex", "btc_test", to_version=77)
        # Only SET LOCAL + SELECT were executed; no UPDATE
        assert cur.execute.call_count == 2

    def test_error_message_contains_version(self):
        tx, _ = _multi_fetchone([None, None])
        repo  = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            with pytest.raises(ConfigHistoryNotFoundError) as exc_info:
                repo.rollback("alex", "btc_test", to_version=42)
        assert "42" in str(exc_info.value)

    def test_returned_config_has_correct_bot_id(self):
        hist    = make_history_row(config_version=1)
        updated = make_row(bot_id="btc_test", config_version=4)
        tx, _   = _multi_fetchone([hist, updated])
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            config = repo.rollback("alex", "btc_test", to_version=1)
        assert config.bot_id == "btc_test"


# ---------------------------------------------------------------------------
# set_status — GUC tagging (regression guard)
# ---------------------------------------------------------------------------

class TestSetStatusGUC:
    """
    set_status() was updated to tag the transaction with SET LOCAL
    app.changed_by = 'bot'. Verify the tagging is present.
    """

    def _make_rowcount_cur(self, rowcount=1):
        cur = MagicMock()
        cur.rowcount = rowcount

        @contextmanager
        def _tx():
            yield cur

        return _tx, cur

    def test_set_local_before_update(self):
        tx, cur = self._make_rowcount_cur(rowcount=1)
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.set_status("alex", "btc_test", BotStatus.STOPPED)
        first_sql = cur.execute.call_args_list[0][0][0]
        assert "SET LOCAL" in first_sql
        assert "app.changed_by" in first_sql

    def test_set_local_value_is_bot(self):
        tx, cur = self._make_rowcount_cur(rowcount=1)
        repo = _make_repo()
        with patch("bot_config.repository.transaction", tx):
            repo.set_status("alex", "btc_test", BotStatus.STOPPED)
        # SET LOCAL is a no-param execute call
        first_sql = cur.execute.call_args_list[0][0][0]
        assert "'bot'" in first_sql
