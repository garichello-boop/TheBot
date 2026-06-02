"""
tests/bot_state/conftest.py

Unit-тест фикстуры для bot_state.
Использует мок db_pool — реального PostgreSQL не требует.
DB-слой мокается корневым tests/conftest.py (db, db.connection, psycopg2).
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock


@pytest.fixture
def user_id() -> str:
    return "test_user"


@pytest.fixture
def bot_id() -> str:
    return "test_bot"


@pytest.fixture
def db_pool():
    """Мок пула соединений. Репозитории принимают его в __init__."""
    return MagicMock()


@pytest.fixture
def bot_state(user_id, bot_id):
    from bot_state.models import BotState
    return BotState.initial(user_id, bot_id, Decimal("1000.00"))


@pytest.fixture
def state_repo(db_pool):
    from bot_state.state_repo import StateRepository
    return StateRepository(db_pool)


@pytest.fixture
def registry_repo(db_pool):
    from bot_state.registry_repo import RegistryRepository
    return RegistryRepository(db_pool)


@pytest.fixture
def state_manager(db_pool):
    from bot_state.state_manager import StateManager
    return StateManager(db_pool, emitter=None)
