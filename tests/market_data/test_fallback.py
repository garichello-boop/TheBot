"""
Тесты: fallback.py — FallbackManager.

ws_connect и ws_teardown инжектируются как MagicMock — без реального WS.
Используем очень короткие задержки чтобы тесты не висели.
"""

import threading
import time
from unittest.mock import MagicMock, call

import pytest

from market_data.fallback import FallbackManager
from market_data.market_data import ProviderStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RECONNECT_DELAY = 0.01   # секунды
MAX_RECONNECT   = 0.05
WAIT_TIMEOUT    = 3.0


def make_fallback(
    ws_connect=None,
    ws_teardown=None,
    on_status_change=None,
    max_attempts=None,
) -> FallbackManager:
    return FallbackManager(
        reconnect_delay_sec=RECONNECT_DELAY,
        max_reconnect_sec=MAX_RECONNECT,
        ws_teardown=ws_teardown or MagicMock(),
        ws_connect=ws_connect or MagicMock(),
        on_status_change=on_status_change or MagicMock(),
        max_reconnect_attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------

class TestInit:
    def test_initial_status_connected(self):
        fm = make_fallback()
        assert fm.status == ProviderStatus.CONNECTED

    def test_invalid_delay_raises(self):
        with pytest.raises(ValueError, match="reconnect_delay_sec"):
            FallbackManager(
                reconnect_delay_sec=0,
                max_reconnect_sec=10,
                ws_teardown=MagicMock(),
                ws_connect=MagicMock(),
                on_status_change=MagicMock(),
            )

    def test_max_less_than_delay_raises(self):
        with pytest.raises(ValueError, match="max_reconnect_sec"):
            FallbackManager(
                reconnect_delay_sec=10,
                max_reconnect_sec=1,
                ws_teardown=MagicMock(),
                ws_connect=MagicMock(),
                on_status_change=MagicMock(),
            )


# ---------------------------------------------------------------------------
# handle_stale → реконнект
# ---------------------------------------------------------------------------

class TestHandleStale:
    def test_handle_stale_triggers_reconnect(self):
        """handle_stale запускает реконнект-цикл и успешно переподключается."""
        status_changes = []
        connected_event = threading.Event()

        def on_status(s):
            status_changes.append(s)
            if s == ProviderStatus.CONNECTED:
                connected_event.set()

        ws_connect = MagicMock()  # успешное подключение
        fm = make_fallback(ws_connect=ws_connect, on_status_change=on_status)

        fm.handle_stale()
        try:
            connected = connected_event.wait(timeout=WAIT_TIMEOUT)
            assert connected, "CONNECTED не был достигнут"
            assert ws_connect.called
        finally:
            fm.stop()

    def test_handle_stale_passes_through_fallback_rest(self):
        """Во время реконнекта статус переходит в FALLBACK_REST."""
        status_changes = []
        connected_event = threading.Event()

        def on_status(s):
            status_changes.append(s)
            if s == ProviderStatus.CONNECTED:
                connected_event.set()

        fm = make_fallback(on_status_change=on_status)
        fm.handle_stale()
        try:
            connected_event.wait(timeout=WAIT_TIMEOUT)
            assert ProviderStatus.FALLBACK_REST in status_changes
        finally:
            fm.stop()

    def test_handle_stale_idempotent(self):
        """Двойной вызов handle_stale не создаёт два потока реконнекта."""
        connect_count = [0]
        lock          = threading.Lock()
        connected_event = threading.Event()

        def ws_connect():
            with lock:
                connect_count[0] += 1
            connected_event.set()

        fm = make_fallback(ws_connect=ws_connect)
        fm.handle_stale()
        fm.handle_stale()  # второй вызов — игнорируется
        try:
            connected_event.wait(timeout=WAIT_TIMEOUT)
            time.sleep(0.1)  # дать время если бы был второй поток
            with lock:
                assert connect_count[0] == 1, f"ws_connect вызван {connect_count[0]} раз"
        finally:
            fm.stop()


# ---------------------------------------------------------------------------
# Реконнект с ошибками
# ---------------------------------------------------------------------------

class TestReconnectRetries:
    def test_retries_on_ws_connect_failure(self):
        """При ошибке ws_connect — делает retry."""
        attempt_count = [0]
        connected_event = threading.Event()

        def ws_connect():
            attempt_count[0] += 1
            if attempt_count[0] < 3:
                raise ConnectionError("временная ошибка")
            connected_event.set()

        fm = make_fallback(ws_connect=ws_connect)
        fm.handle_stale()
        try:
            connected = connected_event.wait(timeout=WAIT_TIMEOUT)
            assert connected, "Реконнект не завершился успехом"
            assert attempt_count[0] == 3
        finally:
            fm.stop()

    def test_failed_after_max_attempts(self):
        """После max_reconnect_attempts попыток → статус FAILED."""
        failed_event = threading.Event()
        status_changes = []

        def on_status(s):
            status_changes.append(s)
            if s == ProviderStatus.FAILED:
                failed_event.set()

        ws_connect = MagicMock(side_effect=ConnectionError("постоянная ошибка"))
        fm = make_fallback(
            ws_connect=ws_connect,
            on_status_change=on_status,
            max_attempts=3,
        )

        fm.handle_stale()
        try:
            failed = failed_event.wait(timeout=WAIT_TIMEOUT)
            assert failed, "FAILED не был достигнут"
            assert ws_connect.call_count == 3
        finally:
            fm.stop()


# ---------------------------------------------------------------------------
# handle_ws_recovered
# ---------------------------------------------------------------------------

class TestHandleWsRecovered:
    def test_recovered_sets_connected(self):
        status_changes = []
        fm = make_fallback(on_status_change=lambda s: status_changes.append(s))
        fm.set_connected()  # начальный статус
        fm.handle_ws_recovered()
        time.sleep(0.05)
        # Статус должен быть CONNECTED (или уже был)
        assert fm.status == ProviderStatus.CONNECTED

    def test_recovered_stops_reconnect_loop(self):
        """handle_ws_recovered прерывает реконнект-цикл."""
        connect_calls = [0]
        lock = threading.Lock()

        def slow_connect():
            time.sleep(0.05)
            with lock:
                connect_calls[0] += 1

        fm = make_fallback(ws_connect=slow_connect, max_attempts=100)
        fm.handle_stale()
        time.sleep(0.02)  # дать время начаться реконнекту

        fm.handle_ws_recovered()
        time.sleep(0.1)   # дать время остановиться

        with lock:
            calls_after_recovery = connect_calls[0]

        # После recovery реконнект должен остановиться
        time.sleep(0.1)
        with lock:
            assert connect_calls[0] == calls_after_recovery, \
                "ws_connect вызывался после handle_ws_recovered"


# ---------------------------------------------------------------------------
# Teardown зомби-сокетов
# ---------------------------------------------------------------------------

class TestTeardown:
    def test_ws_teardown_called_before_connect(self):
        """teardown вызывается перед каждой попыткой connect."""
        call_order = []
        teardown   = MagicMock(side_effect=lambda: call_order.append("teardown"))
        connected  = threading.Event()

        def ws_connect():
            call_order.append("connect")
            connected.set()

        fm = make_fallback(ws_connect=ws_connect, ws_teardown=teardown)
        fm.handle_stale()
        try:
            connected.wait(timeout=WAIT_TIMEOUT)
            assert call_order[0] == "teardown", f"Первый вызов должен быть teardown, не {call_order[0]}"
        finally:
            fm.stop()

    def test_teardown_exception_does_not_stop_reconnect(self):
        """Исключение в teardown не останавливает реконнект."""
        teardown = MagicMock(side_effect=RuntimeError("сокет уже мёртв"))
        connected_event = threading.Event()

        fm = make_fallback(
            ws_connect=lambda: connected_event.set(),
            ws_teardown=teardown,
        )
        fm.handle_stale()
        try:
            connected = connected_event.wait(timeout=WAIT_TIMEOUT)
            assert connected, "Реконнект должен продолжаться несмотря на ошибку teardown"
        finally:
            fm.stop()


# ---------------------------------------------------------------------------
# set_connected / stop
# ---------------------------------------------------------------------------

class TestSetConnectedAndStop:
    def test_set_connected_updates_status(self):
        status_changes = []
        fm = make_fallback(on_status_change=lambda s: status_changes.append(s))
        # Сначала меняем статус
        fm._set_status(ProviderStatus.FALLBACK_REST)
        fm.set_connected()
        assert fm.status == ProviderStatus.CONNECTED

    def test_stop_is_idempotent(self):
        fm = make_fallback()
        fm.stop()
        fm.stop()  # не должно падать

    def test_stop_interrupts_reconnect(self):
        """stop() прерывает ожидающий реконнект немедленно."""
        connect_calls = [0]

        def slow_connect():
            connect_calls[0] += 1
            raise ConnectionError("ошибка")

        fm = make_fallback(
            ws_connect=slow_connect,
            max_attempts=100,
        )
        fm.handle_stale()
        time.sleep(0.05)

        start = time.monotonic()
        fm.stop()
        elapsed = time.monotonic() - start

        # stop() не должен ждать MAX_RECONNECT_SEC
        assert elapsed < MAX_RECONNECT * 3, f"stop() занял {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Расчёт backoff
# ---------------------------------------------------------------------------

class TestBackoffCalc:
    def test_delay_increases_with_attempts(self):
        fm = make_fallback()
        delay0 = fm._calc_delay(0)
        delay1 = fm._calc_delay(1)
        delay2 = fm._calc_delay(2)
        assert delay0 <= delay1 <= delay2

    def test_delay_does_not_exceed_max(self):
        fm = make_fallback()
        for attempt in range(20):
            delay = fm._calc_delay(attempt)
            assert delay <= MAX_RECONNECT, \
                f"Задержка {delay:.3f}s превысила max {MAX_RECONNECT}s на попытке {attempt}"

    def test_delay_has_jitter(self):
        """Две задержки для одного attempt не равны (jitter)."""
        fm = make_fallback()
        delays = {fm._calc_delay(1) for _ in range(20)}
        # С вероятностью > 99.9% хотя бы два значения разные
        assert len(delays) > 1, "Jitter не работает — все задержки одинаковые"
