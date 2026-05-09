"""Тесты TelegramSink — без реальных HTTP-запросов."""
import time
import pytest
from unittest.mock import patch, MagicMock
from observability.events import BotEvent
from observability.sinks.telegram_sink import TelegramSink


def make_event(event_type="TEST", level="INFO", bot_id="bot1") -> BotEvent:
    return BotEvent(
        event_type=event_type,
        level=level,
        message="test",
        bot_id=bot_id,
        cycle_id="c1",
    )


@pytest.fixture
def sink():
    with patch("observability.sinks.telegram_sink.urllib_request.urlopen") as mock_open:
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b'{"ok": true}'
        mock_open.return_value = mock_response

        s = TelegramSink(
            token="test_token",
            chat_id="12345",
            max_per_minute=100,
            dedup_window_sec=1,
        )
        yield s, mock_open
        s.close()


class TestTelegramSink:
    def test_handle_does_not_raise(self, sink):
        s, _ = sink
        s.handle(make_event())  # Не должно падать

    def test_deduplication(self, sink):
        s, mock_urlopen = sink
        # Отправляем одно и то же событие несколько раз
        for _ in range(5):
            s.handle(make_event(event_type="REPEATED_ERROR"))
        s.flush()
        # Только первое должно уйти (остальные в dedup-окне)
        assert mock_urlopen.call_count == 1

    def test_different_event_types_not_deduplicated(self, sink):
        s, mock_urlopen = sink
        s.handle(make_event(event_type="ERROR_A"))
        s.handle(make_event(event_type="ERROR_B"))
        s.flush()
        assert mock_urlopen.call_count == 2

    def test_dedup_window_expires(self, sink):
        s, mock_urlopen = sink
        # dedup_window_sec=1 в фикстуре
        s.handle(make_event(event_type="TIMEOUT_TEST"))
        time.sleep(1.5)
        s.handle(make_event(event_type="TIMEOUT_TEST"))
        s.flush()
        assert mock_urlopen.call_count == 2

    def test_queue_overflow_keeps_critical(self):
        """При переполнении очереди CRITICAL сообщения не теряются."""
        with patch("observability.sinks.telegram_sink.urllib_request.urlopen"):
            s = TelegramSink(
                token="t",
                chat_id="c",
                queue_maxsize=2,
                dedup_window_sec=999,
            )
            # Заполняем очередь INFO
            for i in range(5):
                s.handle(make_event(event_type=f"INFO_{i}", level="INFO"))
            # CRITICAL должен попасть
            s.handle(make_event(event_type="CRIT_EVENT", level="CRITICAL"))
            s.close()

    def test_close_stops_worker(self):
        with patch("observability.sinks.telegram_sink.urllib_request.urlopen"):
            s = TelegramSink(token="t", chat_id="c")
            s.close()
            assert not s._thread.is_alive()
