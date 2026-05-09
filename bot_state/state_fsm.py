from __future__ import annotations

from typing import FrozenSet

from bot_state.models import CycleStatus


class InvalidTransitionError(Exception):
    """Raised when FSM transition is not allowed."""

    def __init__(self, from_status: CycleStatus, to_status: CycleStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Invalid FSM transition: {from_status.value} -> {to_status.value}"
        )


# Transition table: from_status -> set of allowed to_status values.
# STOP_CRANE is reachable from any state — handled separately in transition().
_TRANSITIONS: dict[CycleStatus, FrozenSet[CycleStatus]] = {
    CycleStatus.IDLE: frozenset({
        CycleStatus.ENTERING,
    }),
    CycleStatus.ENTERING: frozenset({
        CycleStatus.IN_POSITION,       # order filled >= threshold
        CycleStatus.IDLE,              # order cancelled or timeout
        CycleStatus.WAITING_FOR_LIQUIDITY,
    }),
    CycleStatus.IN_POSITION: frozenset({
        CycleStatus.IN_POSITION,       # DCA filled, avg_price updated
        CycleStatus.CLOSING,           # TP filled or close command
        CycleStatus.WAITING_FOR_LIQUIDITY,
    }),
    CycleStatus.CLOSING: frozenset({
        CycleStatus.IDLE,              # cycle finalized via Close Protocol
    }),
    CycleStatus.WAITING_FOR_LIQUIDITY: frozenset({
        CycleStatus.IN_POSITION,       # funds available, order placed
        CycleStatus.CLOSING,           # FORCE_CLOSE while waiting
    }),
    CycleStatus.STOP_CRANE: frozenset({
        # No transitions out of STOP_CRANE — manual operator resolve only.
        # Resume is done by operator setting cycle_status directly in DB.
    }),
}

# States from which CLOSE_ONLY/FORCE_CLOSE can redirect to CLOSING
_CLOSE_ONLY_SOURCES: FrozenSet[CycleStatus] = frozenset({
    CycleStatus.IDLE,
    CycleStatus.ENTERING,
    CycleStatus.IN_POSITION,
    CycleStatus.WAITING_FOR_LIQUIDITY,
})


class StateFSM:
    """
    Finite state machine for bot cycle status.

    Rules:
    - STOP_CRANE is reachable from ANY state (critical anomaly).
    - STOP_CRANE has no outgoing transitions (manual resolve only).
    - All other transitions are defined in _TRANSITIONS.
    - Self-transition IN_POSITION -> IN_POSITION is allowed (DCA fill).
    """

    def transition(
        self,
        from_status: CycleStatus,
        to_status: CycleStatus,
    ) -> CycleStatus:
        """
        Validate and return the target status.
        Raises InvalidTransitionError if transition is not allowed.
        """
        # STOP_CRANE is always reachable
        if to_status == CycleStatus.STOP_CRANE:
            return CycleStatus.STOP_CRANE

        allowed = _TRANSITIONS.get(from_status, frozenset())
        if to_status not in allowed:
            raise InvalidTransitionError(from_status, to_status)

        return to_status

    def can_transition(
        self,
        from_status: CycleStatus,
        to_status: CycleStatus,
    ) -> bool:
        """Return True if transition is valid without raising."""
        if to_status == CycleStatus.STOP_CRANE:
            return True
        allowed = _TRANSITIONS.get(from_status, frozenset())
        return to_status in allowed

    def allowed_transitions(self, from_status: CycleStatus) -> FrozenSet[CycleStatus]:
        """Return all valid target statuses from the given state."""
        base = _TRANSITIONS.get(from_status, frozenset())
        # STOP_CRANE always available unless already there
        if from_status != CycleStatus.STOP_CRANE:
            return base | frozenset({CycleStatus.STOP_CRANE})
        return base
