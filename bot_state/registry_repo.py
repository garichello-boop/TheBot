from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from db.connection import get_connection, transaction
from bot_state.models import BotRegistry, OperationalStatus


class RegistryRepository:
    """
    Read and write bot_registry table.

    Stateless repository: db_pool accepted at construction, user_id/bot_id
    passed explicitly to every method. A single instance can serve multiple
    bots (monitoring, multi-bot manager).

    get_connection() / transaction() use the globally configured pool
    (initialised by create_pool() in bot.py before any repo is created).
    self._pool is stored for future direct-pool usage or re-initialisation.

    Public write API
    ----------------
    upsert()           — full insert-or-update; COALESCE keeps existing
                         DB value for every None keyword argument.
    update_heartbeat() — fast-path: only last_heartbeat + RUNNING status.
                         Called every HEARTBEAT_INTERVAL_TICKS ticks.
    update_status()    — semantic shortcut for STOPPED / ERROR transitions.
                         Called by HeartbeatEmitter.mark_stopped/mark_error.
    """

    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self, user_id: str, bot_id: str) -> Optional[BotRegistry]:
        """Load registry row. Returns None if bot has never been started."""
        sql = """
            SELECT
                user_id, bot_id, operational_status,
                last_heartbeat, pid, started_at, stopped_at, error_message
            FROM bot_registry
            WHERE user_id = %s AND bot_id = %s
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (user_id, bot_id))
                row = cur.fetchone()
                if row is None:
                    return None
                return BotRegistry.from_row(dict(row))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert(
        self,
        user_id: str,
        bot_id: str,
        status: OperationalStatus,
        *,
        pid: Optional[int] = None,
        started_at: Optional[datetime] = None,
        stopped_at: Optional[datetime] = None,
        error_message: Optional[str] = None,
        last_heartbeat: Optional[datetime] = None,
    ) -> None:
        """
        Insert or update registry row.

        COALESCE semantics: keyword arguments that are None keep their
        existing DB value. Pass explicitly to overwrite.

        Common patterns:
          upsert(user_id, bot_id, STARTING, started_at=now)
          upsert(user_id, bot_id, RUNNING,  last_heartbeat=now)
          upsert(user_id, bot_id, STOPPED,  stopped_at=now)
          upsert(user_id, bot_id, ERROR,    error_message=msg, stopped_at=now)

        pid defaults to current process PID when not explicitly passed.
        """
        effective_pid = pid if pid is not None else os.getpid()

        sql = """
            INSERT INTO bot_registry (
                user_id, bot_id, operational_status,
                pid, started_at, stopped_at, error_message, last_heartbeat
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, bot_id) DO UPDATE SET
                operational_status = EXCLUDED.operational_status,
                pid                = EXCLUDED.pid,
                started_at         = COALESCE(EXCLUDED.started_at,    bot_registry.started_at),
                stopped_at         = COALESCE(EXCLUDED.stopped_at,    bot_registry.stopped_at),
                error_message      = COALESCE(EXCLUDED.error_message,  bot_registry.error_message),
                last_heartbeat     = COALESCE(EXCLUDED.last_heartbeat, bot_registry.last_heartbeat)
        """
        params = (
            user_id, bot_id,
            status.value,
            effective_pid,
            started_at, stopped_at, error_message, last_heartbeat,
        )
        with transaction() as cur:
            cur.execute(sql, params)

    def update_heartbeat(self, user_id: str, bot_id: str) -> None:
        """
        Fast-path heartbeat update: only touches last_heartbeat + status.
        Called every HEARTBEAT_INTERVAL_TICKS ticks — must be lightweight.
        Skips the INSERT / ON CONFLICT overhead of upsert().
        """
        sql = """
            UPDATE bot_registry
            SET last_heartbeat     = %s,
                operational_status = %s
            WHERE user_id = %s AND bot_id = %s
        """
        now = datetime.now(timezone.utc)
        with transaction() as cur:
            cur.execute(sql, (now, OperationalStatus.RUNNING.value, user_id, bot_id))

    def update_status(
        self,
        user_id: str,
        bot_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Semantic shortcut for lifecycle status transitions.

        Called by HeartbeatEmitter:
          mark_stopped() → update_status(uid, bid, "STOPPED")
          mark_error()   → update_status(uid, bid, "ERROR", error_message=msg)

        status: str matching OperationalStatus enum value ("STOPPED", "ERROR", …).
        Sets stopped_at=now for STOPPED and ERROR; leaves it unchanged otherwise.
        """
        op_status = OperationalStatus(status)
        now = datetime.now(timezone.utc)
        stopped_at = (
            now
            if op_status in (OperationalStatus.STOPPED, OperationalStatus.ERROR)
            else None
        )
        self.upsert(
            user_id, bot_id,
            status=op_status,
            stopped_at=stopped_at,
            error_message=error_message,
        )
