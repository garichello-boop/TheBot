"""
Иерархия исключений для бизнес-логики бота (Пункт 7).

Правила обработки в BotLoop:
  CriticalError        → BOT_CRASHED → остановка цикла.
  RecoverableError     → TICK_FAILED → следующий тик.
                         При N подряд → KillSwitchError.
  StopCraneError       → STOP_CRANE_TRIGGERED → блокировка до ручного резолва.
  KillSwitchError      → KILL_SWITCH_TRIGGERED → остановка. Возобновление вручную.
  InsufficientFundsError → FSM → WAITING_FOR_LIQUIDITY → retry-расписание.
  ReconciliationError  → торговля блокируется до согласования с биржей.
  TickSkippedError     → тик пропущен по бизнес-причине (не инкрементирует счётчик ошибок).
"""
from __future__ import annotations


class BotError(Exception):
    """Базовый класс всех ошибок бота."""


# ---------------------------------------------------------------------------
# Критические ошибки — бот останавливается
# ---------------------------------------------------------------------------


class CriticalError(BotError):
    """
    Критическая ошибка — бот останавливается немедленно.
    BotLoop перехватывает, эмитит BOT_CRASHED и выходит из цикла.
    """


class StopCraneError(CriticalError):
    """
    STOP-CRANE: неизвестный исход ордера или нарушение инварианта состояния.

    Срабатывает в двух случаях:
      1. Ордер отправлен на биржу, результат неизвестен (timeout create_order).
      2. active_order_id не найден на бирже и статус не CANCELLED.

    После срабатывания торговля блокируется. Возобновление только вручную
    через оператора: статус ACTIVE в bot_configs.

    Все три поля payload обязательны — без них разбор инцидента занимает часы.
    """

    def __init__(
        self,
        message: str,
        *,
        invariant: str,
        expected: dict,
        actually_found: dict | None,
        db_state: dict,
    ) -> None:
        super().__init__(message)
        self.invariant = invariant
        self.expected = expected
        self.actually_found = actually_found
        self.db_state = db_state

    def to_payload(self) -> dict:
        return {
            "invariant": self.invariant,
            "expected_on_exchange": self.expected,
            "actually_found": self.actually_found,
            "db_state": self.db_state,
        }


class KillSwitchError(CriticalError):
    """
    Kill-switch: паттерн критических ошибок превысил CRITICAL_ERROR_THRESHOLD,
    или неразрешимый рассинхрон между bot_state и биржей.

    Возобновление только вручную: status=ACTIVE в bot_configs.
    """

    def __init__(self, message: str, *, reason: str, error_count: int) -> None:
        super().__init__(message)
        self.reason = reason
        self.error_count = error_count

    def to_payload(self) -> dict:
        return {
            "reason": self.reason,
            "error_count": self.error_count,
        }


# ---------------------------------------------------------------------------
# Восстанавливаемые ошибки — тик пропускается
# ---------------------------------------------------------------------------


class RecoverableError(BotError):
    """
    Восстанавливаемая ошибка — текущий тик пропускается.
    BotLoop перехватывает, эмитит TICK_FAILED и начинает следующий тик.
    При CRITICAL_ERROR_THRESHOLD подряд → KillSwitchError.
    """


class InsufficientFundsError(RecoverableError):
    """
    Биржа отклонила ордер из-за нехватки средств.

    Отличается от сетевых ошибок — только при этом исключении
    BotLoop переводит FSM в WAITING_FOR_LIQUIDITY с retry-расписанием
    (1 / 5 / 15 / 60 / 15+ мин).
    """

    def __init__(
        self,
        message: str,
        *,
        required: str,
        available: str,
    ) -> None:
        super().__init__(message)
        self.required = required
        self.available = available


class ReconciliationError(RecoverableError):
    """
    Расхождение между bot_state и биржей обнаружено при reconciliation.

    DecisionEngine не вызывается до разрешения конфликта.
    При превышении RECONCILIATION_TIMEOUT_SEC → StopCraneError.
    """

    def __init__(
        self,
        message: str,
        *,
        conflict_type: str,
        db_value: str,
        exchange_value: str,
    ) -> None:
        super().__init__(message)
        self.conflict_type = conflict_type
        self.db_value = db_value
        self.exchange_value = exchange_value


class TickSkippedError(RecoverableError):
    """
    Тик пропущен по бизнес-причине (не техническая ошибка).

    Примеры:
      - Цена ушла дальше MAX_ENTRY_SLIPPAGE_PCT — вход пропущен.
      - Отрицательная дельта позиции заблокирована DecisionEngine.
      - Бот в STOP_CRANE ждёт ручного резолва.

    Не инкрементирует счётчик consecutive_errors в BotLoop.
    """
