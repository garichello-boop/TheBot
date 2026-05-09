"""
EventEmitter — центральная точка публикации событий.

Синхронный: emit() вызывается из торгового цикла и сразу возвращает управление.
TelegramSink и PostgresSink используют фоновые потоки — торговый цикл не блокируется.
FileLogSink и NdjsonSink синхронные — пишут локально без задержек.

Ошибка любого sink изолирована: записывается в fallback logger, бот не останавливается.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from observability.events import BotEvent
from observability.router import NotificationRouter
from observability.sinks.base import AbstractSink

_FALLBACK = logging.getLogger("observability.fallback")


class EventEmitter:

    def __init__(
        self,
        bot_id: str,
        ticker: str = "",
        strategy_name: str = "",
        cycle_id: str = "",
    ) -> None:
        self._bot_id = bot_id
        self._ticker = ticker
        self._strategy_name = strategy_name
        self._cycle_id = cycle_id

        self._file_sinks: List[AbstractSink] = []       # Синхронные (file, ndjson)
        self._telegram_sink: Optional[AbstractSink] = None
        self._postgres_sink: Optional[AbstractSink] = None
        self._router = NotificationRouter()

    # ------------------------------------------------------------------
    # Конфигурация sink-ов
    # ------------------------------------------------------------------

    def add_file_sink(self, sink: AbstractSink) -> None:
        self._file_sinks.append(sink)

    def set_telegram_sink(self, sink: AbstractSink) -> None:
        self._telegram_sink = sink

    def set_postgres_sink(self, sink: AbstractSink) -> None:
        self._postgres_sink = sink

    def set_cycle_id(self, cycle_id: str) -> None:
        """Обновить текущий cycle_id — вызывать при старте/закрытии цикла."""
        self._cycle_id = cycle_id

    # ------------------------------------------------------------------
    # Публикация
    # ------------------------------------------------------------------

    def emit(
        self,
        event_type: str,
        level: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        cycle_id: Optional[str] = None,
        ticker: Optional[str] = None,
    ) -> BotEvent:
        """
        Создать и опубликовать событие.

        cycle_id и ticker можно переопределить для конкретного события.
        Если не переданы — используются значения из контекста эмиттера.
        """
        event = BotEvent(
            event_type=event_type,
            level=level,
            message=message,
            bot_id=self._bot_id,
            ticker=ticker if ticker is not None else self._ticker,
            cycle_id=cycle_id if cycle_id is not None else self._cycle_id,
            strategy_name=self._strategy_name,
            payload=payload or {},
        )
        self._dispatch(event)
        return event

    def emit_event(self, event: BotEvent) -> None:
        """Опубликовать готовый BotEvent (для ReplayManager и тестов)."""
        self._dispatch(event)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, event: BotEvent) -> None:
        # 1. Синхронные sink-и (файл, ndjson) — всегда
        for sink in self._file_sinks:
            self._safe_handle(sink, event, "FileSink")

        # 2. PostgreSQL — всегда (фоновый поток, не блокирует)
        if self._postgres_sink is not None:
            self._safe_handle(self._postgres_sink, event, "PostgresSink")

        # 3. Telegram — только по правилам маршрутизации
        if self._telegram_sink is not None:
            if self._router.should_telegram(event):
                self._safe_handle(self._telegram_sink, event, "TelegramSink")

    @staticmethod
    def _safe_handle(sink: AbstractSink, event: BotEvent, sink_name: str) -> None:
        try:
            sink.handle(event)
        except Exception as exc:
            _FALLBACK.error(
                f"[{sink_name}] Ошибка при обработке события {event.event_type}: {exc}"
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Дождаться обработки всех событий в фоновых потоках."""
        for sink in self._file_sinks:
            self._safe_flush(sink, "FileSink")
        if self._telegram_sink:
            self._safe_flush(self._telegram_sink, "TelegramSink")
        if self._postgres_sink:
            self._safe_flush(self._postgres_sink, "PostgresSink")

    def close(self) -> None:
        """Корректно завершить все sink-и. Вызывать при остановке бота."""
        self.flush()
        all_sinks = (
            self._file_sinks
            + ([self._telegram_sink] if self._telegram_sink else [])
            + ([self._postgres_sink] if self._postgres_sink else [])
        )
        for sink in all_sinks:
            try:
                sink.close()
            except Exception as exc:
                _FALLBACK.error(f"Ошибка закрытия sink: {exc}")

    @staticmethod
    def _safe_flush(sink: AbstractSink, sink_name: str) -> None:
        try:
            sink.flush()
        except Exception as exc:
            _FALLBACK.error(f"[{sink_name}] Ошибка при flush: {exc}")
