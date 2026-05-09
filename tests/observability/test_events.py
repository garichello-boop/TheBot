"""Тесты BotEvent."""
import pytest
from observability.events import BotEvent


def make_event(**kwargs) -> BotEvent:
    defaults = dict(
        event_type="TEST_EVENT",
        level="INFO",
        message="test",
        bot_id="bot1",
        cycle_id="cycle_001",
    )
    defaults.update(kwargs)
    return BotEvent(**defaults)


class TestBotEventCreation:
    def test_minimal_fields(self):
        e = make_event()
        assert e.event_type == "TEST_EVENT"
        assert e.level == "INFO"
        assert e.bot_id == "bot1"
        assert e.cycle_id == "cycle_001"
        assert e.ts_ms > 0
        assert e.event_version == 1

    def test_defaults(self):
        e = make_event()
        assert e.ticker == ""
        assert e.strategy_name == ""
        assert e.payload == {}

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError, match="Недопустимый уровень"):
            make_event(level="VERBOSE")

    def test_empty_event_type_raises(self):
        with pytest.raises(ValueError, match="event_type"):
            make_event(event_type="")

    def test_empty_bot_id_raises(self):
        with pytest.raises(ValueError, match="bot_id"):
            make_event(bot_id="")

    def test_frozen(self):
        e = make_event()
        with pytest.raises(Exception):
            e.message = "modified"  # type: ignore

    def test_all_valid_levels(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            e = make_event(level=level)
            assert e.level == level


class TestBotEventSerialization:
    def test_to_dict_keys(self):
        e = make_event(ticker="BTCUSDT", payload={"price": "3200"})
        d = e.to_dict()
        assert set(d.keys()) == {
            "ts_ms", "level", "event_type", "event_version",
            "message", "bot_id", "ticker", "cycle_id", "strategy_name", "payload"
        }

    def test_roundtrip(self):
        e = make_event(
            ticker="PHOR",
            strategy_name="MeanReversion",
            payload={"qty": "10"},
        )
        restored = BotEvent.from_dict(e.to_dict())
        assert restored.event_type == e.event_type
        assert restored.bot_id == e.bot_id
        assert restored.cycle_id == e.cycle_id
        assert restored.ticker == e.ticker
        assert restored.payload == e.payload
        assert restored.ts_ms == e.ts_ms

    def test_from_dict_missing_optional_fields(self):
        minimal = {
            "ts_ms": 1000000,
            "level": "INFO",
            "event_type": "BOT_STARTED",
            "message": "ok",
            "bot_id": "bot1",
            "cycle_id": "",
        }
        e = BotEvent.from_dict(minimal)
        assert e.ticker == ""
        assert e.payload == {}
