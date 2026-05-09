"""
PostgresSink — запись событий в PostgreSQL через фоновый поток.

Батчинг: накапливает события и сбрасывает батчами для производительности.
При недоступности PostgreSQL события не теряются — остаются в events.ndjson.
Таблица bot_events создаётся автоматически если не существует.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import List, Optional

from observability.events import BotEvent
from observability.sinks.base import AbstractSink

_LOG = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bot_events (
    id          BIGSERIAL PRIMARY KEY,
    ts_ms       BIGINT      NOT NULL,
    level       TEXT        NOT NULL,
    event_type  TEXT        NOT NULL,
    event_version INT       NOT NULL DEFAULT 1,
    message     TEXT        NOT NULL,
    bot_id      TEXT        NOT NULL,
    ticker      TEXT        NOT NULL DEFAULT '',
    cycle_id    TEXT        NOT NULL DEFAULT '',
    strategy_name TEXT      NOT NULL DEFAULT '',
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_events_bot_id    ON bot_events (bot_id);
CREATE INDEX IF NOT EXISTS idx_bot_events_cycle_id  ON bot_events (cycle_id);
CREATE INDEX IF NOT EXISTS idx_bot_events_ts_ms     ON bot_events (ts_ms);
CREATE INDEX IF NOT EXISTS idx_bot_events_event_type ON bot_events (event_type);
"""

_INSERT_SQL = """
INSERT INTO bot_events
    (ts_ms, level, event_type, event_version, message, bot_id, ticker, cycle_id, strategy_name, payload)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class PostgresSink(AbstractSink):

    def __init__(
        self,
        dsn: str,
        batch_size: int = 50,
        flush_interval_sec: float = 5.0,
        queue_maxsize: int = 1000,
    ) -> None:
        self._dsn = dsn
        self._batch_size = batch_size
        self._flush_interval_sec = flush_interval_sec

        self._queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._conn = None

        self._thread = threading.Thread(
            target=self._worker,
            name="postgres-sink",
            daemon=True,
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # AbstractSink
    # ------------------------------------------------------------------

    def handle(self, event: BotEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            _LOG.warning(
                f"PostgresSink: очередь полна, событие потеряно: {event.event_type}. "
                f"Событие сохранено в NDJSON."
            )
        except Exception:
            pass

    def flush(self, timeout: float = 15.0) -> None:
        try:
            self._queue.join()
        except Exception:
            pass

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=20)
        self._close_conn()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        self._ensure_connected()
        last_flush = time.monotonic()
        batch: List[BotEvent] = []

        while not self._stop_event.is_set():
            timeout = max(0.1, self._flush_interval_sec - (time.monotonic() - last_flush))
            try:
                item = self._queue.get(timeout=timeout)
                if item is None:
                    self._queue.task_done()
                    break
                batch.append(item)
                self._queue.task_done()
            except queue.Empty:
                pass

            # Flush по размеру или по времени
            elapsed = time.monotonic() - last_flush
            if len(batch) >= self._batch_size or (batch and elapsed >= self._flush_interval_sec):
                self._flush_batch(batch)
                batch.clear()
                last_flush = time.monotonic()

        # Финальный flush при остановке
        if batch:
            self._flush_batch(batch)
        # Остаток в очереди
        remaining = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                self._queue.task_done()
                if item is not None:
                    remaining.append(item)
            except queue.Empty:
                break
        if remaining:
            self._flush_batch(remaining)

    def _flush_batch(self, batch: List[BotEvent]) -> None:
        if not batch:
            return
        if not self._ensure_connected():
            _LOG.error(
                f"PostgresSink: нет соединения с БД, {len(batch)} событий потеряно. "
                f"Данные сохранены в NDJSON."
            )
            return
        try:
            rows = [
                (
                    e.ts_ms, e.level, e.event_type, e.event_version,
                    e.message, e.bot_id, e.ticker, e.cycle_id,
                    e.strategy_name, json.dumps(e.payload, ensure_ascii=False),
                )
                for e in batch
            ]
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.executemany(_INSERT_SQL, rows)
            self._conn.commit()  # type: ignore[union-attr]
        except Exception as exc:
            _LOG.error(f"PostgresSink: ошибка записи батча ({len(batch)} событий): {exc}")
            self._close_conn()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> bool:
        if self._conn is not None:
            try:
                self._conn.cursor().execute("SELECT 1")
                return True
            except Exception:
                self._close_conn()

        try:
            import psycopg2  # type: ignore
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = False
            with self._conn.cursor() as cur:
                cur.execute(_CREATE_TABLE_SQL)
            self._conn.commit()
            _LOG.info("PostgresSink: подключение к БД установлено")
            return True
        except Exception as exc:
            _LOG.error(f"PostgresSink: не удалось подключиться к PostgreSQL: {exc}")
            self._conn = None
            return False

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
