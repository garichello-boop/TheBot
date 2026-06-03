"""
tests/bot_state/test_history.py

Tests for:
    StateHistoryRow              — model parsing (models.py)
    StateRepository.get_history() — FSM transition audit trail read
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from bot_state.models import (
    ClosingReason,
    CycleStatus,
    StateHistoryRow,
)
from bot_state.state_repo import StateRepository
from .helpers import make_state_row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_history_row(**overrides) -> dict:
    """
    Build a valid bot_state_history row dict.
    Mirrors the columns written by the _bot_state_fsm_audit trigger.
    """
    row = {
        "id":                     1,
        "user_id":                "igor",
        "bot_id":                 "btc_paper_01",
        "old_cycle_status":       "IDLE",
        "new_cycle_status":       "ENTERING",
        "version":                5,
        "cycle_id":               "cycle-abc",
        "virtual_balance_free":   Decimal("800.00"),
        "virtual_balance_locked": Decimal("200.00"),
        "position_qty":           Decimal("0.003"),
        "position_avg_price":     Decimal("30000.00"),
        "dca_count":              0,
        "quote_spent":            Decimal("200.00"),
        "quote_received":         Decimal("0.00"),
        "last_applied_trade_id":  "trade-001",
        "active_entry_order_id":  "order-001",
        "active_tp_order_id":     None,
        "closing_reason":         None,
        "trigger_op":             "UPDATE",
        "recorded_at":            datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


def _make_repo():
    return StateRepository(db_pool=MagicMock())


def _fetchall(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows

    @contextmanager
    def _tx():
        yield cur

    return _tx, cur


# ---------------------------------------------------------------------------
# StateHistoryRow — model parsing
# ---------------------------------------------------------------------------

class TestStateHistoryRow:
    def test_from_row_builds_dataclass(self):
        h = StateHistoryRow.from_row(make_history_row())
        assert h.id == 1
        assert h.user_id == "igor"
        assert h.bot_id == "btc_paper_01"
        assert h.old_cycle_status == CycleStatus.IDLE
        assert h.new_cycle_status == CycleStatus.ENTERING
        assert h.version == 5
        assert h.cycle_id == "cycle-abc"
        assert h.dca_count == 0
        assert h.trigger_op == "UPDATE"

    def test_from_row_parses_cycle_status_enums(self):
        h = StateHistoryRow.from_row(make_history_row(
            old_cycle_status="IN_POSITION",
            new_cycle_status="CLOSING",
        ))
        assert h.old_cycle_status == CycleStatus.IN_POSITION
        assert h.new_cycle_status == CycleStatus.CLOSING

    def test_from_row_null_old_cycle_status_on_insert(self):
        """Initial INSERT has no previous status."""
        h = StateHistoryRow.from_row(make_history_row(
            old_cycle_status=None,
            trigger_op="INSERT",
        ))
        assert h.old_cycle_status is None
        assert h.trigger_op == "INSERT"

    def test_from_row_converts_decimals(self):
        h = StateHistoryRow.from_row(make_history_row(
            virtual_balance_free="900.50",
            virtual_balance_locked="100.50",
        ))
        assert isinstance(h.virtual_balance_free, Decimal)
        assert isinstance(h.virtual_balance_locked, Decimal)
        assert h.virtual_balance_free == Decimal("900.50")

    def test_from_row_keeps_decimal_unchanged(self):
        h = StateHistoryRow.from_row(make_history_row(
            virtual_balance_free=Decimal("777.00"),
        ))
        assert h.virtual_balance_free == Decimal("777.00")

    def test_from_row_null_position_avg_price(self):
        h = StateHistoryRow.from_row(make_history_row(position_avg_price=None))
        assert h.position_avg_price is None

    def test_from_row_parses_closing_reason(self):
        h = StateHistoryRow.from_row(make_history_row(
            closing_reason="SL",
            new_cycle_status="CLOSING",
        ))
        assert h.closing_reason == ClosingReason.SL

    def test_from_row_null_closing_reason(self):
        h = StateHistoryRow.from_row(make_history_row(closing_reason=None))
        assert h.closing_reason is None

    def test_from_row_null_dca_count_defaults_to_zero(self):
        h = StateHistoryRow.from_row(make_history_row(dca_count=None))
        assert h.dca_count == 0

    def test_from_row_null_optional_strings(self):
        h = StateHistoryRow.from_row(make_history_row(
            cycle_id=None,
            last_applied_trade_id=None,
            active_entry_order_id=None,
            active_tp_order_id=None,
        ))
        assert h.cycle_id is None
        assert h.last_applied_trade_id is None
        assert h.active_entry_order_id is None
        assert h.active_tp_order_id is None

    def test_frozen_raises_on_mutation(self):
        h = StateHistoryRow.from_row(make_history_row())
        with pytest.raises((AttributeError, TypeError)):
            h.new_cycle_status = CycleStatus.IDLE  # type: ignore[misc]

    def test_repr_contains_key_fields(self):
        h = StateHistoryRow.from_row(make_history_row())
        r = repr(h)
        assert "btc_paper_01" in r
        assert "v=5" in r
        assert "IDLE → ENTERING" in r


# ---------------------------------------------------------------------------
# StateHistoryRow.transition_label
# ---------------------------------------------------------------------------

class TestTransitionLabel:
    def test_update_transition(self):
        h = StateHistoryRow.from_row(make_history_row(
            old_cycle_status="IDLE",
            new_cycle_status="ENTERING",
        ))
        assert h.transition_label == "IDLE → ENTERING"

    def test_insert_shows_dash_for_no_old_status(self):
        h = StateHistoryRow.from_row(make_history_row(
            old_cycle_status=None,
            new_cycle_status="IDLE",
            trigger_op="INSERT",
        ))
        assert h.transition_label == "— → IDLE"

    def test_closing_transition(self):
        h = StateHistoryRow.from_row(make_history_row(
            old_cycle_status="IN_POSITION",
            new_cycle_status="CLOSING",
        ))
        assert h.transition_label == "IN_POSITION → CLOSING"

    def test_closing_to_idle(self):
        h = StateHistoryRow.from_row(make_history_row(
            old_cycle_status="CLOSING",
            new_cycle_status="IDLE",
        ))
        assert h.transition_label == "CLOSING → IDLE"


# ---------------------------------------------------------------------------
# StateRepository.get_history
# ---------------------------------------------------------------------------

class TestGetHistory:
    def test_returns_list_of_history_rows(self):
        rows = [
            make_history_row(id=3, version=10,
                             old_cycle_status="IN_POSITION",
                             new_cycle_status="CLOSING"),
            make_history_row(id=2, version=5,
                             old_cycle_status="ENTERING",
                             new_cycle_status="IN_POSITION"),
            make_history_row(id=1, version=2,
                             old_cycle_status="IDLE",
                             new_cycle_status="ENTERING"),
        ]
        tx, _ = _fetchall(rows)
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            history = repo.get_history("igor", "btc_paper_01")
        assert len(history) == 3
        assert all(isinstance(h, StateHistoryRow) for h in history)

    def test_empty_list_when_no_history(self):
        tx, _ = _fetchall([])
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            assert repo.get_history("igor", "btc_paper_01") == []

    def test_none_fetchall_treated_as_empty(self):
        tx, _ = _fetchall(None)
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            assert repo.get_history("igor", "btc_paper_01") == []

    def test_newest_first_ordering_preserved(self):
        """Order is determined by SQL (ORDER BY id DESC); we just preserve it."""
        rows = [
            make_history_row(id=5, new_cycle_status="CLOSING"),
            make_history_row(id=3, new_cycle_status="IN_POSITION"),
            make_history_row(id=1, new_cycle_status="ENTERING"),
        ]
        tx, _ = _fetchall(rows)
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            history = repo.get_history("igor", "btc_paper_01")
        assert history[0].new_cycle_status == CycleStatus.CLOSING
        assert history[1].new_cycle_status == CycleStatus.IN_POSITION
        assert history[2].new_cycle_status == CycleStatus.ENTERING

    def test_limit_passed_to_query(self):
        tx, cur = _fetchall([])
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            repo.get_history("igor", "btc_paper_01", limit=10)
        params = cur.execute.call_args[0][1]
        assert 10 in params

    def test_default_limit_is_50(self):
        tx, cur = _fetchall([])
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            repo.get_history("igor", "btc_paper_01")
        params = cur.execute.call_args[0][1]
        assert 50 in params

    def test_user_id_and_bot_id_passed_to_query(self):
        tx, cur = _fetchall([])
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            repo.get_history("igor", "btc_paper_01")
        params = cur.execute.call_args[0][1]
        assert "igor" in params
        assert "btc_paper_01" in params

    def test_insert_row_has_null_old_status(self):
        rows = [make_history_row(old_cycle_status=None, trigger_op="INSERT",
                                 new_cycle_status="IDLE")]
        tx, _ = _fetchall(rows)
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            history = repo.get_history("igor", "btc_paper_01")
        assert history[0].old_cycle_status is None
        assert history[0].trigger_op == "INSERT"

    def test_closing_reason_preserved(self):
        rows = [make_history_row(
            old_cycle_status="IN_POSITION",
            new_cycle_status="CLOSING",
            closing_reason="TP",
        )]
        tx, _ = _fetchall(rows)
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            history = repo.get_history("igor", "btc_paper_01")
        assert history[0].closing_reason == ClosingReason.TP

    def test_full_cycle_sequence_parseable(self):
        """All six FSM states parse correctly from history rows."""
        transitions = [
            ("CLOSING",     "IDLE"),
            ("IN_POSITION", "CLOSING"),
            ("ENTERING",    "IN_POSITION"),
            ("IDLE",        "ENTERING"),
        ]
        rows = [
            make_history_row(id=i + 1,
                             old_cycle_status=old,
                             new_cycle_status=new)
            for i, (old, new) in enumerate(transitions)
        ]
        tx, _ = _fetchall(rows)
        repo = _make_repo()
        with patch("bot_state.state_repo.transaction", tx):
            history = repo.get_history("igor", "btc_paper_01")
        labels = [h.transition_label for h in history]
        assert labels == [
            "CLOSING → IDLE",
            "IN_POSITION → CLOSING",
            "ENTERING → IN_POSITION",
            "IDLE → ENTERING",
        ]
