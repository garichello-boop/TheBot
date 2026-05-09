"""
RetryManager — расписание retry для WAITING_FOR_LIQUIDITY.

Расписание из ТЗ 7:
  Попытка 1: пауза 1 мин  → следующая через 5 мин
  Попытка 2: пауза 5 мин  → следующая через 15 мин
  Попытка 3: пауза 15 мин → следующая через 60 мин
  Попытка 4: пауза 60 мин → следующая через 15 мин
  Попытка 5+: каждые 15 мин

Attempt индексируется с 0 (bot_state.dca_count используется как счётчик
попыток WAITING_FOR_LIQUIDITY — dca_count не инкрементируется при
неудачной попытке, только при успешном DCA).
"""
from __future__ import annotations

# Расписание задержек в секундах: индекс = номер попытки (0-based)
_RETRY_SCHEDULE_SEC: list[int] = [
    60,    # попытка 1: 1 мин
    300,   # попытка 2: 5 мин
    900,   # попытка 3: 15 мин
    3600,  # попытка 4: 60 мин
]
_RETRY_FALLBACK_SEC = 900  # попытки 5+: 15 мин


class RetryManager:
    """
    Вычисляет задержку перед следующей попыткой ордера
    в состоянии WAITING_FOR_LIQUIDITY.
    """

    @staticmethod
    def get_delay_for_attempt(attempt: int) -> int:
        """
        Вернуть задержку в секундах для данной попытки.

        Args:
          attempt — номер попытки (0-based). Берётся из счётчика retry
                    в bot_state (не путать с dca_count).

        Returns:
          Количество секунд ожидания перед следующей попыткой.
        """
        if attempt < len(_RETRY_SCHEDULE_SEC):
            return _RETRY_SCHEDULE_SEC[attempt]
        return _RETRY_FALLBACK_SEC

    @staticmethod
    def format_next_attempt_message(attempt: int) -> str:
        """Сообщение для Telegram при WAITING_FOR_LIQUIDITY."""
        delay = RetryManager.get_delay_for_attempt(attempt)
        delay_min = delay // 60

        if delay_min >= 60:
            delay_str = f"{delay_min // 60} ч"
        else:
            delay_str = f"{delay_min} мин"

        return (
            f"Недостаточно средств. "
            f"Попытка {attempt + 1}. "
            f"Следующая через {delay_str}."
        )
