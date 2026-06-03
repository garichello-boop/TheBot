"""
tests/test_trailing_tp.py

Tests for trailing take-profit:
    MeanReversionParams         — TRAILING_TP_ENABLED / TRAILING_TP_PCT validation
    BotLoop._evaluate_trailing_tp()   — high-water mark update and trigger logic
    BotLoop._execute_trailing_tp_close() — close sequence
    BotLoop._execute_enter()            — resets _trailing_tp_high
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from bot_config.models import CycleSnapshot
from bot_config.strategy_schemas import MeanReversionParams
from bot_state.models import BotState, CycleStatus, ClosingReason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snapshot(params: dict) -> CycleSnapshot:
    return CycleSnapshot(
        strategy_params=params,
        config_version=1,
        started_at=datetime.now(timezone.utc),
    )


def _make_ctx(bid: Decimal) -> MagicMock:
    ctx = MagicMock()
    ctx.price_data.bid = bid
    ctx.ticker = "BTCUSDT"
    ctx.bot_id = "test_bot"
    ctx.user_id = "test_user"
    return ctx


def _make_state(
    avg_price: Decimal | None = Decimal("30000"),
    cycle_status: str = "IN_POSITION",
    position_qty: Decimal = Decimal("0.001"),
    cycle_id: str = "cycle-001",
) -> MagicMock:
    state = MagicMock()
    state.position_avg_price = avg_price
    state.cycle_status = cycle_status
    state.position_qty = position_qty
    state.cycle_id = cycle_id
    return state


def _make_loop() -> "BotLoop":  # type: ignore[name-defined]
    """Create BotLoop with all dependencies mocked."""
    from business_logic.bot_loop import BotLoop  # noqa: PLC0415

    settings = SimpleNamespace(
        balance_drift_pct=5.0,
        broker_request_timeout_sec=5.0,
        broker_retry_delay_sec=1.0,
        broker_max_retries=3,
        cancel_max_retries=5,
        dca_mode="LAZY",
        max_dca_count=3,
        partial_fill_threshold_pct=90.0,
        tp_partial_close_threshold_pct=90.0,
        dust_threshold=0.0001,
        close_remainder_mode="MARKET",
        close_remainder_timeout_sec=30,
        max_market_close_slippage_pct=0.5,
        cooldown_sec=0,
        sl_max_market_slippage_pct=0.5,
        max_position_days=None,
        force_close_on_timeout=False,
        max_entry_slippage_pct=2.0,
        entry_order_timeout_sec=300,
        heartbeat_interval_ticks=10,
        tick_max_duration_sec=30.0,
        tick_interval_sec=10.0,
        critical_error_threshold=5,
    )
    broker = MagicMock()
    broker.get_mode.return_value.value = "PAPER"

    return BotLoop(
        market=MagicMock(),
        broker=broker,
        state_manager=MagicMock(),
        state_repo=MagicMock(),
        registry_repo=MagicMock(),
        config_watcher=MagicMock(),
        strategy=MagicMock(),
        emitter=MagicMock(),
        settings=settings,
        bot_id="test_bot",
        user_id="test_user",
    )


# ---------------------------------------------------------------------------
# MeanReversionParams — TRAILING_TP fields
# ---------------------------------------------------------------------------

class TestMeanReversionParamsTrailingTP:
    def test_defaults_disabled(self):
        params = MeanReversionParams()
        assert params.TRAILING_TP_ENABLED is False
        assert params.TRAILING_TP_PCT == 1.0

    def test_enabled_with_valid_pct(self):
        params = MeanReversionParams(TRAILING_TP_ENABLED=True, TRAILING_TP_PCT=2.0)
        assert params.TRAILING_TP_ENABLED is True
        assert params.TRAILING_TP_PCT == 2.0

    def test_enabled_uses_default_pct(self):
        """TRAILING_TP_ENABLED=True without explicit PCT uses default 1.0."""
        params = MeanReversionParams(TRAILING_TP_ENABLED=True)
        assert params.TRAILING_TP_PCT == 1.0

    def test_disabled_zero_pct_allowed(self):
        """PCT=0 is allowed when trailing is disabled (no cross-field check)."""
        # TRAILING_TP_ENABLED defaults to False, so no cross-validation
        params = MeanReversionParams.model_validate(
            {"TRAILING_TP_ENABLED": False, "TRAILING_TP_PCT": 0.5}
        )
        assert params.TRAILING_TP_ENABLED is False

    def test_string_coercion(self):
        """String values from JSONB are coerced correctly."""
        params = MeanReversionParams.model_validate(
            {"TRAILING_TP_ENABLED": "true", "TRAILING_TP_PCT": "1.5"}
        )
        assert params.TRAILING_TP_ENABLED is True
        assert params.TRAILING_TP_PCT == 1.5

    def test_false_string_coercion(self):
        params = MeanReversionParams.model_validate({"TRAILING_TP_ENABLED": "false"})
        assert params.TRAILING_TP_ENABLED is False

    def test_extra_params_pass_through(self):
        """Unknown keys (like tp_pct, ma_period) are not rejected."""
        params = MeanReversionParams.model_validate(
            {"tp_pct": 0.035, "ma_period": 180, "TRAILING_TP_ENABLED": True}
        )
        assert params.TRAILING_TP_ENABLED is True


# ---------------------------------------------------------------------------
# _evaluate_trailing_tp — edge cases
# ---------------------------------------------------------------------------

class TestEvaluateTrailingTp:
    """Tests for BotLoop._evaluate_trailing_tp()."""

    def test_returns_false_when_no_snapshot(self):
        loop = _make_loop()
        loop._cycle_snapshot = None
        ctx = _make_ctx(Decimal("31000"))
        state = _make_state()
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_returns_false_when_disabled(self):
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": False,
            "tp_pct": 0.035,
        })
        ctx = _make_ctx(Decimal("31000"))
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_returns_false_when_key_missing(self):
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({"tp_pct": 0.035})
        ctx = _make_ctx(Decimal("31000"))
        state = _make_state()
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_returns_false_when_avg_price_none(self):
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        ctx = _make_ctx(Decimal("31000"))
        state = _make_state(avg_price=None)
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_returns_false_when_avg_price_zero(self):
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        ctx = _make_ctx(Decimal("31000"))
        state = _make_state(avg_price=Decimal("0"))
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_initialises_high_water_mark_on_first_call(self):
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        loop._trailing_tp_high = None
        ctx = _make_ctx(Decimal("30500"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._evaluate_trailing_tp(ctx, state)
        assert loop._trailing_tp_high == Decimal("30500")

    def test_updates_high_water_when_price_rises(self):
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        loop._trailing_tp_high = Decimal("31000")
        ctx = _make_ctx(Decimal("32000"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._evaluate_trailing_tp(ctx, state)
        assert loop._trailing_tp_high == Decimal("32000")

    def test_does_not_lower_high_water_when_price_drops(self):
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        loop._trailing_tp_high = Decimal("33000")
        ctx = _make_ctx(Decimal("32000"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._evaluate_trailing_tp(ctx, state)
        assert loop._trailing_tp_high == Decimal("33000")

    def test_not_triggered_on_first_tick(self):
        """
        On the very first tick, high_water == bid.
        trailing_trigger = bid * 0.99 < bid, so current_bid > trigger.
        Must NOT fire.
        """
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        loop._trailing_tp_high = None
        # avg=30000, tp_pct=3.5% → activation=31050
        # bid=32000 > activation (trail is active in principle)
        # but on FIRST tick: high=32000, trigger=31680, bid=32000 > 31680 → no fire
        ctx = _make_ctx(Decimal("32000"))
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_not_activated_below_initial_tp(self):
        """Price hasn't reached activation level (avg * (1+tp_pct)) yet."""
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,  # activation = 30000 * 1.035 = 31050
        })
        loop._trailing_tp_high = Decimal("31000")  # below 31050
        ctx = _make_ctx(Decimal("30800"))
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_not_triggered_when_trailing_level_below_activation(self):
        """
        If trail_pct is very large, trailing_trigger may drop below activation_price.
        In that case we must NOT fire (would lock in a loss relative to initial TP).
        Example: high=31100, trail=10%, trigger=27990 < activation=31050 → no fire.
        """
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 10.0,   # huge trail
            "tp_pct": 0.035,           # activation = 31050
        })
        # high_water = 31100 (just above activation)
        # trailing_trigger = 31100 * 0.90 = 27990 < 31050 → NO fire
        loop._trailing_tp_high = Decimal("31100")
        ctx = _make_ctx(Decimal("28000"))
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_triggered_when_bid_drops_to_trailing_level(self):
        """
        Happy path: price peaked well above activation, now dropped to trailing level.
        avg=30000, tp_pct=3.5% → activation=31050
        high_water=33000, trail=1% → trigger=32670
        bid=32670 → FIRE
        """
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        loop._trailing_tp_high = Decimal("33000")
        ctx = _make_ctx(Decimal("32670"))
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is True

    def test_triggered_when_bid_below_trailing_level(self):
        """bid strictly below trailing trigger → should fire."""
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        loop._trailing_tp_high = Decimal("33000")
        ctx = _make_ctx(Decimal("32000"))  # well below trigger 32670
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is True

    def test_not_triggered_when_bid_above_trailing_level(self):
        """bid above trailing trigger → should NOT fire."""
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        loop._trailing_tp_high = Decimal("33000")
        ctx = _make_ctx(Decimal("33000"))  # at the high, trigger=32670, bid > trigger
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is False

    def test_uses_take_profit_fallback_key(self):
        """Handles TAKE_PROFIT key (MeanReversionParams schema name) as fallback."""
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "TAKE_PROFIT": 0.02,  # 2% → activation = 30000 * 1.02 = 30600
        })
        loop._trailing_tp_high = Decimal("32000")
        ctx = _make_ctx(Decimal("31600"))  # 32000 * 0.99 = 31680 > 31600 → fire
        state = _make_state(avg_price=Decimal("30000"))
        assert loop._evaluate_trailing_tp(ctx, state) is True

    def test_high_water_accumulated_across_ticks(self):
        """
        Simulate several ticks: price rises then drops.
        Verify high-water mark accumulates and trigger fires on reversal.
        """
        loop = _make_loop()
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,  # activation=31050
        })
        loop._trailing_tp_high = None
        avg = Decimal("30000")

        # Tick 1: bid=31000 (below activation) → no fire, high=31000
        assert loop._evaluate_trailing_tp(_make_ctx(Decimal("31000")), _make_state(avg)) is False
        assert loop._trailing_tp_high == Decimal("31000")

        # Tick 2: bid=32000 (above activation) → high=32000, trigger=31680, bid>trigger
        assert loop._evaluate_trailing_tp(_make_ctx(Decimal("32000")), _make_state(avg)) is False
        assert loop._trailing_tp_high == Decimal("32000")

        # Tick 3: bid=33000 → high=33000, trigger=32670, bid>trigger
        assert loop._evaluate_trailing_tp(_make_ctx(Decimal("33000")), _make_state(avg)) is False
        assert loop._trailing_tp_high == Decimal("33000")

        # Tick 4: bid=32600 (below trigger 32670) → FIRE
        assert loop._evaluate_trailing_tp(_make_ctx(Decimal("32600")), _make_state(avg)) is True
        # high-water doesn't change (32600 < 33000)
        assert loop._trailing_tp_high == Decimal("33000")


# ---------------------------------------------------------------------------
# _execute_trailing_tp_close
# ---------------------------------------------------------------------------

class TestExecuteTrailingTpClose:
    def _loop_with_close_mock(self, close_status: str = "COMPLETE"):
        loop = _make_loop()
        loop._trailing_tp_high = Decimal("33000")
        loop._cycle_snapshot = _make_snapshot({
            "TRAILING_TP_ENABLED": True,
            "TRAILING_TP_PCT": 1.0,
            "tp_pct": 0.035,
        })
        # Mock state_manager.transition to return a new state mock
        new_state = MagicMock()
        loop._state_manager.transition.return_value = new_state
        # Replace _close_protocol with a full MagicMock (it's a real object from __init__)
        final_state = MagicMock()
        loop._close_protocol = MagicMock()
        loop._close_protocol.run.return_value = (final_state, close_status)
        return loop, new_state, final_state

    def test_emits_trailing_tp_triggered_event(self):
        loop, _, _ = self._loop_with_close_mock()
        ctx = _make_ctx(Decimal("32600"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._execute_trailing_tp_close(ctx, state)
        loop._emitter.emit.assert_called_once()
        event_type = loop._emitter.emit.call_args[1]["event_type"]
        assert event_type == "TRAILING_TP_TRIGGERED"

    def test_transitions_to_closing_with_tp_reason(self):
        loop, _, _ = self._loop_with_close_mock()
        ctx = _make_ctx(Decimal("32600"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._execute_trailing_tp_close(ctx, state)
        loop._state_manager.transition.assert_called_once_with(
            state,
            CycleStatus.CLOSING,
            closing_reason=ClosingReason.TP,
        )

    def test_calls_close_protocol_run(self):
        loop, new_state, _ = self._loop_with_close_mock()
        ctx = _make_ctx(Decimal("32600"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._execute_trailing_tp_close(ctx, state)
        loop._close_protocol.run.assert_called_once_with(ctx, new_state)

    def test_clears_snapshot_and_high_water_on_complete(self):
        loop, _, _ = self._loop_with_close_mock(close_status="COMPLETE")
        ctx = _make_ctx(Decimal("32600"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._execute_trailing_tp_close(ctx, state)
        assert loop._cycle_snapshot is None
        assert loop._trailing_tp_high is None

    def test_does_not_clear_on_incomplete(self):
        loop, _, _ = self._loop_with_close_mock(close_status="IN_PROGRESS")
        ctx = _make_ctx(Decimal("32600"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._execute_trailing_tp_close(ctx, state)
        # Snapshot and high_water should NOT be cleared
        assert loop._cycle_snapshot is not None
        assert loop._trailing_tp_high == Decimal("33000")

    def test_returns_final_state(self):
        loop, _, final_state = self._loop_with_close_mock()
        ctx = _make_ctx(Decimal("32600"))
        state = _make_state(avg_price=Decimal("30000"))
        result = loop._execute_trailing_tp_close(ctx, state)
        assert result is final_state

    def test_payload_contains_high_water_and_profit(self):
        loop, _, _ = self._loop_with_close_mock()
        ctx = _make_ctx(Decimal("32600"))
        state = _make_state(avg_price=Decimal("30000"))
        loop._execute_trailing_tp_close(ctx, state)
        payload = loop._emitter.emit.call_args[1]["payload"]
        assert "high_water_mark" in payload
        assert "profit_pct" in payload
        assert payload["high_water_mark"] == "33000"


# ---------------------------------------------------------------------------
# _execute_enter — resets _trailing_tp_high
# ---------------------------------------------------------------------------

class TestExecuteEnterResetsTrailingHigh:
    def test_trailing_high_reset_on_new_cycle(self):
        loop = _make_loop()
        loop._trailing_tp_high = Decimal("99999")  # stale value from previous cycle
        loop._cycle_snapshot = _make_snapshot({"tp_pct": 0.035})

        # Minimal decision mock
        decision = MagicMock()
        decision.entry_qty = Decimal("0.001")
        decision.entry_price = Decimal("30000")
        decision.dca_levels = ()

        # config_watcher.create_snapshot() must return a valid CycleSnapshot
        loop._config_watcher.create_snapshot.return_value = _make_snapshot({"tp_pct": 0.035})

        # state_manager.commit returns a state mock
        new_state = MagicMock()
        new_state.cycle_id = "new-cycle"
        loop._state_manager.commit.return_value = new_state

        # Replace _order_manager with a full MagicMock (it's a real object from __init__)
        loop._order_manager = MagicMock()
        loop._order_manager.place_entry_order.return_value = (MagicMock(), new_state)

        ctx = _make_ctx(Decimal("30000"))
        # _execute_enter calls dataclasses.replace(state, ...) — needs a real BotState
        state = BotState.initial("test_user", "test_bot", Decimal("1000"))

        loop._execute_enter(ctx, state, decision)

        assert loop._trailing_tp_high is None
