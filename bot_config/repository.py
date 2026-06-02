"""
bot_config/repository.py

Reads bot configuration from PostgreSQL.
"""

from __future__ import annotations

import hashlib
import logging

import psycopg2
import psycopg2.extras

from db import get_connection, transaction
from .models import BotConfig, BotStatus
from .validator import ConfigValidator, ValidationResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ADR-001: Advisory lock instead of SELECT FOR UPDATE
# ---------------------------------------------------------------------------
#
# The spec (Point 5) says "SELECT FOR UPDATE" to prevent duplicate bot
# instances. We use a PostgreSQL advisory lock instead. Here is why.
#
# SELECT FOR UPDATE holds a row-level lock only for the duration of the
# enclosing transaction. As soon as that transaction commits, the lock is
# gone. Since WFO must be able to UPDATE bot_configs at any time, the
# startup transaction must commit quickly — which means the lock lasts
# milliseconds, not hours. A second process starting 1 second later sees
# no lock at all. SELECT FOR UPDATE does not solve the problem.
#
# pg_try_advisory_lock() is a SESSION-LEVEL lock:
#   - Held for the entire lifetime of the database connection.
#   - Released automatically if the connection closes (crash or shutdown).
#   - pg_try_advisory_lock() returns False immediately if another session
#     holds the lock — no waiting, no blocking.
#   - Does not interfere with WFO UPDATEs to bot_configs.
#
# Implementation detail: the lock is held on a DEDICATED connection
# (_lock_conn) that is never returned to the connection pool. Advisory
# locks are tied to the session; returning a locked connection to the pool
# would release the lock when another borrower resets it.
#
# The second layer of duplicate protection (ongoing "is bot alive" check)
# is the bot_registry heartbeat implemented in Point 6.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BotConfigNotFoundError(Exception):
    """No row found in bot_configs for the given (user_id, bot_id)."""


class BotAlreadyRunningError(Exception):
    """Advisory lock is held by another process — bot is already running."""


class BotConfigInvalidError(Exception):
    """Config loaded from DB failed validation."""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class ConfigRepository:
    """
    Loads and reloads bot configuration from bot_configs.

    Constructor
    -----------
    ConfigRepository(db_pool, validator=None)

    db_pool  — connection pool created by create_pool() in bot.py.
               Stored as self._pool; get_connection()/transaction() use the
               globally configured pool (same pattern as StateRepository).
    validator — optional ConfigValidator; defaults to ConfigValidator() if None.

    Lifecycle:
        repo = ConfigRepository(db_pool)
        config = repo.load(user_id, bot_id)    # startup: lock + validate
        ...
        config = repo.reload(user_id, bot_id)  # before new cycle: no lock
        ...
        repo.release(user_id, bot_id)           # shutdown: release lock
    """

    def __init__(
        self,
        db_pool,
        validator: ConfigValidator | None = None,
    ) -> None:
        self._pool = db_pool  # stored; get_connection()/transaction() use global pool
        self._validator = validator if validator is not None else ConfigValidator()
        # Dedicated connection that holds the advisory lock for the bot's lifetime.
        # See ADR-001 above for why this must not be a pool connection.
        self._lock_conn: psycopg2.extensions.connection | None = None

    # ------------------------------------------------------------------
    # Startup load (with advisory lock)
    # ------------------------------------------------------------------

    def load(self, user_id: str, bot_id: str) -> BotConfig:
        """
        Load config at startup. Acquires a session-level advisory lock first.

        Raises:
            BotAlreadyRunningError  — another process holds the lock.
            BotConfigNotFoundError  — no row in bot_configs.
            BotConfigInvalidError   — config failed validation.
        """
        self._acquire_lock(user_id, bot_id)

        try:
            config = self._select(user_id, bot_id)
        except Exception:
            self._release_lock()
            raise

        result = self._validator.validate(config)
        if not result.is_valid:
            self._release_lock()
            raise BotConfigInvalidError(
                f"Config for {user_id}/{bot_id} is invalid at startup: "
                + "; ".join(result.errors)
            )

        logger.info(
            "ConfigRepository: loaded config for %s/%s "
            "(strategy=%s, version=%d, status=%s).",
            user_id, bot_id,
            config.strategy_name, config.config_version, config.status.value,
        )
        return config

    # ------------------------------------------------------------------
    # Hot-reload (no lock, soft validation)
    # ------------------------------------------------------------------

    def reload(self, user_id: str, bot_id: str) -> tuple[BotConfig, ValidationResult]:
        """
        Reload config before a new trading cycle. No lock acquired.

        Returns (config, result). If validation fails the caller should
        keep the previous config and emit CONFIG_ERROR — do not raise here.

        Raises:
            BotConfigNotFoundError — row disappeared from DB (unexpected).
        """
        config = self._select(user_id, bot_id)
        result = self._validator.validate(config)

        if result.is_valid:
            logger.info(
                "ConfigRepository: reloaded config for %s/%s (version=%d).",
                user_id, bot_id, config.config_version,
            )
        else:
            logger.error(
                "ConfigRepository: reloaded config for %s/%s is INVALID "
                "(version=%d): %s",
                user_id, bot_id, config.config_version,
                "; ".join(result.errors),
            )

        return config, result

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def release(self, user_id: str, bot_id: str) -> None:
        """
        Explicitly release the advisory lock and close the lock connection.
        Call at clean shutdown. On crash, PostgreSQL releases it automatically.
        """
        self._release_lock()
        logger.info(
            "ConfigRepository: advisory lock released for %s/%s.", user_id, bot_id
        )

    # ------------------------------------------------------------------
    # Status update
    # ------------------------------------------------------------------

    def set_status(self, user_id: str, bot_id: str, status: BotStatus) -> None:
        """
        Programmatically update bot status in bot_configs.

        Called by the bot itself when a status change is required without
        operator intervention — for example, when a TP is cancelled manually
        (FSM → CLOSE_ONLY) or a STOP_CRANE condition is detected.

        Increments config_version so ConfigWatcher detects the change on
        the next cycle boundary and reloads the full config.

        Note: this is a deliberate exception to the rule that only the
        operator and WFO write to bot_configs. The bot writes status only
        — never strategy_params or virtual_balance.

        Raises:
            BotConfigNotFoundError: row not found in bot_configs.
        """
        query = """
            UPDATE bot_configs
               SET status         = %s,
                   config_version = config_version + 1,
                   updated_at     = NOW()
             WHERE user_id = %s
               AND bot_id  = %s
        """
        with transaction() as cur:
            cur.execute(query, (status.value, user_id, bot_id))
            if cur.rowcount == 0:
                raise BotConfigNotFoundError(
                    f"set_status: no row in bot_configs for "
                    f"user_id={user_id!r}, bot_id={bot_id!r}."
                )

        logger.info(
            "ConfigRepository: status set to %r for %s/%s.",
            status.value, user_id, bot_id,
        )

    # ------------------------------------------------------------------
    # Advisory lock internals
    # ------------------------------------------------------------------

    def _acquire_lock(self, user_id: str, bot_id: str) -> None:
        if self._lock_conn is not None:
            logger.warning(
                "ConfigRepository._acquire_lock: lock connection already open, "
                "releasing before re-acquiring."
            )
            self._release_lock()

        lock_key = _advisory_key(user_id, bot_id)

        # Borrow a pool connection only to read its DSN, then open our own.
        # See ADR-001: the lock connection must not be returned to the pool.
        import os
        self._lock_conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "thebot"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "mypassword123"),
        )
        self._lock_conn.autocommit = True

        with self._lock_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s);", (lock_key,))
            acquired: bool = cur.fetchone()[0]

        if not acquired:
            self._lock_conn.close()
            self._lock_conn = None
            raise BotAlreadyRunningError(
                f"Bot {user_id}/{bot_id} is already running in another process. "
                f"Stop it before starting a new instance."
            )

        logger.debug(
            "ConfigRepository: advisory lock acquired for %s/%s (key=%d).",
            user_id, bot_id, lock_key,
        )

    def _release_lock(self) -> None:
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()
            except Exception:
                pass
            self._lock_conn = None

    # ------------------------------------------------------------------
    # DB select
    # ------------------------------------------------------------------

    def _select(self, user_id: str, bot_id: str) -> BotConfig:
        """Plain SELECT — used by both load() and reload()."""
        query = """
            SELECT
                user_id, bot_id, ticker, exchange,
                strategy_name, strategy_params, virtual_balance,
                status, config_version, created_at, updated_at
            FROM bot_configs
            WHERE user_id = %s AND bot_id = %s
        """
        with transaction() as cur:
            cur.execute(query, (user_id, bot_id))
            row = cur.fetchone()

        if row is None:
            raise BotConfigNotFoundError(
                f"No config found in bot_configs for user_id={user_id!r}, "
                f"bot_id={bot_id!r}. "
                f"Insert a row before starting the bot."
            )

        return BotConfig.from_row(dict(row))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _advisory_key(user_id: str, bot_id: str) -> int:
    """
    Derive a stable int64 advisory lock key from (user_id, bot_id).
    SHA-256 first 8 bytes, interpreted as signed big-endian int64.
    Collision probability for any two distinct pairs: ~1 in 2^63.
    """
    raw = f"{user_id}:{bot_id}".encode()
    digest = hashlib.sha256(raw).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)
