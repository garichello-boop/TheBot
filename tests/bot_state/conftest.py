import os
import pytest
from dotenv import load_dotenv


def _get_dsn() -> str:
    load_dotenv()
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "thebot")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    return f"host={host} port={port} dbname={name} user={user} password={password}"


@pytest.fixture(scope="session", autouse=True)
def init_db_pool():
    """Initialize DB pool once per test session."""
    from db.connection import init_pool, close_pool
    init_pool(dsn=_get_dsn(), min_conn=1, max_conn=5)
    yield
    close_pool()


@pytest.fixture
def user_id() -> str:
    return "test_user"


@pytest.fixture
def bot_id() -> str:
    return "test_bot"


@pytest.fixture
def bot_state(user_id, bot_id):
    from decimal import Decimal
    from bot_state.models import BotState
    return BotState.initial(user_id, bot_id, Decimal("1000.00"))


@pytest.fixture
def state_repo(user_id, bot_id):
    from bot_state.state_repo import StateRepository
    return StateRepository(user_id, bot_id)


@pytest.fixture
def registry_repo(user_id, bot_id):
    from bot_state.registry_repo import RegistryRepository
    return RegistryRepository(user_id, bot_id)


@pytest.fixture
def state_manager(state_repo):
    from bot_state.state_manager import StateManager
    return StateManager(repo=state_repo, emitter=None)


@pytest.fixture(autouse=True)
def clean_bot_state(init_db_pool, user_id, bot_id):
    """Delete test rows before each test. Runs automatically."""
    from db.connection import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bot_state WHERE user_id = %s AND bot_id = %s",
                (user_id, bot_id),
            )
            cur.execute(
                "DELETE FROM bot_registry WHERE user_id = %s AND bot_id = %s",
                (user_id, bot_id),
            )
        conn.commit()
    yield
