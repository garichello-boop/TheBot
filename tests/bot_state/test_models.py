"""
Unit tests for BotState and BotRegistry models.
No DB required.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from bot_state.models import BotState, BotRegistry, CycleStatus, OperationalStatus
from tests.bot_state.helpers import make_state_row, make_registry_row


# ------------------------------------------------------------------
# BotState.initial()
# ------------------------------------------------------------------

class TestBotStateInitial:
    def test_initial_values(self):
        state = BotState.initial("user1", "bot1", Decimal("500.00"))
        assert state.user_id == "user1"
        assert state.bot_id == "bot1"
        assert state.version == 0
        assert state.cycle_status == CycleStatus.IDLE
        assert state.virtual_balance_free == Decimal("500.00")
        assert state.virtual_balance_locked == Decimal("0")
        assert state.position_qty == Decimal("0")
        assert state.quote_spent == Decimal("0")
        assert state.quote_received == Decimal("0")
        assert state.active_dca_order_ids == ()
        assert state.cycle_id is None

    def test_is_idle(self):
        state = BotState.initial("u", "b", Decimal("100"))
        assert state.is_idle is True
        assert state.has_position is False

    def test_virtual_balance_total(self):
        state = BotState.initial("u", "b", Decimal("300"))
        assert state.virtual_balance_total == Decimal("300")


# ------------------------------------------------------------------
# BotState invariants (__post_init__)
# ------------------------------------------------------------------

class TestBotStateInvariants:
    def test_negative_version_raises(self):
        with pytest.raises(ValueError, match="version"):
            BotState.initial("u", "b", Decimal("100")).with_updates(version=-1)

    def test_negative_balance_free_raises(self):
        with pytest.raises(ValueError, match="virtual_balance_free"):
            BotState.initial("u", "b", Decimal("100")).with_updates(
                virtual_balance_free=Decimal("-1")
            )

    def test_negative_balance_locked_raises(self):
        with pytest.raises(ValueError, match="virtual_balance_locked"):
            BotState.initial("u", "b", Decimal("100")).with_updates(
                virtual_balance_locked=Decimal("-0.01")
            )

    def test_negative_position_qty_raises(self):
        with pytest.raises(ValueError, match="position_qty"):
            BotState.initial("u", "b", Decimal("100")).with_updates(
                position_qty=Decimal("-1")
            )

    def test_zero_values_are_valid(self):
        state = BotState.initial("u", "b", Decimal("0"))
        assert state.virtual_balance_free == Decimal("0")


# ------------------------------------------------------------------
# BotState.with_updates()
# ------------------------------------------------------------------

class TestBotStateWithUpdates:
    def test_version_auto_incremented(self):
        state = BotState.initial("u", "b", Decimal("100"))
        assert state.version == 0
        new = state.with_updates(cycle_status=CycleStatus.ENTERING)
        assert new.version == 1

    def test_explicit_version_respected(self):
        state = BotState.initial("u", "b", Decimal("100"))
        new = state.with_updates(version=5)
        assert new.version == 5

    def test_unchanged_fields_preserved(self):
        state = BotState.initial("u", "b", Decimal("777"))
        new = state.with_updates(cycle_status=CycleStatus.ENTERING)
        assert new.virtual_balance_free == Decimal("777")
        assert new.user_id == "u"
        assert new.bot_id == "b"

    def test_list_converted_to_tuple(self):
        state = BotState.initial("u", "b", Decimal("100"))
        new = state.with_updates(active_dca_order_ids=["id1", "id2"])
        assert isinstance(new.active_dca_order_ids, tuple)
        assert new.active_dca_order_ids == ("id1", "id2")

    def test_original_state_unchanged(self):
        """frozen=True — original state must not mutate."""
        state = BotState.initial("u", "b", Decimal("100"))
        _ = state.with_updates(cycle_status=CycleStatus.ENTERING)
        assert state.cycle_status == CycleStatus.IDLE
        assert state.version == 0


# ------------------------------------------------------------------
# BotState.from_row()
# ------------------------------------------------------------------

class TestBotStateFromRow:
    def test_basic_row(self):
        row = make_state_row()
        state = BotState.from_row(row)
        assert state.user_id == "test_user"
        assert state.bot_id == "test_bot"
        assert state.cycle_status == CycleStatus.IDLE
        assert state.virtual_balance_free == Decimal("1000.00")
        assert state.active_dca_order_ids == ()

    def test_dca_order_ids_converted_to_tuple(self):
        row = make_state_row(active_dca_order_ids=["order1", "order2"])
        state = BotState.from_row(row)
        assert state.active_dca_order_ids == ("order1", "order2")

    def test_none_dca_order_ids_becomes_empty_tuple(self):
        row = make_state_row(active_dca_order_ids=None)
        state = BotState.from_row(row)
        assert state.active_dca_order_ids == ()

    def test_in_position_status(self):
        row = make_state_row(
            cycle_status="IN_POSITION",
            cycle_id="cycle_001",
            position_qty=Decimal("10.5"),
            position_avg_price=Decimal("3200.00"),
            dca_count=1,
        )
        state = BotState.from_row(row)
        assert state.cycle_status == CycleStatus.IN_POSITION
        assert state.cycle_id == "cycle_001"
        assert state.position_qty == Decimal("10.5")
        assert state.position_avg_price == Decimal("3200.00")
        assert state.has_position is True

    def test_all_cycle_statuses_parse(self):
        for status in CycleStatus:
            row = make_state_row(cycle_status=status.value)
            state = BotState.from_row(row)
            assert state.cycle_status == status

    def test_unknown_cycle_status_raises(self):
        row = make_state_row(cycle_status="INVALID_STATUS")
        with pytest.raises(ValueError):
            BotState.from_row(row)


# ------------------------------------------------------------------
# BotRegistry.from_row()
# ------------------------------------------------------------------

class TestBotRegistryFromRow:
    def test_basic_row(self):
        row = make_registry_row()
        reg = BotRegistry.from_row(row)
        assert reg.user_id == "test_user"
        assert reg.bot_id == "test_bot"
        assert reg.operational_status == OperationalStatus.STOPPED
        assert reg.pid is None
        assert reg.last_heartbeat is None

    def test_running_status(self):
        now = datetime.now(timezone.utc)
        row = make_registry_row(
            operational_status="RUNNING",
            pid=12345,
            last_heartbeat=now,
        )
        reg = BotRegistry.from_row(row)
        assert reg.operational_status == OperationalStatus.RUNNING
        assert reg.pid == 12345
        assert reg.last_heartbeat == now

    def test_all_statuses_parse(self):
        for status in OperationalStatus:
            row = make_registry_row(operational_status=status.value)
            reg = BotRegistry.from_row(row)
            assert reg.operational_status == status

    def test_unknown_status_raises(self):
        row = make_registry_row(operational_status="ZOMBIE")
        with pytest.raises(ValueError):
            BotRegistry.from_row(row)
