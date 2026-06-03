"""
Observability — подсистема наблюдаемости торгового бота.

Точка входа: setup_observability() создаёт и возвращает настроенный EventEmitter.

Пример использования (bot.py):
    from observability import setup_observability

    emitter = setup_observability(
        settings=settings,       # AppSettings — все параметры берутся отсюда
        bot_id=bot_id,
        ticker=ticker,
        tg_token=km.get("TG_BOT_TOKEN"),    # опционально
        tg_chat_id=km.get("TG_CHAT_ID"),    # опционально
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
    settings,                           # AppSettings — без аннотации, избегаем циклического импорта
    bot_id: str,
    ticker: str = "",
    strategy_name: str = "",
    tg_token: Optional[str] = None,     # km.get("TG_BOT_TOKEN") из bot.py
    tg_chat_id: Optional[str] = None,   # km.get("TG_CHAT_ID") из bot.py
) -> EventEmitter:
    """
    Создать и настроенный EventEmitter, извлекая параметры из AppSettings.

    Параметры Telegram и PostgreSQL опциональны:
    - Telegram включается если переданы tg_token + tg_chat_id
      и settings.telegram.mode != OFF.
    - PostgreSQL включается если settings.database.password задан
      (предполагается что конфигурация БД означает желание писать события).

    Все параметры логирования, rate-limiting Telegram и ротации файлов
    берутся из settings.logging и settings.telegram.
    """
    # --- Извлечь параметры из AppSettings ---
    log_folder       = settings.logging.folder
    log_level        = settings.logging.level.value
    log_max_bytes    = settings.logging.max_bytes
    log_backup_count = settings.logging.backup_count
    tg_mode          = settings.telegram.mode.value        # ALL / IMPORTANT / OFF
    tg_max_per_min   = settings.telegram.max_per_minute
    tg_dedup_sec     = settings.telegram.dedup_window_sec

    # PostgreSQL: используем URL из settings.database если пароль задан
    db = settings.database
    postgres_dsn = db.url if db.password else None

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
    if tg_mode != "OFF" and tg_token and tg_chat_id:
        tg_sink = TelegramSink(
            token=tg_token,
            chat_id=tg_chat_id,
            max_per_minute=tg_max_per_min,
            dedup_window_sec=tg_dedup_sec,
        )
        emitter.set_telegram_sink(tg_sink)

    # --- PostgresSink ---
    if postgres_dsn:
        pg_sink = PostgresSink(
            dsn=postgres_dsn,
            batch_size=50,
            flush_interval_sec=5.0,
        )
        emitter.set_postgres_sink(pg_sink)

    return emitter


def _setup_console_handler(min_level: str) -> None:
    """Настроить вывод WARNING+ в консоль через стандартный logging."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr:
            return

    handler = logging.StreamHandler(
        stream=open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)
    )
    handler.setLevel(getattr(logging, min_level.upper(), logging.WARNING))
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
