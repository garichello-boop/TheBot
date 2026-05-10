"""
tests/conftest.py — конфигурация pytest для unit-тестов.

Мокает внешние зависимости (PostgreSQL, psycopg2) ДО импорта тестовых
модулей. Должен быть на уровне модуля (не в хуках) — иначе не сработает
при фазе коллекции pytest.

Что мокается:
  db, db.connection    — используются в state_repo, registry_repo, bot_config
  psycopg2             — используется в bot_config/repository.py (advisory lock)
  psycopg2.extras      — используется там же

Все юнит-тесты работают без реального PostgreSQL.
Интеграционные тесты (требующие БД) помечать @pytest.mark.integration.
"""
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Мок-курсор и мок-соединение
# ---------------------------------------------------------------------------

class _MockCursor:
    """Минимальный мок курсора psycopg2 для unit-тестов."""
    def __init__(self):
        self.rowcount = 1

    def execute(self, sql, params=None):
        pass

    def executemany(self, sql, params=None):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _MockConnection:
    """Минимальный мок соединения psycopg2 для unit-тестов."""
    def __init__(self):
        self.dsn = "postgresql://mock:mock@localhost/mock"
        self.autocommit = False

    def cursor(self):
        return _MockCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Мок-функции для get_connection и transaction
# ---------------------------------------------------------------------------

@contextmanager
def _mock_get_connection():
    yield _MockConnection()


@contextmanager
def _mock_transaction():
    yield _MockCursor()


# ---------------------------------------------------------------------------
# Регистрация мок-модулей в sys.modules
# ВАЖНО: выполняется при импорте conftest, до коллекции тестов
# ---------------------------------------------------------------------------

def _register_db_mocks() -> None:
    """Зарегистрировать мок-модули для всей цепочки импортов."""

    # db и db.connection: используются в state_repo, registry_repo, bot_config
    db_mock = MagicMock()
    db_mock.get_connection = _mock_get_connection
    db_mock.transaction = _mock_transaction

    db_connection_mock = MagicMock()
    db_connection_mock.get_connection = _mock_get_connection
    db_connection_mock.transaction = _mock_transaction

    sys.modules.setdefault("db", db_mock)
    sys.modules.setdefault("db.connection", db_connection_mock)

    # psycopg2: используется в bot_config/repository.py для advisory lock
    psycopg2_mock = MagicMock()
    psycopg2_mock.connect = MagicMock(return_value=_MockConnection())
    psycopg2_mock.extensions = MagicMock()
    psycopg2_mock.extras = MagicMock()

    sys.modules.setdefault("psycopg2", psycopg2_mock)
    sys.modules.setdefault("psycopg2.extras", psycopg2_mock.extras)
    sys.modules.setdefault("psycopg2.extensions", psycopg2_mock.extensions)

    # pybit: используется в BybitOrderTracker (не нужен для unit-тестов)
    pybit_mock = MagicMock()
    sys.modules.setdefault("pybit", pybit_mock)
    sys.modules.setdefault("pybit.unified_trading", pybit_mock)


# Вызываем сразу при импорте conftest
_register_db_mocks()
