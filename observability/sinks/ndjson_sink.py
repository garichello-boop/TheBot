"""
NdjsonSink — структурированный поток событий в NDJSON-формате.

Один JSON = одна строка. Синхронный.
Два файла:
  - events.ndjson — все события (50 МБ, 20 файлов). Источник для ReplayManager.
  - errors.ndjson  — только ERROR и CRITICAL (10 МБ, 10 файлов). Быстрая диагностика.
"""

from __future__ import annotations

import json
import os
from logging.handlers import RotatingFileHandler
import logging
from typing import Optional

from observability.events import BotEvent
from observability.sinks.base import AbstractSink

_ERROR_LEVELS = frozenset({"ERROR", "CRITICAL"})


class _RawLineHandler(logging.Handler):
    """Пишет record.msg как есть — уже сериализованный JSON."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self.stream  # type: ignore[attr-defined]
            stream.write(record.getMessage() + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


class _RotatingRawFileHandler(RotatingFileHandler):
    """RotatingFileHandler с кастомным форматом — пишет сырые строки."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            self.stream.write(record.getMessage() + "\n")
            self.stream.flush()
        except Exception:
            self.handleError(record)


class NdjsonSink(AbstractSink):

    def __init__(
        self,
        events_path: str = "logs/events.ndjson",
        errors_path: str = "logs/errors.ndjson",
        events_max_bytes: int = 50 * 1024 * 1024,
        events_backup_count: int = 20,
        errors_max_bytes: int = 10 * 1024 * 1024,
        errors_backup_count: int = 10,
    ) -> None:
        self._events_path = events_path
        self._errors_path = errors_path

        self._events_logger = self._make_logger(
            events_path, events_max_bytes, events_backup_count, "obs.ndjson.events"
        )
        self._errors_logger = self._make_logger(
            errors_path, errors_max_bytes, errors_backup_count, "obs.ndjson.errors"
        )

    @staticmethod
    def _make_logger(path: str, max_bytes: int, backup_count: int, name: str) -> logging.Logger:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:
            handler = _RotatingRawFileHandler(
                filename=path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            logger.addHandler(handler)
        return logger

    def handle(self, event: BotEvent) -> None:
        try:
            line = json.dumps(event.to_dict(), ensure_ascii=False)
            self._events_logger.info(line)
            if event.level in _ERROR_LEVELS:
                self._errors_logger.info(line)
        except Exception:
            pass

    def close(self) -> None:
        for logger in (self._events_logger, self._errors_logger):
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
