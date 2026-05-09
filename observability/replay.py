"""
ReplayManager — восстановление пропущенных событий из NDJSON в PostgreSQL.

При старте читает все файлы ротации в хронологическом порядке,
сопоставляет с PostgreSQL по (ts_ms, bot_id, event_type),
досылает пропущенные события батчами.

Потеря данных исключена даже при длительном падении БД.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from typing import Iterator, List, Optional, Set, Tuple

from observability.events import BotEvent

_LOG = logging.getLogger(__name__)

# Сигнатура события для дедупликации
EventSignature = Tuple[int, str, str]  # (ts_ms, bot_id, event_type)

_INSERT_SQL = """
INSERT INTO bot_events
    (ts_ms, level, event_type, event_version, message, bot_id, ticker, cycle_id, strategy_name, payload)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""


class ReplayManager:
    """
    Читает NDJSON-файлы и досылает пропущенные события в PostgreSQL.

    Использование:
        manager = ReplayManager(events_path="logs/events.ndjson", bot_id="phor_dca")
        replayed = manager.replay(conn)
        print(f"Досланo {replayed} событий")
    """

    def __init__(
        self,
        events_path: str = "logs/events.ndjson",
        bot_id_filter: Optional[str] = None,
        batch_size: int = 100,
    ) -> None:
        self._events_path = events_path
        self._bot_id_filter = bot_id_filter
        self._batch_size = batch_size

    def replay(self, conn) -> int:
        """
        Досылает пропущенные события из NDJSON в PostgreSQL.
        Возвращает количество вставленных событий.
        conn — psycopg2 connection.
        """
        _LOG.info(
            f"ReplayManager: начало replay. "
            f"NDJSON: {self._events_path}, фильтр bot_id: {self._bot_id_filter or 'все'}"
        )

        existing_signatures = self._load_existing_signatures(conn, self._bot_id_filter)
        _LOG.info(f"ReplayManager: в БД найдено {len(existing_signatures)} сигнатур")

        replayed = 0
        batch: List[BotEvent] = []

        for event in self._iter_events_chronological():
            sig = self._signature(event)
            if sig not in existing_signatures:
                batch.append(event)
                if len(batch) >= self._batch_size:
                    replayed += self._insert_batch(conn, batch)
                    batch.clear()

        if batch:
            replayed += self._insert_batch(conn, batch)

        _LOG.info(f"ReplayManager: replay завершён, вставлено {replayed} событий")
        return replayed

    def count_ndjson_events(self) -> int:
        """Количество событий в NDJSON-файлах (для мониторинга)."""
        return sum(1 for _ in self._iter_events_chronological())

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _iter_events_chronological(self) -> Iterator[BotEvent]:
        """Читает все файлы ротации в хронологическом порядке."""
        for path in self._get_rotation_files_ordered():
            yield from self._read_ndjson_file(path)

    def _get_rotation_files_ordered(self) -> List[str]:
        """
        Возвращает файлы в хронологическом порядке:
        events.ndjson.20 → ... → events.ndjson.1 → events.ndjson

        RotatingFileHandler: большой номер = более старый файл.
        """
        base = self._events_path
        rotated = glob.glob(f"{base}.*")

        def rotation_key(path: str) -> int:
            suffix = path.rsplit(".", 1)[-1]
            try:
                return int(suffix)
            except ValueError:
                return 0

        rotated_sorted = sorted(
            [f for f in rotated if f.split(".")[-1].isdigit()],
            key=rotation_key,
            reverse=True,  # большой номер = старый = первый
        )

        result = rotated_sorted
        if os.path.exists(base):
            result = result + [base]
        return result

    def _read_ndjson_file(self, path: str) -> Iterator[BotEvent]:
        """Читает NDJSON-файл построчно, пропускает невалидные строки."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        event = BotEvent.from_dict(data)
                        if self._bot_id_filter and event.bot_id != self._bot_id_filter:
                            continue
                        yield event
                    except (json.JSONDecodeError, KeyError, ValueError) as exc:
                        _LOG.warning(
                            f"ReplayManager: невалидная строка {line_num} в {path}: {exc}"
                        )
        except OSError as exc:
            _LOG.error(f"ReplayManager: не удалось открыть {path}: {exc}")

    def _load_existing_signatures(self, conn, bot_id: Optional[str]) -> Set[EventSignature]:
        """
        Загружает сигнатуры из PostgreSQL для дедупликации.
        Читает только за период покрытый NDJSON-файлами.
        """
        min_ts = self._get_min_ts_from_files()
        if min_ts is None:
            return set()

        query = "SELECT ts_ms, bot_id, event_type FROM bot_events WHERE ts_ms >= %s"
        params: list = [min_ts]

        if bot_id:
            query += " AND bot_id = %s"
            params.append(bot_id)

        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            return {(row[0], row[1], row[2]) for row in rows}
        except Exception as exc:
            _LOG.error(f"ReplayManager: не удалось загрузить сигнатуры из БД: {exc}")
            return set()

    def _get_min_ts_from_files(self) -> Optional[int]:
        for event in self._iter_events_chronological():
            return event.ts_ms
        return None

    def _insert_batch(self, conn, batch: List[BotEvent]) -> int:
        try:
            rows = [
                (
                    e.ts_ms, e.level, e.event_type, e.event_version,
                    e.message, e.bot_id, e.ticker, e.cycle_id,
                    e.strategy_name, json.dumps(e.payload, ensure_ascii=False),
                )
                for e in batch
            ]
            with conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, rows)
            conn.commit()
            return len(batch)
        except Exception as exc:
            _LOG.error(f"ReplayManager: ошибка вставки батча: {exc}")
            try:
                conn.rollback()
            except Exception:
                pass
            return 0

    @staticmethod
    def _signature(event: BotEvent) -> EventSignature:
        return (event.ts_ms, event.bot_id, event.event_type)
