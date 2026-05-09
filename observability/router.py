"""
NotificationRouter — правила маршрутизации событий.

Определяет какие события идут в Telegram, какие только в файл.
Следует таблице из TZ_3.
"""

from __future__ import annotations

from observability.events import BotEvent

# События которые отправляются в Telegram
_TELEGRAM_EVENT_TYPES = frozenset({
    # Жизненный цикл
    "BOT_STARTED", "BOT_STOPPED", "BOT_CRASHED",
    # Торговые циклы
    "CYCLE_STARTED", "CYCLE_CLOSED",
    # Ордера
    "ORDER_CREATED", "ORDER_FILLED", "ORDER_CREATE_FAILED", "ORDER_REJECTED",
    "ORDER_CANCELLED",
    # Take-profit
    "TP_CREATED", "TP_FILLED",
    # Аварийные
    "STOP_CRANE_TRIGGERED", "KILL_SWITCH_TRIGGERED",
    # Инфраструктура
    "CREDENTIALS_MISSING", "CREDENTIALS_INVALID",
    "STATE_SAVE_FAILED",
    "PG_CONNECTION_FAILED",
    # Конфиг
    "CONFIG_ERROR",
    # Баланс
    "INSUFFICIENT_FUNDS",
    # Reconciliation
    "RECONCILIATION_ERROR",
})

# События которые НИКОГДА не идут в Telegram (только в файл)
_FILE_ONLY_EVENT_TYPES = frozenset({
    "PRICE_RECEIVED",
    "BOT_HEARTBEAT",
    "TRADE_ALREADY_APPLIED",
    "PATCH_PREPARED",
    "PATCH_VALIDATED",
    "STATE_SAVED",
})

# Уровни которые принудительно идут в Telegram независимо от типа
_FORCE_TELEGRAM_LEVELS = frozenset({"CRITICAL"})


class NotificationRouter:
    """
    Определяет нужно ли отправить событие в Telegram.

    Логика приоритетов:
    1. CRITICAL уровень → всегда в Telegram.
    2. Событие в _FILE_ONLY_EVENT_TYPES → только в файл.
    3. Событие в _TELEGRAM_EVENT_TYPES → в Telegram.
    4. Остальное → только в файл.
    """

    @staticmethod
    def should_telegram(event: BotEvent) -> bool:
        if event.level in _FORCE_TELEGRAM_LEVELS:
            return True
        if event.event_type in _FILE_ONLY_EVENT_TYPES:
            return False
        return event.event_type in _TELEGRAM_EVENT_TYPES

    @staticmethod
    def should_console(event: BotEvent) -> bool:
        """WARNING и выше идут в консоль."""
        return event.level in {"WARNING", "ERROR", "CRITICAL"}
