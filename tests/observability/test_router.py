"""Тесты NotificationRouter."""
import pytest
from observability.events import BotEvent
from observability.router import NotificationRouter


def make_event(event_type: str, level: str = "INFO") -> BotEvent:
    return BotEvent(
        event_type=event_type,
        level=level,
        message="test",
        bot_id="bot1",
        cycle_id="",
    )


class TestNotificationRouter:
    def test_bot_started_goes_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("BOT_STARTED"))

    def test_heartbeat_not_to_telegram(self):
        assert not NotificationRouter.should_telegram(make_event("BOT_HEARTBEAT"))

    def test_price_received_not_to_telegram(self):
        assert not NotificationRouter.should_telegram(make_event("PRICE_RECEIVED"))

    def test_stop_crane_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("STOP_CRANE_TRIGGERED"))

    def test_kill_switch_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("KILL_SWITCH_TRIGGERED"))

    def test_critical_always_to_telegram(self):
        # Даже неизвестное событие уровня CRITICAL идёт в Telegram
        assert NotificationRouter.should_telegram(make_event("UNKNOWN_EVENT", level="CRITICAL"))

    def test_order_filled_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("ORDER_FILLED"))

    def test_order_create_failed_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("ORDER_CREATE_FAILED"))

    def test_trade_already_applied_not_to_telegram(self):
        assert not NotificationRouter.should_telegram(make_event("TRADE_ALREADY_APPLIED"))

    def test_warning_to_console(self):
        assert NotificationRouter.should_console(make_event("ANY_EVENT", level="WARNING"))

    def test_debug_not_to_console(self):
        assert not NotificationRouter.should_console(make_event("ANY_EVENT", level="DEBUG"))

    def test_info_not_to_console(self):
        assert not NotificationRouter.should_console(make_event("ANY_EVENT", level="INFO"))

    def test_cycle_started_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("CYCLE_STARTED"))

    def test_cycle_closed_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("CYCLE_CLOSED"))

    def test_insufficient_funds_to_telegram(self):
        assert NotificationRouter.should_telegram(make_event("INSUFFICIENT_FUNDS"))
