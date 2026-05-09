"""
Пункт 2: Управление переключением источников рыночных данных.

FallbackManager отвечает за:
  - Переключение WS → REST когда WatchdogTimer фиксирует устаревание данных.
  - Переподключение WS с экспоненциальным backoff + jitter.
  - Teardown зомби-сокетов перед каждой попыткой реконнекта.
  - Возврат WS → основной источник после восстановления.

Реальные операции WS (connect/disconnect) инжектируются как callable —
FallbackManager не знает про Bybit, pybit или любую конкретную библиотеку.

Схема переходов:
  CONNECTED   --[stale/ws_error]--> RECONNECTING --> CONNECTED
                                                 --> FALLBACK_REST --> CONNECTED
                                                 --> FAILED
"""

import logging
import random
import threading
import time
from typing import Callable, Optional

from market_data.market_data import ProviderStatus

logger = logging.getLogger(__name__)


class FallbackManager:
    """
    Управляет переключением между WebSocket и REST и процессом реконнекта.

    Параметры:
        reconnect_delay_sec  — начальная задержка реконнекта (из MarketSettings).
        max_reconnect_sec    — максимальная задержка реконнекта (из MarketSettings).
        ws_teardown          — закрыть текущее WS-соединение (может быть мёртвым).
                               Должен быть идемпотентным и не бросать исключений.
        ws_connect           — открыть новое WS-соединение.
                               Бросает исключение при неудаче.
        on_status_change     — callback для уведомления провайдера о смене статуса.
                               Вызывается в потоке FallbackManager, должен быть thread-safe.
        max_reconnect_attempts — None = бесконечно. После исчерпания → FAILED.

    Пример подключения к WatchdogTimer:
        fallback = FallbackManager(
            reconnect_delay_sec=1.0,
            max_reconnect_sec=30.0,
            ws_teardown=bybit_ws.close,
            ws_connect=bybit_ws.connect,
            on_status_change=provider._set_status,
        )
        watchdog = WatchdogTimer(
            stale_threshold_sec=30,
            on_stale=fallback.handle_stale,
            on_recovered=fallback.handle_ws_recovered,
        )
    """

    def __init__(
        self,
        reconnect_delay_sec: float,
        max_reconnect_sec: float,
        ws_teardown: Callable[[], None],
        ws_connect: Callable[[], None],
        on_status_change: Callable[[ProviderStatus], None],
        max_reconnect_attempts: Optional[int] = None,
    ) -> None:
        if reconnect_delay_sec <= 0:
            raise ValueError(f"reconnect_delay_sec должен быть > 0, получено: {reconnect_delay_sec}")
        if max_reconnect_sec < reconnect_delay_sec:
            raise ValueError(
                f"max_reconnect_sec ({max_reconnect_sec}) "
                f"должен быть >= reconnect_delay_sec ({reconnect_delay_sec})"
            )

        self._reconnect_delay_sec     = reconnect_delay_sec
        self._max_reconnect_sec       = max_reconnect_sec
        self._ws_teardown             = ws_teardown
        self._ws_connect              = ws_connect
        self._on_status_change        = on_status_change
        self._max_reconnect_attempts  = max_reconnect_attempts

        self._lock:          threading.Lock            = threading.Lock()
        self._stop_event:    threading.Event           = threading.Event()
        self._thread:        Optional[threading.Thread] = None
        self._current_status: ProviderStatus           = ProviderStatus.CONNECTED

    # ------------------------------------------------------------------
    # Public API — вызывается из WatchdogTimer callbacks
    # ------------------------------------------------------------------

    def handle_stale(self) -> None:
        """
        WatchdogTimer зафиксировал устаревание данных (WS завис но не упал).
        Запускаем реконнект-цикл в фоне. До восстановления WS — провайдер
        переходит в FALLBACK_REST.
        """
        logger.warning("FallbackManager: данные устарели → запускаем реконнект.")
        self._set_status(ProviderStatus.STALE)
        self._start_reconnect_loop()

    def handle_ws_error(self) -> None:
        """
        WS упал явно (исключение при чтении, соединение разорвано).
        Немедленно запускаем реконнект.
        """
        logger.warning("FallbackManager: WS-ошибка → запускаем реконнект.")
        self._set_status(ProviderStatus.RECONNECTING)
        self._start_reconnect_loop()

    def handle_ws_recovered(self) -> None:
        """
        WatchdogTimer зафиксировал что данные снова поступают (WS восстановился сам).
        Останавливаем реконнект-цикл если он ещё идёт.
        """
        logger.info("FallbackManager: WS восстановился → останавливаем реконнект.")
        self._stop_reconnect_loop()
        self._set_status(ProviderStatus.CONNECTED)

    def stop(self) -> None:
        """Корректно остановить FallbackManager при завершении провайдера."""
        self._stop_reconnect_loop()
        logger.debug("FallbackManager: остановлен.")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> ProviderStatus:
        with self._lock:
            return self._current_status

    def set_connected(self) -> None:
        """Явно пометить как CONNECTED — вызывается провайдером при успешном старте."""
        self._set_status(ProviderStatus.CONNECTED)

    # ------------------------------------------------------------------
    # Reconnect loop
    # ------------------------------------------------------------------

    def _start_reconnect_loop(self) -> None:
        """Запустить реконнект-цикл в фоновом потоке. Идемпотентен."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.debug("FallbackManager: реконнект уже идёт, пропускаем.")
                return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._reconnect_loop,
            name="FallbackManager-Reconnect",
            daemon=True,
        )
        self._thread.start()

    def _stop_reconnect_loop(self) -> None:
        """Остановить реконнект-цикл."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._max_reconnect_sec + 1.0)
            self._thread = None

    def _reconnect_loop(self) -> None:
        """
        Основной цикл реконнекта — работает в фоновом потоке.

        Алгоритм:
          1. Teardown зомби-сокета (всегда, даже если WS уже мёртв).
          2. Переключиться на REST (провайдер продолжает работать через REST пока мы реконнектимся).
          3. Ждать с экспоненциальной задержкой + jitter.
          4. Попытаться подключить WS.
          5. При успехе → CONNECTED, выход.
          6. При неудаче → инкрементировать попытку, повторить.
          7. После max_reconnect_attempts → FAILED.
        """
        logger.info("FallbackManager: запуск реконнект-цикла.")
        attempt = 0

        while not self._stop_event.is_set():
            # Шаг 1: teardown зомби-сокета
            self._safe_teardown()

            # Шаг 2: переключить на REST пока реконнектимся
            if self.status != ProviderStatus.FALLBACK_REST:
                self._set_status(ProviderStatus.FALLBACK_REST)

            # Шаг 3: задержка перед следующей попыткой
            delay = self._calc_delay(attempt)
            logger.info(
                "FallbackManager: попытка %d — ждём %.1fs перед реконнектом.",
                attempt + 1, delay,
            )
            if self._stop_event.wait(timeout=delay):
                logger.debug("FallbackManager: реконнект прерван сигналом stop.")
                return

            # Шаг 4: попытка реконнекта
            try:
                logger.info("FallbackManager: попытка %d — подключаем WS.", attempt + 1)
                self._ws_connect()
                # Успех
                logger.info("FallbackManager: WS подключён успешно (попытка %d).", attempt + 1)
                self._set_status(ProviderStatus.CONNECTED)
                return

            except Exception as exc:
                attempt += 1
                logger.warning(
                    "FallbackManager: попытка %d не удалась: %s.",
                    attempt, exc,
                )

                # Шаг 7: исчерпали попытки
                if (
                    self._max_reconnect_attempts is not None
                    and attempt >= self._max_reconnect_attempts
                ):
                    logger.error(
                        "FallbackManager: исчерпаны все %d попыток реконнекта → FAILED.",
                        self._max_reconnect_attempts,
                    )
                    self._set_status(ProviderStatus.FAILED)
                    return

        logger.debug("FallbackManager: реконнект-цикл завершён по stop_event.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calc_delay(self, attempt: int) -> float:
        """
        Экспоненциальная задержка с jitter.

        По ТЗ: delay = min(base * 2^n + random(0, delay * 0.3), max_reconnect_sec)

        attempt=0 → base * 1  + jitter
        attempt=1 → base * 2  + jitter
        attempt=2 → base * 4  + jitter
        ...
        """
        base_delay = self._reconnect_delay_sec * (2 ** attempt)
        jitter     = random.uniform(0, base_delay * 0.3)
        delay      = min(base_delay + jitter, self._max_reconnect_sec)
        return delay

    def _safe_teardown(self) -> None:
        """
        Teardown зомби-сокета — не бросает исключений.

        Python WS-библиотеки часто оставляют мёртвые TCP-соединения.
        Без явного teardown перед реконнектом сервер исчерпает файловые дескрипторы.
        """
        try:
            self._ws_teardown()
            logger.debug("FallbackManager: teardown WS выполнен.")
        except Exception as exc:
            # Teardown мёртвого сокета может бросить — это нормально
            logger.debug("FallbackManager: teardown завершился с ошибкой (ожидаемо): %s", exc)

    def _set_status(self, status: ProviderStatus) -> None:
        """Обновить статус и уведомить провайдера через callback."""
        with self._lock:
            if self._current_status == status:
                return
            old_status = self._current_status
            self._current_status = status

        logger.info(
            "FallbackManager: статус %s → %s.",
            old_status.value, status.value,
        )
        try:
            self._on_status_change(status)
        except Exception:
            logger.exception("FallbackManager: ошибка в on_status_change().")
