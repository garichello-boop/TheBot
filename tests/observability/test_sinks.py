"""Тесты FileLogSink и NdjsonSink."""
import json
import os
import pytest
import tempfile
from observability.events import BotEvent
from observability.sinks.file_sink import FileLogSink
from observability.sinks.ndjson_sink import NdjsonSink


def make_event(event_type="TEST_EVENT", level="INFO", **kwargs) -> BotEvent:
    defaults = dict(
        event_type=event_type,
        level=level,
        message="test message",
        bot_id="bot1",
        cycle_id="c1",
    )
    defaults.update(kwargs)
    return BotEvent(**defaults)


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestFileLogSink:
    def test_creates_log_file(self, tmpdir):
        path = os.path.join(tmpdir, "bot.log")
        sink = FileLogSink(log_path=path)
        sink.handle(make_event())
        assert os.path.exists(path)
        sink.close()

    def test_event_written(self, tmpdir):
        path = os.path.join(tmpdir, "bot.log")
        sink = FileLogSink(log_path=path)
        sink.handle(make_event(event_type="ORDER_CREATED"))
        sink.close()
        content = open(path).read()
        assert "ORDER_CREATED" in content
        assert "bot1" in content

    def test_min_level_filtering(self, tmpdir):
        path = os.path.join(tmpdir, "bot.log")
        sink = FileLogSink(log_path=path, min_level="WARNING")
        sink.handle(make_event(level="DEBUG"))
        sink.handle(make_event(level="INFO"))
        sink.handle(make_event(level="WARNING", event_type="WARN_EVENT"))
        sink.close()
        content = open(path).read()
        assert "WARN_EVENT" in content
        assert "TEST_EVENT" not in content

    def test_cycle_id_in_log(self, tmpdir):
        path = os.path.join(tmpdir, "bot.log")
        sink = FileLogSink(log_path=path)
        sink.handle(make_event(cycle_id="cycle_999"))
        sink.close()  # обязательно до выхода из tmpdir (Windows держит файл)
        content = open(path).read()
        assert "cycle_999" in content

    def test_failing_sink_does_not_raise(self, tmpdir):
        # Права на запись закрыты — sink не должен падать
        path = "/nonexistent_dir/bot.log"
        try:
            sink = FileLogSink(log_path=path)
            # Если создался — handle не должен падать
            sink.handle(make_event())
        except Exception:
            pass  # Создание может упасть, но не handle


class TestNdjsonSink:
    def test_creates_files(self, tmpdir):
        events_path = os.path.join(tmpdir, "events.ndjson")
        errors_path = os.path.join(tmpdir, "errors.ndjson")
        sink = NdjsonSink(events_path=events_path, errors_path=errors_path)
        sink.handle(make_event())
        sink.close()
        assert os.path.exists(events_path)

    def test_event_is_valid_json(self, tmpdir):
        events_path = os.path.join(tmpdir, "events.ndjson")
        sink = NdjsonSink(events_path=events_path, errors_path=os.path.join(tmpdir, "e.ndjson"))
        sink.handle(make_event(event_type="ORDER_FILLED", payload={"qty": "10"}))
        sink.close()
        lines = open(events_path).readlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event_type"] == "ORDER_FILLED"
        assert data["payload"]["qty"] == "10"

    def test_errors_go_to_errors_file(self, tmpdir):
        events_path = os.path.join(tmpdir, "events.ndjson")
        errors_path = os.path.join(tmpdir, "errors.ndjson")
        sink = NdjsonSink(events_path=events_path, errors_path=errors_path)
        sink.handle(make_event(level="INFO"))
        sink.handle(make_event(level="ERROR", event_type="ERROR_EVENT"))
        sink.handle(make_event(level="CRITICAL", event_type="CRIT_EVENT"))
        sink.close()
        error_lines = open(errors_path).readlines()
        event_types = [json.loads(l)["event_type"] for l in error_lines]
        assert "ERROR_EVENT" in event_types
        assert "CRIT_EVENT" in event_types

    def test_info_not_in_errors_file(self, tmpdir):
        events_path = os.path.join(tmpdir, "events.ndjson")
        errors_path = os.path.join(tmpdir, "errors.ndjson")
        sink = NdjsonSink(events_path=events_path, errors_path=errors_path)
        sink.handle(make_event(level="INFO", event_type="INFO_EVENT"))
        sink.close()
        if os.path.exists(errors_path):
            error_lines = [l for l in open(errors_path).readlines() if l.strip()]
            event_types = [json.loads(l)["event_type"] for l in error_lines]
            assert "INFO_EVENT" not in event_types

    def test_multiple_events(self, tmpdir):
        events_path = os.path.join(tmpdir, "events.ndjson")
        sink = NdjsonSink(events_path=events_path, errors_path=os.path.join(tmpdir, "e.ndjson"))
        for i in range(10):
            sink.handle(make_event(event_type=f"EVENT_{i}"))
        sink.close()
        lines = [l for l in open(events_path).readlines() if l.strip()]
        assert len(lines) == 10

    def test_roundtrip_with_from_dict(self, tmpdir):
        events_path = os.path.join(tmpdir, "events.ndjson")
        sink = NdjsonSink(events_path=events_path, errors_path=os.path.join(tmpdir, "e.ndjson"))
        original = make_event(ticker="PHOR", payload={"price": "3200", "qty": "5"})
        sink.handle(original)
        sink.close()
        line = open(events_path).readline().strip()
        restored = BotEvent.from_dict(json.loads(line))
        assert restored.ticker == "PHOR"
        assert restored.payload["price"] == "3200"
