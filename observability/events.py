"""
BotEvent — неизменяемый dataclass для всех событий системы.

Обязательные поля: bot_id, cycle_id, event_type, level, ts_ms.
payload используется для дополнительного контекста без изменения схемы БД.

EventType — реестр всех допустимых event_type строк.
Используется для автодополнения и документации. Строки в emitter.emit()
можно передавать напрямую — жёсткой проверки нет, чтобы не ломать
существующий код при добавлении новых событий.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Допустимые уровни (соответствуют logging)
LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class EventType:
    """
    Реестр всех event_type строк системы.

    Каждое поле = строка используемая в emitter.emit(event_type=...).
    Добавление нового события — добавить константу сюда.
    Группы соответствуют таксономии из ТЗ-3.
    """

    # -- Жизненный цикл бота ------------------------------------------
    BOT_STARTING   = "BOT_STARTING"
    BOT_STARTED    = "BOT_STARTED"
    BOT_STOPPING   = "BOT_STOPPING"
    BOT_STOPPED    = "BOT_STOPPED"
    BOT_CRASHED    = "BOT_CRASHED"
    BOT_HEARTBEAT  = "BOT_HEARTBEAT"

    # -- Рыночные данные -----------------------------------------------
    PRICE_RECEIVED             = "PRICE_RECEIVED"
    PRICE_STALE                = "PRICE_STALE"
    MARKET_WATCHER_STARTED     = "MARKET_WATCHER_STARTED"
    MARKET_WATCHER_RECONNECTED = "MARKET_WATCHER_RECONNECTED"
    MARKET_WATCHER_ERROR       = "MARKET_WATCHER_ERROR"

    # -- Торговый цикл ------------------------------------------------
    CYCLE_STARTED          = "CYCLE_STARTED"
    CYCLE_STATUS_CHANGED   = "CYCLE_STATUS_CHANGED"
    CYCLE_CLOSING_STARTED  = "CYCLE_CLOSING_STARTED"
    CYCLE_CLOSED           = "CYCLE_CLOSED"
    CYCLE_FINALIZED        = "CYCLE_FINALIZED"

    # -- Ордера -------------------------------------------------------
    ORDER_CREATE_REQUESTED = "ORDER_CREATE_REQUESTED"
    ORDER_CREATED          = "ORDER_CREATED"
    ORDER_CREATE_FAILED    = "ORDER_CREATE_FAILED"
    ORDER_CANCELLED        = "ORDER_CANCELLED"
    ORDER_FILLED           = "ORDER_FILLED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_REJECTED         = "ORDER_REJECTED"

    # -- Take-profit --------------------------------------------------
    TP_CREATED          = "TP_CREATED"
    TP_CANCEL_REQUESTED = "TP_CANCEL_REQUESTED"
    TP_REPLACE_STARTED  = "TP_REPLACE_STARTED"
    TP_REPLACE_FINISHED = "TP_REPLACE_FINISHED"
    TP_FILLED           = "TP_FILLED"
    TP_PARTIALLY_FILLED = "TP_PARTIALLY_FILLED"
    TP_CLOSE_DETECTED   = "TP_CLOSE_DETECTED"

    # -- Stop-Loss (ТЗ-7 StopLoss) ------------------------------------
    SL_TRIGGERED   = "SL_TRIGGERED"
    """
    SL условие выполнено, инициировано CLOSING.
    Level: WARNING
    Payload: sl_price, current_bid, avg_price, sl_pct, loss_pct, cycle_id
    """

    SL_CLOSE_BLOCKED = "SL_CLOSE_BLOCKED"
    """
    Проскальзывание при MARKET-закрытии по SL превысило
    SL_MAX_MARKET_SLIPPAGE_PCT → STOP_CRANE.
    Level: CRITICAL
    Payload: required_slippage_pct, max_slippage_pct, bid, avg_price,
             spread, cycle_id
    """

    # -- Сделки -------------------------------------------------------
    TRADE_DISCOVERED     = "TRADE_DISCOVERED"
    TRADE_APPLIED        = "TRADE_APPLIED"
    TRADE_ALREADY_APPLIED = "TRADE_ALREADY_APPLIED"
    FEES_APPLIED         = "FEES_APPLIED"

    # -- State / Patch ------------------------------------------------
    STATE_LOADED       = "STATE_LOADED"
    STATE_SAVED        = "STATE_SAVED"
    STATE_SAVE_FAILED  = "STATE_SAVE_FAILED"
    PATCH_PREPARED     = "PATCH_PREPARED"
    PATCH_APPLIED      = "PATCH_APPLIED"
    PATCH_REJECTED     = "PATCH_REJECTED"

    # -- STOP-CRANE / Kill-switch -------------------------------------
    STOP_CRANE_TRIGGERED  = "STOP_CRANE_TRIGGERED"
    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"

    # -- PostgreSQL ---------------------------------------------------
    PG_CONNECTED           = "PG_CONNECTED"
    PG_CONNECTION_FAILED   = "PG_CONNECTION_FAILED"
    PG_QUERY_FAILED        = "PG_QUERY_FAILED"
    PG_MIRROR_WRITE_FAILED = "PG_MIRROR_WRITE_FAILED"

    # -- Config -------------------------------------------------------
    CONFIG_LOADED             = "CONFIG_LOADED"
    CONFIG_ERROR              = "CONFIG_ERROR"
    INSUFFICIENT_FUNDS        = "INSUFFICIENT_FUNDS"

    # -- Безопасность -------------------------------------------------
    CREDENTIALS_MISSING       = "CREDENTIALS_MISSING"
    CREDENTIALS_INVALID       = "CREDENTIALS_INVALID"
    CONFIG_VALIDATION_FAILED  = "CONFIG_VALIDATION_FAILED"

    # -- Reconciliation -----------------------------------------------
    RECONCILIATION_STARTED  = "RECONCILIATION_STARTED"
    RECONCILIATION_FINISHED = "RECONCILIATION_FINISHED"
    RECONCILIATION_ERROR    = "RECONCILIATION_ERROR"


@dataclass(frozen=True)
class BotEvent:
    """
    Структурированное событие бота.

    frozen=True: события неизменяемы после создания.
    Все sink-и получают один и тот же объект — без риска мутации.
    """

    # --- Обязательные поля ---
    event_type: str
    level: str          # DEBUG / INFO / WARNING / ERROR / CRITICAL
    message: str
    bot_id: str         # Обязательно — поддерживает мультибот-сценарии
    cycle_id: str       # Обязательно — связывает события одной сделки. "" если нет цикла

    # --- Автозаполняемые поля ---
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    event_version: int = 1

    # --- Опциональные поля ---
    ticker: str = ""
    strategy_name: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(
                f"Недопустимый уровень события: '{self.level}'. "
                f"Допустимые: {sorted(LEVELS)}"
            )
        if not self.event_type:
            raise ValueError("event_type не может быть пустым")
        if not self.bot_id:
            raise ValueError("bot_id не может быть пустым")

    def to_dict(self) -> Dict[str, Any]:
        """Сериализация в словарь для JSON/NDJSON."""
        return {
            "ts_ms": self.ts_ms,
            "level": self.level,
            "event_type": self.event_type,
            "event_version": self.event_version,
            "message": self.message,
            "bot_id": self.bot_id,
            "ticker": self.ticker,
            "cycle_id": self.cycle_id,
            "strategy_name": self.strategy_name,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotEvent":
        """Десериализация из словаря (для ReplayManager)."""
        return cls(
            ts_ms=data["ts_ms"],
            level=data["level"],
            event_type=data["event_type"],
            event_version=data.get("event_version", 1),
            message=data["message"],
            bot_id=data["bot_id"],
            ticker=data.get("ticker", ""),
            cycle_id=data.get("cycle_id", ""),
            strategy_name=data.get("strategy_name", ""),
            payload=data.get("payload", {}),
        )
