"""
FileLogSink — человекочитаемый лог в bot.log.

Синхронный. RotatingFileHandler: 10 МБ, 10 файлов.
Фильтрация по min_level: события ниже уровня игнорируются.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from observability.events import BotEvent
from observability.sinks.base import AbstractSink

_LOG_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class FileLogSink(AbstractSink):

    def __init__(
        self,
        log_path: str = "logs/bot.log",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 10,
        min_level: str = "DEBUG",
    ) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)

        self._min_level_weight = _LOG_LEVEL_MAP.get(min_level.upper(), logging.DEBUG)

        # Уникальное имя логгера по пути файла — исключает дублирование handler-ов
        logger_name = f"obs.file.{log_path.replace('/', '_').replace('.', '_')}"
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False

        if not self._logger.handlers:
            handler = RotatingFileHandler(
                filename=log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
            self._logger.addHandler(handler)

    def handle(self, event: BotEvent) -> None:
        try:
            log_level = _LOG_LEVEL_MAP.get(event.level, logging.DEBUG)
            if log_level < self._min_level_weight:
                return

            parts = [f"[{event.bot_id}]", f"[{event.event_type}]"]
            if event.cycle_id:
                parts.append(f"[cycle={event.cycle_id}]")
            if event.ticker:
                parts.append(f"[{event.ticker}]")
            parts.append(event.message)
            if event.payload:
                parts.append(f"| {event.payload}")

            self._logger.log(log_level, " ".join(parts))
        except Exception:
            pass  # Sink не останавливает бота

    def close(self) -> None:
        for handler in self._logger.handlers[:]:
            handler.close()
            self._logger.removeHandler(handler)
