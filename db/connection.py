"""
db/connection.py

Synchronous PostgreSQL connection pool for the trading bot.
Uses psycopg2.pool.ThreadedConnectionPool — thread-safe, reusable connections.

Usage:
    # At bot startup (once):
    from db import init_pool
    init_pool(settings.database.dsn)

    # In repository code:
    from db import transaction
    with transaction() as cur:
        cur.execute("SELECT * FROM bot_configs WHERE bot_id = %s", (bot_id,))
        row = cur.fetchone()

    # Or raw connection (for SELECT FOR UPDATE with manual commit):
    from db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ... FOR UPDATE", ...)
        conn.commit()
"""

import logging
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# Module-level pool instance. Initialized once at startup via init_pool().
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def init_pool(dsn: str, min_conn: int = 1, max_conn: int = 5) -> None:
    """
    Initialize the connection pool. Must be called once at bot startup
    before any repository code runs.

    Args:
        dsn: PostgreSQL DSN, e.g. "postgresql://user:pass@host:5432/dbname"
        min_conn: Minimum connections kept alive.
        max_conn: Maximum simultaneous connections.
    """
    global _pool
    if _pool is not None:
        logger.warning("db.init_pool: pool already initialized, skipping.")
        return

    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=min_conn,
        maxconn=max_conn,
        dsn=dsn,
        cursor_factory=psycopg2.extras.RealDictCursor,  # rows as dicts, not tuples
    )
    logger.info(
        "PostgreSQL connection pool initialized (min=%d, max=%d).",
        min_conn,
        max_conn,
    )


def close_pool() -> None:
    """
    Close all connections and destroy the pool.
    Call at bot shutdown.
    """
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("PostgreSQL connection pool closed.")


def is_initialized() -> bool:
    """True if init_pool() has been called successfully."""
    return _pool is not None


@contextmanager
def get_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Borrow a connection from the pool and return it when done.

    Does NOT auto-commit or auto-rollback — the caller controls the transaction.
    Use `transaction()` for the common case of a single atomic operation.

    On any exception the connection is rolled back before being returned to
    the pool, so the next borrower always gets a clean state.

    Example (SELECT FOR UPDATE with explicit commit):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ... FOR UPDATE")
                row = cur.fetchone()
                cur.execute("UPDATE ...")
            conn.commit()
    """
    if _pool is None:
        raise RuntimeError(
            "Database pool not initialized. Call db.init_pool(dsn) at startup."
        )

    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _pool.putconn(conn)


@contextmanager
def transaction() -> Generator[psycopg2.extras.RealDictCursor, None, None]:
    """
    Context manager for a single atomic database transaction.

    Yields a RealDictCursor. Commits on clean exit, rolls back on exception.

    Example:
        with transaction() as cur:
            cur.execute(
                "UPDATE bot_configs SET status = %s WHERE bot_id = %s",
                ("STOPPED", bot_id),
            )
            # commit happens automatically here
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
