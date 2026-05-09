"""
Пункт 2: Watchdog для контроля свежести рыночных данных.

Работает в отдельном потоке. Независим от частоты тиков бота — 
бот может тикать раз в минуту, а Watchdog проверяет свежесть каждые N секунд.

Логика переходов:
  fresh → stale  : вызывает on_stale()  (однократно при переходе)
  stale → fresh  : вызывает on_recovered() (однократно при переходе)

Не вызывает on_stale() повторно пока состояние не изменилось —
FallbackManager не будет получать спам.
"""

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class WatchdogTimer:
    """
    Следит за тем, чтобы рыночные данные поступали не реже чем раз в stale_threshold_sec.

    Провайдер вызывает update() при каждом успешно принятом тике.
    Если update() не вызывался дольше stale_threshold_sec — срабатывает on_stale().
    Как только update() вызван снова — срабатывает on_recovered().

    Параметры:
        stale_threshold_sec  — порог устаревания (из MarketSettings).
        on_stale             — callback при переходе fresh → stale.
                               Вызывается в потоке Watchdog, должен быть thread-safe.
        on_recovered         — callback при переходе stale → fresh. Опционален.
        check_interval_sec   — как часто Watchdog проверяет состояние.
                               Дефолт: stale_threshold_sec / 3, минимум 1 сек.
        name                 — имя потока (для отладки).

    Пример:
        watchdog = WatchdogTimer(
            stale_threshold_sec=30,
            on_stale=fallback_manager.activate_rest,
            on_recovered=fallback_manager.restore_ws,
        )
        watchdog.start()
        # При каждом WS-тике:
        watchdog.update()
        # При остановке:
        watchdog.stop()
    """

    def __init__(
        self,
        stale_threshold_sec: int,
        on_stale: Callable[[], None],
        on_recovered: Optional[Callable[[], None]] = None,
        check_interval_sec: Optional[float] = None,
        name: str = "WatchdogTimer",
    ) -> None:
        if stale_threshold_sec <= 0:
            raise ValueError(f"stale_threshold_sec должен быть > 0, получено: {stale_threshold_sec}")

        self._stale_threshold_sec = stale_threshold_sec
        self._on_stale            = on_stale
        self._on_recovered        = on_recovered
        self._check_interval_sec  = check_interval_sec or max(1.0, stale_threshold_sec / 3)
        self._name                = name

        # Состояние — защищено локом
        self._lock:           threading.Lock  = threading.Lock()
        self._last_update_ts: float           = time.monotonic()
        self._is_stale:       bool            = False

        # Управление потоком
        self._stop_event: threading.Event          = threading.Event()
        self._thread:     Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self) -> None:
        """
        Зафиксировать получение свежих данных.

        Вызывается провайдером при каждом принятом WS-тике или успешном REST-запросе.
        Thread-safe — можно вызывать из любого потока.
        """
        with self._lock:
            self._last_update_ts = time.monotonic()
            was_stale = self._is_stale
            self._is_stale = False

        # Вызываем callback вне лока — не держим лок во время внешнего кода
        if was_stale and self._on_recovered is not None:
            logger.info("WatchdogTimer [%s]: данные восстановились.", self._name)
            try:
                self._on_recovered()
            except Exception:
                logger.exception("WatchdogTimer [%s]: ошибка в on_recovered().", self._name)

    def start(self) -> None:
        """
        Запустить Watchdog в фоновом потоке.
        Идемпотентен — повторный вызов игнорируется если поток уже жив.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.debug("WatchdogTimer [%s]: уже запущен, пропускаем.", self._name)
            return

        self._stop_event.clear()

        with self._lock:
            self._last_update_ts = time.monotonic()  # сброс при старте
            self._is_stale       = False

        self._thread = threading.Thread(
            target=self._run,
            name=self._name,
            daemon=True,   # поток не блокирует завершение процесса
        )
        self._thread.start()
        logger.debug(
            "WatchdogTimer [%s]: запущен (threshold=%ds, check_interval=%.1fs).",
            self._name, self._stale_threshold_sec, self._check_interval_sec,
        )

    def stop(self, timeout: Optional[float] = None) -> None:
        """
        Остановить Watchdog.
        Ждёт завершения потока до timeout секунд (дефолт: check_interval + 1).
        Идемпотентен.
        """
        if self._thread is None:
            return

        self._stop_event.set()
        join_timeout = timeout if timeout is not None else self._check_interval_sec + 1.0
        self._thread.join(timeout=join_timeout)

        if self._thread.is_alive():
            logger.warning(
                "WatchdogTimer [%s]: поток не завершился за %.1fs.",
                self._name, join_timeout,
            )
        else:
            logger.debug("WatchdogTimer [%s]: остановлен.", self._name)

        self._thread = None

    def is_running(self) -> bool:
        """True если фоновый поток жив."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_stale(self) -> bool:
        """Текущее состояние: True если данные устарели."""
        with self._lock:
            return self._is_stale

    @property
    def age_sec(self) -> float:
        """Сколько секунд прошло с последнего update()."""
        with self._lock:
            return time.monotonic() - self._last_update_ts

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Основной цикл Watchdog — работает в фоновом потоке."""
        logger.debug("WatchdogTimer [%s]: поток стартовал.", self._name)

        # stop_event.wait() блокирует на check_interval_sec или до сигнала stop
        while not self._stop_event.wait(timeout=self._check_interval_sec):
            self._check()

        logger.debug("WatchdogTimer [%s]: поток завершается.", self._name)

    def _check(self) -> None:
        """Проверить свежесть — вызывается из фонового потока."""
        now = time.monotonic()

        with self._lock:
            age       = now - self._last_update_ts
            was_stale = self._is_stale
            is_stale  = age > self._stale_threshold_sec
            self._is_stale = is_stale

        # Переход fresh → stale (только один раз при переходе)
        if is_stale and not was_stale:
            logger.warning(
                "WatchdogTimer [%s]: данные устарели (age=%.1fs > threshold=%ds).",
                self._name, age, self._stale_threshold_sec,
            )
            try:
                self._on_stale()
            except Exception:
                logger.exception("WatchdogTimer [%s]: ошибка в on_stale().", self._name)
