"""
BotEvent — неизменяемый dataclass для всех событий системы.

Обязательные поля: bot_id, cycle_id, event_type, level, ts_ms.
payload используется для дополнительного контекста без изменения схемы БД.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Допустимые уровни (соответствуют logging)
LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


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
