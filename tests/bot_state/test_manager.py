"""
Tests for StateManager.
Mix of unit (mock emitter) and integration (real DB) tests.
clean_bot_state fixture wipes rows before each test.
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, call

from bot_state.models import BotState, CycleStatus
from bot_state.state_fsm import InvalidTransitionError
from bot_state.state_manager import StateManager


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

@pytest.fixture
def mock_emitter():
    return MagicMock()


@pytest.fixture
def manager_with_emitter(state_repo, mock_emitter):
    return StateManager(repo=state_repo, emitter=mock_emitter)


@pytest.fixture
def initialized_state(state_repo):
    """Fresh bot_state row in DB, returns BotState."""
    return state_repo.initialize(Decimal("1000.00"))


# ------------------------------------------------------------------
# initialize()
# ------------------------------------------------------------------

class TestStateManagerInitialize:
    def test_creates_idle_state(self, state_manager, user_id, bot_id):
        state = state_manager.initialize(Decimal("500.00"))
        assert state.cycle_status == CycleStatus.IDLE
        assert state.virtual_balance_free == Decimal("500.00")
        assert state.version == 0

    def test_persisted_to_db(self, state_manager, state_repo):
        state_manager.initialize(Decimal("200.00"))
        loaded = state_repo.load()
        assert loaded is not None
        assert loaded.virtual_balance_free == Decimal("200.00")

    def test_emits_state_loaded(self, manager_with_emitter, mock_emitter):
        manager_with_emitter.initialize(Decimal("100.00"))
        mock_emitter.emit.assert_called_once()
        call_kwargs = mock_emitter.emit.call_args.kwargs
        assert call_kwargs["event_type"] == "STATE_LOADED"
        assert call_kwargs["level"] == "INFO"

    def test_no_emitter_no_error(self, state_manager):
        """emitter=None should not raise."""
        state = state_manager.initialize(Decimal("100.00"))
        assert state is not None


# ------------------------------------------------------------------
# load()
# ------------------------------------------------------------------

class TestStateManagerLoad:
    def test_returns_none_when_missing(self, state_manager):
        result = state_manager.load()
        assert result is None

    def test_returns_state_after_initialize(self, state_manager):
        state_manager.initialize(Decimal("300.00"))
        loaded = state_manager.load()
        assert loaded is not None
        assert loaded.cycle_status == CycleStatus.IDLE

    def test_emits_state_loaded(self, manager_with_emitter, mock_emitter):
        manager_with_emitter.initialize(Decimal("100.00"))
        mock_emitter.reset_mock()
        manager_with_emitter.load()
        mock_emitter.emit.assert_called_once()
        assert mock_emitter.emit.call_args.kwargs["event_type"] == "STATE_LOADED"

    def test_no_emit_when_none(self, manager_with_emitter, mock_emitter):
        """load() on missing row: no emit."""
        manager_with_emitter.load()
        mock_emitter.emit.assert_not_called()


# ------------------------------------------------------------------
# transition()
# ------------------------------------------------------------------

class TestStateManagerTransition:
    def test_valid_transition_persisted(self, state_manager, initialized_state):
        new_state = state_manager.transition(
            initialized_state,
            CycleStatus.ENTERING,
            pending_client_order_id="uuid-001",
        )
        assert new_state.cycle_status == CycleStatus.ENTERING
        assert new_state.pending_client_order_id == "uuid-001"
        assert new_state.version == 1

        loaded = state_manager.load()
        assert loaded.cycle_status == CycleStatus.ENTERING

    def test_version_incremented_in_db(self, state_manager, initialized_state):
        state_manager.transition(initialized_state, CycleStatus.ENTERING)
        loaded = state_manager.load()
        assert loaded.version == 1

    def test_invalid_transition_raises_no_db_write(self, state_manager, initialized_state):
        with pytest.raises(InvalidTransitionError):
            state_manager.transition(initialized_state, CycleStatus.CLOSING)

        # DB must be unchanged
        loaded = state_manager.load()
        assert loaded.cycle_status == CycleStatus.IDLE
        assert loaded.version == 0

    def test_stop_crane_from_any_state(self, state_manager, initialized_state):
        new_state = state_manager.transition(initialized_state, CycleStatus.STOP_CRANE)
        assert new_state.cycle_status == CycleStatus.STOP_CRANE

        loaded = state_manager.load()
        assert loaded.cycle_status == CycleStatus.STOP_CRANE

    def test_emits_cycle_status_changed(self, manager_with_emitter, mock_emitter, state_repo):
        state = state_repo.initialize(Decimal("1000.00"))
        mock_emitter.reset_mock()
        manager_with_emitter.transition(state, CycleStatus.ENTERING)

        mock_emitter.emit.assert_called_once()
        payload = mock_emitter.emit.call_args.kwargs
        assert payload["event_type"] == "CYCLE_STATUS_CHANGED"
        assert payload["payload"]["from_status"] == "IDLE"
        assert payload["payload"]["to_status"] == "ENTERING"

    def test_emitter_failure_does_not_stop_transition(self, state_repo):
        """If emitter raises, DB write already happened — state is safe."""
        broken_emitter = MagicMock()
        broken_emitter.emit.side_effect = RuntimeError("Telegram is down")
        manager = StateManager(repo=state_repo, emitter=broken_emitter)

        state = state_repo.initialize(Decimal("1000.00"))
        new_state = manager.transition(state, CycleStatus.ENTERING)

        assert new_state.cycle_status == CycleStatus.ENTERING
        loaded = state_repo.load()
        assert loaded.cycle_status == CycleStatus.ENTERING

    def test_in_position_self_transition(self, state_manager, state_repo):
        """DCA fill: IN_POSITION -> IN_POSITION with updated dca_count."""
        state = state_repo.initialize(Decimal("1000.00"))
        state = state_manager.transition(state, CycleStatus.ENTERING)
        state = state_manager.transition(
            state, CycleStatus.IN_POSITION,
            position_qty=Decimal("10"),
            position_avg_price=Decimal("3200"),
        )
        new_state = state_manager.transition(
            state, CycleStatus.IN_POSITION,
            dca_count=1,
            position_qty=Decimal("20"),
            position_avg_price=Decimal("3100"),
        )
        assert new_state.cycle_status == CycleStatus.IN_POSITION
        assert new_state.dca_count == 1
        assert new_state.position_qty == Decimal("20")


# ------------------------------------------------------------------
# update()
# ------------------------------------------------------------------

class TestStateManagerUpdate:
    def test_updates_field_without_fsm(self, state_manager, initialized_state):
        new_state = state_manager.update(
            initialized_state,
            pending_client_order_id="uuid-pending",
        )
        assert new_state.pending_client_order_id == "uuid-pending"
        assert new_state.cycle_status == CycleStatus.IDLE
        assert new_state.version == 1

    def test_update_persisted(self, state_manager, initialized_state):
        state_manager.update(initialized_state, cycle_id="cycle_001")
        loaded = state_manager.load()
        assert loaded.cycle_id == "cycle_001"

    def test_update_rejects_cycle_status(self, state_manager, initialized_state):
        with pytest.raises(ValueError, match="cycle_status"):
            state_manager.update(
                initialized_state,
                cycle_status=CycleStatus.ENTERING,
            )

    def test_update_version_incremented(self, state_manager, initialized_state):
        new_state = state_manager.update(initialized_state, cycle_id="c1")
        assert new_state.version == 1


# ------------------------------------------------------------------
# Commit before emit: ordering guarantee
# ------------------------------------------------------------------

class TestCommitBeforeEmit:
    def test_db_written_before_emit_called(self, state_repo, mock_emitter):
        """
        Verify commit happens before emit by checking DB inside emit callback.
        If emit is called before commit, DB would still show old state.
        """
        observed_db_state = {}

        def capture_db_on_emit(**kwargs):
            loaded = state_repo.load()
            observed_db_state["cycle_status"] = loaded.cycle_status if loaded else None

        mock_emitter.emit.side_effect = capture_db_on_emit
        manager = StateManager(repo=state_repo, emitter=mock_emitter)

        state = state_repo.initialize(Decimal("1000.00"))
        manager.transition(state, CycleStatus.ENTERING)

        assert observed_db_state["cycle_status"] == CycleStatus.ENTERING
