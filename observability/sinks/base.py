"""AbstractSink — базовый класс для всех sink-ов."""

from __future__ import annotations
import abc


class AbstractSink(abc.ABC):
    """
    Абстрактный sink. Реализуй handle() в каждом конкретном sink-е.

    Контракт безопасности:
    - handle() НЕ должен пробрасывать исключения. EventEmitter поймает
      любое исключение и запишет в fallback logger, не останавливая бота.
    - flush() вызывается при graceful shutdown — дождаться очереди.
    - close() вызывается последним — освободить ресурсы.
    """

    @abc.abstractmethod
    def handle(self, event) -> None:
        """Обработать событие. Не пробрасывать исключения."""
        ...

    def flush(self) -> None:
        """Дождаться обработки всех событий в очереди. Базовая реализация — no-op."""
        pass

    def close(self) -> None:
        """Корректно завершить работу. Переопредели если нужно закрыть ресурсы."""
        pass
