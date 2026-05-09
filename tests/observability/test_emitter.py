"""Тесты EventEmitter."""
import pytest
from unittest.mock import MagicMock, call
from observability.emitter import EventEmitter
from observability.events import BotEvent
from observability.sinks.base import AbstractSink


class CaptureSink(AbstractSink):
    def __init__(self):
        self.events = []
        self.closed = False

    def handle(self, event: BotEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


class FailingSink(AbstractSink):
    def handle(self, event: BotEvent) -> None:
        raise RuntimeError("Sink error")


@pytest.fixture
def emitter():
    return EventEmitter(bot_id="test_bot", ticker="BTCUSDT", strategy_name="MR")


class TestEmit:
    def test_emit_returns_event(self, emitter):
        sink = CaptureSink()
        emitter.add_file_sink(sink)
        event = emitter.emit("BOT_STARTED", "INFO", "started")
        assert isinstance(event, BotEvent)
        assert event.event_type == "BOT_STARTED"

    def test_context_enrichment(self, emitter):
        sink = CaptureSink()
        emitter.add_file_sink(sink)
        emitter.emit("ORDER_CREATED", "INFO", "order")
        e = sink.events[0]
        assert e.bot_id == "test_bot"
        assert e.ticker == "BTCUSDT"
        assert e.strategy_name == "MR"

    def test_cycle_id_from_context(self, emitter):
        sink = CaptureSink()
        emitter.add_file_sink(sink)
        emitter.set_cycle_id("cycle_42")
        emitter.emit("CYCLE_STARTED", "INFO", "cycle")
        assert sink.events[0].cycle_id == "cycle_42"

    def test_cycle_id_override(self, emitter):
        sink = CaptureSink()
        emitter.add_file_sink(sink)
        emitter.set_cycle_id("cycle_42")
        emitter.emit("TICK_SKIPPED", "INFO", "skip", cycle_id="override_id")
        assert sink.events[0].cycle_id == "override_id"

    def test_file_sinks_always_receive(self, emitter):
        s1, s2 = CaptureSink(), CaptureSink()
        emitter.add_file_sink(s1)
        emitter.add_file_sink(s2)
        emitter.emit("BOT_HEARTBEAT", "DEBUG", "heartbeat")
        assert len(s1.events) == 1
        assert len(s2.events) == 1

    def test_telegram_sink_routing(self, emitter):
        tg = CaptureSink()
        file_sink = CaptureSink()
        emitter.add_file_sink(file_sink)
        emitter.set_telegram_sink(tg)

        # BOT_STARTED → в Telegram
        emitter.emit("BOT_STARTED", "INFO", "started")
        assert len(tg.events) == 1

        # BOT_HEARTBEAT → только в файл
        emitter.emit("BOT_HEARTBEAT", "DEBUG", "heartbeat")
        assert len(tg.events) == 1  # не изменился

    def test_critical_always_to_telegram(self, emitter):
        tg = CaptureSink()
        emitter.set_telegram_sink(tg)
        # Событие не в списке telegram-событий, но CRITICAL
        emitter.emit("SOME_UNKNOWN_EVENT", "CRITICAL", "critical!")
        assert len(tg.events) == 1

    def test_failing_sink_does_not_crash_bot(self, emitter):
        fail_sink = FailingSink()
        good_sink = CaptureSink()
        emitter.add_file_sink(fail_sink)
        emitter.add_file_sink(good_sink)
        # Не должно выбрасывать исключение
        emitter.emit("BOT_STARTED", "INFO", "started")
        # Хороший sink всё равно получил событие
        assert len(good_sink.events) == 1

    def test_emit_with_payload(self, emitter):
        sink = CaptureSink()
        emitter.add_file_sink(sink)
        emitter.emit("ORDER_FILLED", "INFO", "filled", payload={"qty": "10", "price": "3200"})
        assert sink.events[0].payload == {"qty": "10", "price": "3200"}


class TestEmitterLifecycle:
    def test_close_propagates_to_sinks(self, emitter):
        sink = CaptureSink()
        emitter.add_file_sink(sink)
        emitter.close()
        assert sink.closed

    def test_set_cycle_id(self, emitter):
        sink = CaptureSink()
        emitter.add_file_sink(sink)
        emitter.set_cycle_id("new_cycle")
        emitter.emit("CYCLE_STARTED", "INFO", "cycle")
        assert sink.events[0].cycle_id == "new_cycle"
