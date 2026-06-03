"""
bot_config/repository.py

Reads bot configuration from PostgreSQL.

ConfigRepository  — loads, reloads, and mutates bot_configs rows.
                    Acquires a session-level advisory lock at startup
                    (see ADR-001 below).

New in this version:
    get_history()  — returns audit trail from bot_configs_history.
    rollback()     — restores strategy_params from a history entry.
"""

from __future__ import annotations

import hashlib
import logging

import psycopg2
import psycopg2.extras

from db import get_connection, transaction
from .models import BotConfig, BotStatus, ConfigHistoryRow
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


class ConfigHistoryNotFoundError(Exception):
    """No bot_configs_history entry found for the requested config_version."""


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

    Audit trail (new):
        history = repo.get_history(user_id, bot_id, limit=20)
        config  = repo.rollback(user_id, bot_id, to_version=3)
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
        with transaction() as cur:
            cur.execute("SET LOCAL app.changed_by = 'bot'")
            cur.execute(
                """
                UPDATE bot_configs
                   SET status         = %s,
                       config_version = config_version + 1,
                       updated_at     = NOW()
                 WHERE user_id = %s
                   AND bot_id  = %s
                """,
                (status.value, user_id, bot_id),
            )
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
    # Audit trail: history
    # ------------------------------------------------------------------

    def get_history(
        self,
        user_id: str,
        bot_id: str,
        limit: int = 20,
    ) -> list[ConfigHistoryRow]:
        """
        Return the N most recent history entries for a bot, newest first.

        Each entry is a full snapshot of bot_configs at the moment of an
        INSERT or UPDATE (captured by the audit trigger).

        Args:
            limit: maximum rows to return (default 20, pass 0 for all).

        Returns:
            List of ConfigHistoryRow ordered by id DESC (newest first).
            Empty list if no history exists yet (e.g. before first change).
        """
        query = """
            SELECT id, user_id, bot_id, config_version,
                   ticker, strategy_name, strategy_params,
                   virtual_balance, status, changed_by, changed_at
              FROM bot_configs_history
             WHERE user_id = %s AND bot_id = %s
             ORDER BY id DESC
             LIMIT %s
        """
        with transaction() as cur:
            cur.execute(query, (user_id, bot_id, limit))
            rows = cur.fetchall()

        return [ConfigHistoryRow.from_row(dict(r)) for r in (rows or [])]

    # ------------------------------------------------------------------
    # Audit trail: rollback
    # ------------------------------------------------------------------

    def rollback(
        self,
        user_id: str,
        bot_id: str,
        to_version: int,
        changed_by: str = "operator",
    ) -> BotConfig:
        """
        Restore strategy_params from a specific history version.

        Reads bot_configs_history where config_version = to_version, then
        applies its strategy_params to the live bot_configs row, incrementing
        config_version by 1 so ConfigWatcher detects the change on the next
        cycle boundary.

        The audit trigger captures this rollback with:
            changed_by = "rollback_to_v{to_version}:{changed_by}"

        Args:
            to_version: config_version of the history entry to restore.
                        Use get_history() to list available versions.
            changed_by: who initiated the rollback (default "operator").
                        Pass "wfo" if called from a WFO script.

        Returns:
            Updated BotConfig with the new (incremented) config_version.

        Raises:
            ConfigHistoryNotFoundError: no history entry for to_version.
            BotConfigNotFoundError:     bot_configs row not found.

        Note: no position check is performed. The operator is responsible
        for deciding whether it is safe to roll back strategy_params while
        a cycle is active. If the bot is running, the new params take effect
        on the next tick via ConfigWatcher's normal hot-reload flow.
        """
        guc_value = f"rollback_to_v{to_version}:{changed_by}"

        with transaction() as cur:
            # Tag this transaction so the audit trigger records the rollback source.
            cur.execute("SET LOCAL app.changed_by = %s", (guc_value,))

            # 1. Fetch target strategy_params from history.
            cur.execute(
                """
                SELECT strategy_params
                  FROM bot_configs_history
                 WHERE user_id = %s AND bot_id = %s AND config_version = %s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (user_id, bot_id, to_version),
            )
            hist = cur.fetchone()
            if hist is None:
                raise ConfigHistoryNotFoundError(
                    f"No history entry found for {user_id!r}/{bot_id!r} "
                    f"at config_version={to_version}. "
                    f"Call get_history() to list available versions."
                )

            strategy_params = dict(hist["strategy_params"] or {})

            # 2. Apply rollback. RETURNING avoids a second round-trip to read
            #    the new state. The trigger fires here and writes to history.
            cur.execute(
                """
                UPDATE bot_configs
                   SET strategy_params = %s,
                       config_version  = config_version + 1,
                       updated_at      = NOW()
                 WHERE user_id = %s AND bot_id = %s
                 RETURNING
                    user_id, bot_id, ticker, exchange,
                    strategy_name, strategy_params, virtual_balance,
                    status, config_version, created_at, updated_at
                """,
                (psycopg2.extras.Json(strategy_params), user_id, bot_id),
            )
            row = cur.fetchone()

        if row is None:
            raise BotConfigNotFoundError(
                f"rollback: no row in bot_configs for "
                f"user_id={user_id!r}, bot_id={bot_id!r}."
            )

        new_config = BotConfig.from_row(dict(row))
        logger.info(
            "ConfigRepository: rolled back %s/%s strategy_params "
            "from history v%d → new config_version=%d (changed_by=%r).",
            user_id, bot_id, to_version, new_config.config_version, changed_by,
        )
        return new_config

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
