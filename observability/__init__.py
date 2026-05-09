"""
Observability — подсистема наблюдаемости торгового бота.

Точка входа: setup_observability() создаёт и возвращает настроенный EventEmitter.

Пример использования:
    from observability import setup_observability

    emitter = setup_observability(
        bot_id="phor_dca",
        ticker="PHOR",
        strategy_name="MeanReversion",
        log_folder="logs/",
        log_level="INFO",
        telegram_token="...",      # опционально
        telegram_chat_id="...",    # опционально
        telegram_mode="IMPORTANT", # ALL / IMPORTANT / OFF
        postgres_dsn="...",        # опционально
    )

    emitter.emit(event_type="BOT_STARTED", level="INFO", message="Бот запущен")
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from observability.emitter import EventEmitter
from observability.events import BotEvent
from observability.replay import ReplayManager
from observability.router import NotificationRouter
from observability.sinks.file_sink import FileLogSink
from observability.sinks.ndjson_sink import NdjsonSink
from observability.sinks.postgres_sink import PostgresSink
from observability.sinks.telegram_sink import TelegramSink

__all__ = [
    "setup_observability",
    "EventEmitter",
    "BotEvent",
    "ReplayManager",
    "NotificationRouter",
    "FileLogSink",
    "NdjsonSink",
    "TelegramSink",
    "PostgresSink",
]


def setup_observability(
    bot_id: str,
    ticker: str = "",
    strategy_name: str = "",
    # Logging
    log_folder: str = "logs/",
    log_level: str = "INFO",
    log_max_bytes: int = 10 * 1024 * 1024,
    log_backup_count: int = 10,
    # Telegram
    telegram_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    telegram_mode: str = "IMPORTANT",  # ALL / IMPORTANT / OFF
    telegram_max_per_minute: int = 20,
    telegram_dedup_window_sec: int = 300,
    # PostgreSQL
    postgres_dsn: Optional[str] = None,
    postgres_batch_size: int = 50,
    postgres_flush_interval_sec: float = 5.0,
) -> EventEmitter:
    """
    Создать и настроить EventEmitter со всеми sink-ами.

    Минимальная конфигурация: только bot_id.
    Telegram и PostgreSQL — опциональны, подключаются если переданы credentials.
    """
    os.makedirs(log_folder, exist_ok=True)

    emitter = EventEmitter(
        bot_id=bot_id,
        ticker=ticker,
        strategy_name=strategy_name,
    )

    # --- FileLogSink ---
    file_sink = FileLogSink(
        log_path=os.path.join(log_folder, "bot.log"),
        max_bytes=log_max_bytes,
        backup_count=log_backup_count,
        min_level=log_level,
    )
    emitter.add_file_sink(file_sink)

    # --- NdjsonSink ---
    ndjson_sink = NdjsonSink(
        events_path=os.path.join(log_folder, "events.ndjson"),
        errors_path=os.path.join(log_folder, "errors.ndjson"),
        events_max_bytes=50 * 1024 * 1024,
        events_backup_count=20,
        errors_max_bytes=10 * 1024 * 1024,
        errors_backup_count=10,
    )
    emitter.add_file_sink(ndjson_sink)

    # --- Консоль (WARNING и выше) ---
    _setup_console_handler(log_level)

    # --- TelegramSink ---
    if telegram_mode.upper() != "OFF" and telegram_token and telegram_chat_id:
        tg_sink = TelegramSink(
            token=telegram_token,
            chat_id=telegram_chat_id,
            max_per_minute=telegram_max_per_minute,
            dedup_window_sec=telegram_dedup_window_sec,
        )
        emitter.set_telegram_sink(tg_sink)

    # --- PostgresSink ---
    if postgres_dsn:
        pg_sink = PostgresSink(
            dsn=postgres_dsn,
            batch_size=postgres_batch_size,
            flush_interval_sec=postgres_flush_interval_sec,
        )
        emitter.set_postgres_sink(pg_sink)

    return emitter


def _setup_console_handler(min_level: str) -> None:
    """Настроить вывод WARNING+ в консоль через стандартный logging."""
    root = logging.getLogger()
    # Не добавлять дублирующий handler
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr:
            return

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(getattr(logging, min_level.upper(), logging.WARNING))
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
