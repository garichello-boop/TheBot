from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from db.connection import get_connection, transaction
from bot_state.models import BotRegistry, OperationalStatus


class RegistryRepository:
    """
    Read and write bot_registry table.

    bot_registry tracks operational status of the bot process (heartbeat, pid).
    Separate from bot_state: different access patterns, different write frequency.
    Heartbeat updates happen every N ticks; state updates happen on every order event.
    """

    def __init__(self, user_id: str, bot_id: str) -> None:
        self.user_id = user_id
        self.bot_id = bot_id

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self) -> Optional[BotRegistry]:
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
                cur.execute(sql, (self.user_id, self.bot_id))
                row = cur.fetchone()
                if row is None:
                    return None
                return BotRegistry.from_row(dict(row))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert(
        self,
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
        Only provided keyword arguments are written; others keep their DB value.
        pid defaults to current process PID when not explicitly passed.
        """
        effective_pid = pid if pid is not None else os.getpid()

        sql = """
            INSERT INTO bot_registry (
                user_id, bot_id, operational_status,
                pid, started_at, stopped_at, error_message, last_heartbeat
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, bot_id) DO UPDATE SET
                operational_status  = EXCLUDED.operational_status,
                pid                 = EXCLUDED.pid,
                started_at          = COALESCE(EXCLUDED.started_at,  bot_registry.started_at),
                stopped_at          = COALESCE(EXCLUDED.stopped_at,  bot_registry.stopped_at),
                error_message       = COALESCE(EXCLUDED.error_message, bot_registry.error_message),
                last_heartbeat      = COALESCE(EXCLUDED.last_heartbeat, bot_registry.last_heartbeat)
        """
        params = (
            self.user_id,
            self.bot_id,
            status.value,
            effective_pid,
            started_at,
            stopped_at,
            error_message,
            last_heartbeat,
        )
        with transaction() as cur:
            cur.execute(sql, params)

    def update_heartbeat(self) -> None:
        """
        Fast-path heartbeat update: only touches last_heartbeat and operational_status.
        Called every HEARTBEAT_INTERVAL_TICKS ticks — must be lightweight.
        """
        sql = """
            UPDATE bot_registry
            SET last_heartbeat     = %s,
                operational_status = %s
            WHERE user_id = %s AND bot_id = %s
        """
        now = datetime.now(timezone.utc)
        with transaction() as cur:
            cur.execute(
                sql,
                (now, OperationalStatus.RUNNING.value, self.user_id, self.bot_id),
            )

    def mark_error(self, message: str) -> None:
        """Set status ERROR with error message. Called on BOT_CRASHED."""
        sql = """
            UPDATE bot_registry
            SET operational_status = %s,
                error_message      = %s,
                stopped_at         = %s
            WHERE user_id = %s AND bot_id = %s
        """
        now = datetime.now(timezone.utc)
        with transaction() as cur:
            cur.execute(
                sql,
                (
                    OperationalStatus.ERROR.value,
                    message,
                    now,
                    self.user_id,
                    self.bot_id,
                ),
            )

    def mark_stopped(self) -> None:
        """Set status STOPPED with stopped_at timestamp. Called on clean shutdown."""
        sql = """
            UPDATE bot_registry
            SET operational_status = %s,
                stopped_at         = %s
            WHERE user_id = %s AND bot_id = %s
        """
        now = datetime.now(timezone.utc)
        with transaction() as cur:
            cur.execute(
                sql,
                (OperationalStatus.STOPPED.value, now, self.user_id, self.bot_id),
            )
