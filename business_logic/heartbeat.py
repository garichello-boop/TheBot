"""
HeartbeatEmitter — пульс бота.

Из ТЗ 7:
  Каждые HEARTBEAT_INTERVAL_TICKS тиков:
    - Обновляет bot_registry.last_heartbeat в PostgreSQL.
    - Эмитит BOT_HEARTBEAT (DEBUG).

  При остановке: bot_registry.operational_status → STOPPED.
  При аварии:    bot_registry.operational_status → ERROR.

Внешний монитор или веб-интерфейс использует last_heartbeat чтобы
определить живой бот или нет.

Зависимости:
  RegistryRepository.update_heartbeat(user_id, bot_id) — из П6.
  EventEmitter из П3.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_state import RegistryRepository
    from observability import EventEmitter
    from .tick_context import TickContext

logger = logging.getLogger(__name__)


class HeartbeatEmitter:
    """
    Управляет обновлением heartbeat в bot_registry.

    Инициализируется при старте бота. Вызывается из tick-loop.
    """

    def __init__(
        self,
        registry_repo: "RegistryRepository",
        emitter: "EventEmitter",
        *,
        interval_ticks: int,
        bot_id: str,
        user_id: str,
    ) -> None:
        self._registry_repo = registry_repo
        self._emitter       = emitter
        self._interval      = interval_ticks
        self._bot_id        = bot_id
        self._user_id       = user_id

    def maybe_emit(self, ctx: "TickContext") -> None:
        """
        Вызывается в конце каждого тика.
        Обновляет БД и эмитит событие каждые interval_ticks тиков.
        """
        if ctx.tick_number % self._interval != 0:
            return

        try:
            self._registry_repo.update_heartbeat(self._user_id, self._bot_id)
        except Exception as exc:
            # Ошибка heartbeat — не останавливаем бота, только логируем.
            # Внешний монитор обнаружит протухший heartbeat самостоятельно.
            logger.warning("Не удалось обновить heartbeat: %s", exc)

        self._emitter.emit(
            event_type="BOT_HEARTBEAT",
            level="DEBUG",
            message="Heartbeat",
            payload={
                "fsm_state":   ctx.cycle_status,
                "tick_number": ctx.tick_number,
                "bot_id":      self._bot_id,
            },
        )

    def mark_stopped(self) -> None:
        """При корректной остановке бота."""
        try:
            self._registry_repo.update_status(
                self._user_id, self._bot_id, status="STOPPED"
            )
        except Exception as exc:
            logger.warning("Не удалось обновить статус STOPPED: %s", exc)

    def mark_error(self, message: str) -> None:
        """При аварийной остановке."""
        try:
            self._registry_repo.update_status(
                self._user_id, self._bot_id,
                status="ERROR",
                error_message=message,
            )
        except Exception as exc:
            logger.warning("Не удалось обновить статус ERROR: %s", exc)
