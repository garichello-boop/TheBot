"""
Unit tests for StateFSM.
No DB required.
"""
import pytest

from bot_state.models import CycleStatus
from bot_state.state_fsm import StateFSM, InvalidTransitionError


@pytest.fixture
def fsm() -> StateFSM:
    return StateFSM()


# ------------------------------------------------------------------
# Valid transitions
# ------------------------------------------------------------------

class TestValidTransitions:
    def test_idle_to_entering(self, fsm):
        result = fsm.transition(CycleStatus.IDLE, CycleStatus.ENTERING)
        assert result == CycleStatus.ENTERING

    def test_entering_to_in_position(self, fsm):
        result = fsm.transition(CycleStatus.ENTERING, CycleStatus.IN_POSITION)
        assert result == CycleStatus.IN_POSITION

    def test_entering_to_idle(self, fsm):
        result = fsm.transition(CycleStatus.ENTERING, CycleStatus.IDLE)
        assert result == CycleStatus.IDLE

    def test_entering_to_waiting_for_liquidity(self, fsm):
        result = fsm.transition(CycleStatus.ENTERING, CycleStatus.WAITING_FOR_LIQUIDITY)
        assert result == CycleStatus.WAITING_FOR_LIQUIDITY

    def test_in_position_self_transition_dca(self, fsm):
        """DCA fill: IN_POSITION -> IN_POSITION is allowed."""
        result = fsm.transition(CycleStatus.IN_POSITION, CycleStatus.IN_POSITION)
        assert result == CycleStatus.IN_POSITION

    def test_in_position_to_closing(self, fsm):
        result = fsm.transition(CycleStatus.IN_POSITION, CycleStatus.CLOSING)
        assert result == CycleStatus.CLOSING

    def test_in_position_to_waiting_for_liquidity(self, fsm):
        result = fsm.transition(CycleStatus.IN_POSITION, CycleStatus.WAITING_FOR_LIQUIDITY)
        assert result == CycleStatus.WAITING_FOR_LIQUIDITY

    def test_closing_to_idle(self, fsm):
        result = fsm.transition(CycleStatus.CLOSING, CycleStatus.IDLE)
        assert result == CycleStatus.IDLE

    def test_waiting_for_liquidity_to_in_position(self, fsm):
        result = fsm.transition(CycleStatus.WAITING_FOR_LIQUIDITY, CycleStatus.IN_POSITION)
        assert result == CycleStatus.IN_POSITION

    def test_waiting_for_liquidity_to_closing(self, fsm):
        result = fsm.transition(CycleStatus.WAITING_FOR_LIQUIDITY, CycleStatus.CLOSING)
        assert result == CycleStatus.CLOSING


# ------------------------------------------------------------------
# STOP_CRANE reachable from any state
# ------------------------------------------------------------------

class TestStopCraneFromAnyState:
    @pytest.mark.parametrize("from_status", list(CycleStatus))
    def test_stop_crane_always_reachable(self, fsm, from_status):
        result = fsm.transition(from_status, CycleStatus.STOP_CRANE)
        assert result == CycleStatus.STOP_CRANE


# ------------------------------------------------------------------
# STOP_CRANE has no outgoing transitions
# ------------------------------------------------------------------

class TestStopCraneNoExit:
    @pytest.mark.parametrize("to_status", [
        CycleStatus.IDLE,
        CycleStatus.ENTERING,
        CycleStatus.IN_POSITION,
        CycleStatus.CLOSING,
        CycleStatus.WAITING_FOR_LIQUIDITY,
    ])
    def test_no_exit_from_stop_crane(self, fsm, to_status):
        with pytest.raises(InvalidTransitionError):
            fsm.transition(CycleStatus.STOP_CRANE, to_status)


# ------------------------------------------------------------------
# Invalid transitions
# ------------------------------------------------------------------

class TestInvalidTransitions:
    @pytest.mark.parametrize("from_status, to_status", [
        (CycleStatus.IDLE, CycleStatus.IN_POSITION),
        (CycleStatus.IDLE, CycleStatus.CLOSING),
        (CycleStatus.IDLE, CycleStatus.WAITING_FOR_LIQUIDITY),
        (CycleStatus.IDLE, CycleStatus.IDLE),           # no self-transition in IDLE
        (CycleStatus.ENTERING, CycleStatus.CLOSING),
        (CycleStatus.IN_POSITION, CycleStatus.IDLE),
        (CycleStatus.IN_POSITION, CycleStatus.ENTERING),
        (CycleStatus.CLOSING, CycleStatus.ENTERING),
        (CycleStatus.CLOSING, CycleStatus.IN_POSITION),
        (CycleStatus.CLOSING, CycleStatus.CLOSING),
        (CycleStatus.CLOSING, CycleStatus.WAITING_FOR_LIQUIDITY),
        (CycleStatus.WAITING_FOR_LIQUIDITY, CycleStatus.IDLE),
        (CycleStatus.WAITING_FOR_LIQUIDITY, CycleStatus.ENTERING),
    ])
    def test_invalid_transition_raises(self, fsm, from_status, to_status):
        with pytest.raises(InvalidTransitionError) as exc_info:
            fsm.transition(from_status, to_status)
        assert from_status == exc_info.value.from_status
        assert to_status == exc_info.value.to_status


# ------------------------------------------------------------------
# can_transition()
# ------------------------------------------------------------------

class TestCanTransition:
    def test_valid_returns_true(self, fsm):
        assert fsm.can_transition(CycleStatus.IDLE, CycleStatus.ENTERING) is True

    def test_invalid_returns_false(self, fsm):
        assert fsm.can_transition(CycleStatus.IDLE, CycleStatus.CLOSING) is False

    def test_stop_crane_always_true(self, fsm):
        assert fsm.can_transition(CycleStatus.IDLE, CycleStatus.STOP_CRANE) is True
        assert fsm.can_transition(CycleStatus.CLOSING, CycleStatus.STOP_CRANE) is True


# ------------------------------------------------------------------
# allowed_transitions()
# ------------------------------------------------------------------

class TestAllowedTransitions:
    def test_idle_allowed(self, fsm):
        allowed = fsm.allowed_transitions(CycleStatus.IDLE)
        assert CycleStatus.ENTERING in allowed
        assert CycleStatus.STOP_CRANE in allowed
        assert CycleStatus.CLOSING not in allowed

    def test_stop_crane_has_no_allowed(self, fsm):
        allowed = fsm.allowed_transitions(CycleStatus.STOP_CRANE)
        assert len(allowed) == 0

    def test_stop_crane_not_in_its_own_allowed(self, fsm):
        """STOP_CRANE -> STOP_CRANE: not in allowed_transitions for STOP_CRANE."""
        allowed = fsm.allowed_transitions(CycleStatus.STOP_CRANE)
        assert CycleStatus.STOP_CRANE not in allowed
