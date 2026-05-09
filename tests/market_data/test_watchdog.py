"""
Тесты: watchdog.py — WatchdogTimer.

Используем короткие threshold/interval чтобы тесты были быстрыми.
threading.Event с timeout — чтобы не зависать при провале.
"""

import threading
import time

import pytest

from market_data.watchdog import WatchdogTimer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STALE_SEC    = 0.2   # порог устаревания
CHECK_SEC    = 0.05  # интервал проверки
WAIT_TIMEOUT = 2.0   # максимальное ожидание в тесте


def make_watchdog(on_stale=None, on_recovered=None) -> WatchdogTimer:
    return WatchdogTimer(
        stale_threshold_sec=STALE_SEC,
        on_stale=on_stale or (lambda: None),
        on_recovered=on_recovered,
        check_interval_sec=CHECK_SEC,
    )


# ---------------------------------------------------------------------------
# Жизненный цикл
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_not_running_before_start(self):
        wd = make_watchdog()
        assert wd.is_running() is False

    def test_running_after_start(self):
        wd = make_watchdog()
        wd.start()
        try:
            assert wd.is_running() is True
        finally:
            wd.stop()

    def test_not_running_after_stop(self):
        wd = make_watchdog()
        wd.start()
        wd.stop()
        assert wd.is_running() is False

    def test_start_idempotent(self):
        wd = make_watchdog()
        wd.start()
        wd.start()  # второй вызов — не создаёт второй поток
        try:
            assert wd.is_running() is True
        finally:
            wd.stop()

    def test_stop_idempotent(self):
        wd = make_watchdog()
        wd.start()
        wd.stop()
        wd.stop()  # второй вызов — не падает


# ---------------------------------------------------------------------------
# on_stale — срабатывание
# ---------------------------------------------------------------------------

class TestOnStale:
    def test_on_stale_called_when_data_expires(self):
        """Если update() не вызывается дольше threshold — on_stale срабатывает."""
        stale_event = threading.Event()
        wd = make_watchdog(on_stale=lambda: stale_event.set())
        wd.start()
        try:
            triggered = stale_event.wait(timeout=WAIT_TIMEOUT)
            assert triggered, "on_stale не был вызван в ожидаемое время"
        finally:
            wd.stop()

    def test_on_stale_called_only_once(self):
        """on_stale вызывается один раз при переходе fresh→stale, не на каждом чеке."""
        call_count = [0]
        lock       = threading.Lock()

        def on_stale():
            with lock:
                call_count[0] += 1

        wd = make_watchdog(on_stale=on_stale)
        wd.start()
        try:
            # Ждём достаточно долго чтобы прошло несколько check-итераций после stale
            time.sleep(STALE_SEC + CHECK_SEC * 5)
            with lock:
                count = call_count[0]
            assert count == 1, f"on_stale вызван {count} раз, ожидался 1"
        finally:
            wd.stop()

    def test_is_stale_property_after_threshold(self):
        """is_stale == True после истечения threshold."""
        stale_event = threading.Event()
        wd = make_watchdog(on_stale=lambda: stale_event.set())
        wd.start()
        try:
            stale_event.wait(timeout=WAIT_TIMEOUT)
            assert wd.is_stale is True
        finally:
            wd.stop()

    def test_not_stale_immediately_after_start(self):
        """Сразу после старта данные считаются свежими."""
        wd = make_watchdog()
        wd.start()
        try:
            assert wd.is_stale is False
        finally:
            wd.stop()


# ---------------------------------------------------------------------------
# update() — сброс таймера
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_prevents_stale(self):
        """Регулярный update() не даёт on_stale сработать."""
        stale_called = threading.Event()
        wd = make_watchdog(on_stale=lambda: stale_called.set())
        wd.start()
        try:
            # Обновляем чаще чем threshold истекает
            deadline = time.monotonic() + STALE_SEC * 3
            while time.monotonic() < deadline:
                wd.update()
                time.sleep(CHECK_SEC / 2)

            assert not stale_called.is_set(), "on_stale не должен был сработать"
        finally:
            wd.stop()

    def test_update_resets_stale_state(self):
        """После update() is_stale возвращается в False."""
        stale_event = threading.Event()
        wd = make_watchdog(on_stale=lambda: stale_event.set())
        wd.start()
        try:
            stale_event.wait(timeout=WAIT_TIMEOUT)
            assert wd.is_stale is True
            wd.update()
            assert wd.is_stale is False
        finally:
            wd.stop()


# ---------------------------------------------------------------------------
# on_recovered — срабатывание
# ---------------------------------------------------------------------------

class TestOnRecovered:
    def test_on_recovered_called_after_update_post_stale(self):
        """on_recovered вызывается когда update() вызван после stale."""
        stale_event     = threading.Event()
        recovered_event = threading.Event()

        wd = make_watchdog(
            on_stale=lambda: stale_event.set(),
            on_recovered=lambda: recovered_event.set(),
        )
        wd.start()
        try:
            # Ждём stale
            stale_event.wait(timeout=WAIT_TIMEOUT)
            # Обновляем — должен сработать on_recovered
            wd.update()
            triggered = recovered_event.wait(timeout=WAIT_TIMEOUT)
            assert triggered, "on_recovered не был вызван"
        finally:
            wd.stop()

    def test_on_recovered_not_called_when_never_stale(self):
        """on_recovered не вызывается если stale не было."""
        recovered_called = threading.Event()
        stale_event      = threading.Event()

        wd = make_watchdog(
            on_stale=lambda: stale_event.set(),
            on_recovered=lambda: recovered_called.set(),
        )
        wd.start()
        try:
            # Сразу обновляем — recovered не должен быть вызван (не было stale)
            wd.update()
            time.sleep(CHECK_SEC * 2)
            assert not recovered_called.is_set()
        finally:
            wd.stop()

    def test_on_recovered_called_only_once_per_recovery(self):
        """on_recovered вызывается один раз при переходе stale→fresh."""
        stale_event    = threading.Event()
        recovered_count = [0]
        lock           = threading.Lock()

        def on_recovered():
            with lock:
                recovered_count[0] += 1

        wd = make_watchdog(
            on_stale=lambda: stale_event.set(),
            on_recovered=on_recovered,
        )
        wd.start()
        try:
            stale_event.wait(timeout=WAIT_TIMEOUT)
            # Обновляем несколько раз — recovered должен быть вызван один раз
            wd.update()
            wd.update()
            wd.update()
            time.sleep(CHECK_SEC * 2)
            with lock:
                count = recovered_count[0]
            assert count == 1, f"on_recovered вызван {count} раз, ожидался 1"
        finally:
            wd.stop()


# ---------------------------------------------------------------------------
# age_sec
# ---------------------------------------------------------------------------

class TestAgeSec:
    def test_age_increases_over_time(self):
        wd = make_watchdog()
        wd.start()
        try:
            time.sleep(0.1)
            assert wd.age_sec >= 0.05
        finally:
            wd.stop()

    def test_age_resets_after_update(self):
        wd = make_watchdog()
        wd.start()
        try:
            time.sleep(0.15)
            wd.update()
            assert wd.age_sec < 0.05
        finally:
            wd.stop()


# ---------------------------------------------------------------------------
# Валидация параметров
# ---------------------------------------------------------------------------

class TestValidation:
    def test_zero_threshold_raises(self):
        with pytest.raises(ValueError, match="stale_threshold_sec"):
            WatchdogTimer(stale_threshold_sec=0, on_stale=lambda: None)

    def test_negative_threshold_raises(self):
        with pytest.raises(ValueError, match="stale_threshold_sec"):
            WatchdogTimer(stale_threshold_sec=-1, on_stale=lambda: None)
