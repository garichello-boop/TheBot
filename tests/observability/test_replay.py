"""Тесты ReplayManager."""
import json
import os
import tempfile
import pytest
from observability.events import BotEvent
from observability.replay import ReplayManager


def make_event(event_type="TEST", bot_id="bot1", ts_ms=None) -> BotEvent:
    e = BotEvent(
        event_type=event_type,
        level="INFO",
        message="test",
        bot_id=bot_id,
        cycle_id="c1",
    )
    if ts_ms is not None:
        # BotEvent frozen — пересоздаём с нужным ts_ms
        return BotEvent(
            event_type=event_type,
            level="INFO",
            message="test",
            bot_id=bot_id,
            cycle_id="c1",
            ts_ms=ts_ms,
        )
    return e


def write_ndjson(path: str, events: list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestReplayManagerFileReading:
    def test_count_events(self, tmpdir):
        path = os.path.join(tmpdir, "events.ndjson")
        events = [make_event(f"EVENT_{i}") for i in range(5)]
        write_ndjson(path, events)

        mgr = ReplayManager(events_path=path)
        assert mgr.count_ndjson_events() == 5

    def test_empty_file(self, tmpdir):
        path = os.path.join(tmpdir, "events.ndjson")
        open(path, "w").close()
        mgr = ReplayManager(events_path=path)
        assert mgr.count_ndjson_events() == 0

    def test_nonexistent_file(self, tmpdir):
        path = os.path.join(tmpdir, "missing.ndjson")
        mgr = ReplayManager(events_path=path)
        assert mgr.count_ndjson_events() == 0

    def test_bot_id_filter(self, tmpdir):
        path = os.path.join(tmpdir, "events.ndjson")
        events = [
            make_event("EVT", bot_id="bot1"),
            make_event("EVT", bot_id="bot2"),
            make_event("EVT", bot_id="bot1"),
        ]
        write_ndjson(path, events)

        mgr = ReplayManager(events_path=path, bot_id_filter="bot1")
        assert mgr.count_ndjson_events() == 2

    def test_invalid_lines_skipped(self, tmpdir):
        path = os.path.join(tmpdir, "events.ndjson")
        with open(path, "w") as f:
            f.write(json.dumps(make_event("GOOD").to_dict()) + "\n")
            f.write("not json at all\n")
            f.write(json.dumps(make_event("GOOD2").to_dict()) + "\n")

        mgr = ReplayManager(events_path=path)
        assert mgr.count_ndjson_events() == 2

    def test_rotation_files_ordered(self, tmpdir):
        base = os.path.join(tmpdir, "events.ndjson")
        # Создаём ротационные файлы
        write_ndjson(base + ".2", [make_event("OLD", ts_ms=1000)])
        write_ndjson(base + ".1", [make_event("MIDDLE", ts_ms=2000)])
        write_ndjson(base, [make_event("NEW", ts_ms=3000)])

        mgr = ReplayManager(events_path=base)
        all_events = list(mgr._iter_events_chronological())
        assert len(all_events) == 3
        # Порядок: старые первыми (.2 → .1 → base)
        assert all_events[0].event_type == "OLD"
        assert all_events[2].event_type == "NEW"
