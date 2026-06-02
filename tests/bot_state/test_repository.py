"""
Integration tests for StateRepository and RegistryRepository.
Requires live PostgreSQL connection (thebot DB).
clean_bot_state fixture (conftest.py) wipes rows before each test.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from bot_state.models import BotState, BotRegistry, CycleStatus, OperationalStatus
from bot_state.state_repo import StateRepository, DuplicateBotError
from bot_state.registry_repo import RegistryRepository

pytestmark = pytest.mark.skip(
    reason="Integration test: requires live PostgreSQL. "
           "Remove skip and run against real DB to execute."
)


# ------------------------------------------------------------------
# StateRepository.initialize()
# ------------------------------------------------------------------

class TestStateRepositoryInitialize:
    def test_creates_row(self, state_repo, user_id, bot_id):
        state = state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        assert state.version == 0
        assert state.cycle_status == CycleStatus.IDLE
        assert state.virtual_balance_free == Decimal("1000.00")
        assert state.virtual_balance_locked == Decimal("0")
        assert state.position_qty == Decimal("0")
        assert state.active_dca_order_ids == ()

    def test_row_readable_after_init(self, state_repo, user_id, bot_id):
        state_repo.initialize(user_id, bot_id, Decimal("500.00"))
        loaded = state_repo.load(user_id, bot_id)
        assert loaded is not None
        assert loaded.virtual_balance_free == Decimal("500.00")
        assert loaded.cycle_status == CycleStatus.IDLE

    def test_duplicate_initialize_raises(self, state_repo, user_id, bot_id):
        state_repo.initialize(user_id, bot_id, Decimal("100.00"))
        with pytest.raises(Exception):  # psycopg2 IntegrityError (PK conflict)
            state_repo.initialize(user_id, bot_id, Decimal("200.00"))


# ------------------------------------------------------------------
# StateRepository.load()
# ------------------------------------------------------------------

class TestStateRepositoryLoad:
    def test_returns_none_when_missing(self, state_repo, user_id, bot_id):
        result = state_repo.load(user_id, bot_id)
        assert result is None

    def test_loads_initialized_state(self, state_repo, user_id, bot_id):
        state_repo.initialize(user_id, bot_id, Decimal("750.00"))
        loaded = state_repo.load(user_id, bot_id)
        assert loaded is not None
        assert loaded.user_id == "test_user"
        assert loaded.bot_id == "test_bot"
        assert loaded.version == 0

    def test_active_dca_order_ids_is_tuple(self, state_repo, user_id, bot_id):
        state_repo.initialize(user_id, bot_id, Decimal("100.00"))
        loaded = state_repo.load(user_id, bot_id)
        assert isinstance(loaded.active_dca_order_ids, tuple)
        assert loaded.active_dca_order_ids == ()


# ------------------------------------------------------------------
# StateRepository.save()
# ------------------------------------------------------------------

class TestStateRepositorySave:
    def test_save_updates_fields(self, state_repo, user_id, bot_id):
        state = state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        new_state = state.with_updates(
            cycle_status=CycleStatus.ENTERING,
            pending_client_order_id="uuid-123",
        )
        state_repo.save(new_state)

        loaded = state_repo.load(user_id, bot_id)
        assert loaded.cycle_status == CycleStatus.ENTERING
        assert loaded.version == 1
        assert loaded.pending_client_order_id == "uuid-123"

    def test_save_increments_version(self, state_repo, user_id, bot_id):
        state = state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        for expected_version in range(1, 4):
            state = state.with_updates(cycle_id=f"cycle_{expected_version}")
            state_repo.save(state)
            loaded = state_repo.load(user_id, bot_id)
            assert loaded.version == expected_version

    def test_save_preserves_dca_order_ids(self, state_repo, user_id, bot_id):
        state = state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        new_state = state.with_updates(
            active_dca_order_ids=["dca_order_1", "dca_order_2"]
        )
        state_repo.save(new_state)

        loaded = state_repo.load(user_id, bot_id)
        assert loaded.active_dca_order_ids == ("dca_order_1", "dca_order_2")

    def test_save_numeric_precision(self, state_repo, user_id, bot_id):
        state = state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        precise_qty = Decimal("0.00123456")
        new_state = state.with_updates(
            position_qty=precise_qty,
            position_avg_price=Decimal("3200.55"),
            quote_spent=Decimal("3.94706736"),
        )
        state_repo.save(new_state)

        loaded = state_repo.load(user_id, bot_id)
        assert loaded.position_qty == precise_qty
        assert loaded.position_avg_price == Decimal("3200.55")

    def test_version_conflict_raises(self, state_repo, user_id, bot_id):
        """Optimistic lock: save with wrong expected version raises RuntimeError."""
        state = state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        # Save once to bump DB version to 1
        state_repo.save(state.with_updates(cycle_id="cycle_1"))
        # Try to save again with original state (version=0, expects DB version -1)
        with pytest.raises(RuntimeError, match="version conflict"):
            state_repo.save(state.with_updates(cycle_id="cycle_conflict"))

    def test_save_clears_nullable_fields(self, state_repo, user_id, bot_id):
        state = state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        state = state.with_updates(
            cycle_id="cycle_001",
            pending_client_order_id="some-uuid",
            active_entry_order_id="entry-123",
        )
        state_repo.save(state)

        # Clear fields back to None
        cleared = state.with_updates(
            pending_client_order_id=None,
            active_entry_order_id=None,
        )
        state_repo.save(cleared)

        loaded = state_repo.load(user_id, bot_id)
        assert loaded.pending_client_order_id is None
        assert loaded.active_entry_order_id is None


# ------------------------------------------------------------------
# StateRepository.load(for_update=True)
# ------------------------------------------------------------------

class TestStateRepositoryForUpdate:
    def test_load_for_update_returns_state(self, state_repo, user_id, bot_id):
        state_repo.initialize(user_id, bot_id, Decimal("1000.00"))
        loaded = state_repo.load(user_id, bot_id, for_update=True)
        assert loaded is not None
        assert loaded.cycle_status == CycleStatus.IDLE

    def test_load_for_update_missing_returns_none(self, state_repo, user_id, bot_id):
        result = state_repo.load(user_id, bot_id, for_update=True)
        assert result is None


# ------------------------------------------------------------------
# RegistryRepository.upsert()
# ------------------------------------------------------------------

class TestRegistryRepositoryUpsert:
    def test_insert_on_first_call(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.STARTING)
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded is not None
        assert loaded.operational_status == OperationalStatus.STARTING

    def test_update_on_second_call(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.STARTING)
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.RUNNING)
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded.operational_status == OperationalStatus.RUNNING

    def test_started_at_preserved_on_update(self, registry_repo, user_id, bot_id):
        started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.STARTING, started_at=started)
        # Update status without started_at — should keep original value
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.RUNNING)
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded.started_at is not None

    def test_pid_saved(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.RUNNING, pid=99999)
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded.pid == 99999

    def test_error_message_saved(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(
            status=OperationalStatus.ERROR,
            error_message="Something went wrong",
        )
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded.operational_status == OperationalStatus.ERROR
        assert loaded.error_message == "Something went wrong"


# ------------------------------------------------------------------
# RegistryRepository.load()
# ------------------------------------------------------------------

class TestRegistryRepositoryLoad:
    def test_returns_none_when_missing(self, registry_repo, user_id, bot_id):
        result = registry_repo.load(user_id, bot_id)
        assert result is None

    def test_returns_registry_after_upsert(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.STOPPED)
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded is not None
        assert loaded.user_id == "test_user"
        assert loaded.bot_id == "test_bot"


# ------------------------------------------------------------------
# RegistryRepository.update_heartbeat()
# ------------------------------------------------------------------

class TestRegistryRepositoryHeartbeat:
    def test_heartbeat_updates_timestamp(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.RUNNING)
        registry_repo.update_heartbeat(user_id, bot_id)
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded.last_heartbeat is not None
        assert loaded.operational_status == OperationalStatus.RUNNING

    def test_heartbeat_on_missing_row_is_noop(self, registry_repo, user_id, bot_id):
        # No row exists — UPDATE affects 0 rows, should not raise
        registry_repo.update_heartbeat(user_id, bot_id)


# ------------------------------------------------------------------
# RegistryRepository.mark_stopped() / mark_error()
# ------------------------------------------------------------------

class TestRegistryRepositoryStatusMethods:
    def test_mark_stopped(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.RUNNING)
        registry_repo.mark_stopped(user_id, bot_id)
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded.operational_status == OperationalStatus.STOPPED
        assert loaded.stopped_at is not None

    def test_mark_error(self, registry_repo, user_id, bot_id):
        registry_repo.upsert(user_id, bot_id, status=OperationalStatus.RUNNING)
        registry_repo.mark_error(user_id, bot_id, "Critical failure in OrderManager")
        loaded = registry_repo.load(user_id, bot_id)
        assert loaded.operational_status == OperationalStatus.ERROR
        assert loaded.error_message == "Critical failure in OrderManager"
        assert loaded.stopped_at is not None
