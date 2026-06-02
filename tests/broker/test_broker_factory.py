"""
tests/broker/test_broker_factory.py

Unit-тесты BrokerFactory.create().
"""
from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_broker_settings(
    broker_type: str = "paper",
    request_timeout_sec: float = 5.0,
    retry_delay_sec: float = 1.0,
    max_retries: int = 3,
    paper_initial_balance: float = 1000.0,
    paper_commission_pct: float = 0.1,
    paper_slippage_pct: float = 0.05,
):
    from config.settings import BrokerType
    s = MagicMock()
    s.broker_type           = BrokerType(broker_type)
    s.request_timeout_sec   = request_timeout_sec
    s.retry_delay_sec       = retry_delay_sec
    s.max_retries           = max_retries
    s.paper_initial_balance = paper_initial_balance
    s.paper_commission_pct  = paper_commission_pct
    s.paper_slippage_pct    = paper_slippage_pct
    return s


def make_settings(broker_type: str = "paper", **broker_overrides):
    s = MagicMock()
    s.broker = make_broker_settings(broker_type=broker_type, **broker_overrides)
    return s


def make_key_manager(keys: dict | None = None):
    km = MagicMock()
    store = keys or {}
    km.get.side_effect = lambda name: store.get(name, f"mock_{name}")
    return km


def make_emitter():
    return MagicMock()


def make_bybit_bundle(settings_kw=None, km=None, testnet_type="bybit"):
    """
    Создать bundle с полностью замоканным BybitBroker и BybitOrderTracker.
    Возвращает (bundle, mock_bybit_cls, mock_tracker_cls).
    """
    from broker.broker_factory import BrokerFactory

    s_kw = settings_kw or {}

    with patch("broker.broker_factory.BybitBroker") as mock_bybit_cls, \
         patch("broker.broker_factory.BybitOrderTracker") as mock_tracker_cls:

        mock_bybit_instance   = MagicMock()
        fake_tracker          = MagicMock()
        mock_bybit_cls.return_value   = mock_bybit_instance
        mock_tracker_cls.return_value = fake_tracker

        bundle = BrokerFactory.create(
            settings=make_settings(testnet_type, **s_kw),
            key_manager=km or make_key_manager(),
            emitter=make_emitter(),
            trade_repo=MagicMock(),
            bot_id="test_bot",
        )
        # Сохраняем call_args до выхода из with-блока
        bybit_call   = mock_bybit_cls.call_args
        tracker_call = mock_tracker_cls.call_args

    return bundle, bybit_call, tracker_call, fake_tracker


# ---------------------------------------------------------------------------
# BROKER_TYPE=paper
# ---------------------------------------------------------------------------

class TestCreatePaper:
    def test_returns_paper_broker(self):
        from broker.broker_factory import BrokerFactory
        from broker.paper_broker import PaperBroker

        bundle = BrokerFactory.create(
            settings=make_settings("paper"),
            key_manager=make_key_manager(),
            emitter=make_emitter(),
            trade_repo=MagicMock(),
            bot_id="test_bot",
        )
        assert isinstance(bundle.broker, PaperBroker)

    def test_tracker_is_none(self):
        from broker.broker_factory import BrokerFactory

        bundle = BrokerFactory.create(
            settings=make_settings("paper"),
            key_manager=make_key_manager(),
            emitter=make_emitter(),
            trade_repo=MagicMock(),
            bot_id="test_bot",
        )
        assert bundle.tracker is None

    def test_paper_initial_balance_passed(self):
        from broker.broker_factory import BrokerFactory

        bundle = BrokerFactory.create(
            settings=make_settings("paper", paper_initial_balance=2500.0),
            key_manager=make_key_manager(),
            emitter=make_emitter(),
            trade_repo=MagicMock(),
            bot_id="test_bot",
        )
        balance = bundle.broker.get_balance()
        # Balance.free — dict {asset: Decimal}
        assert balance.free["USDT"] == Decimal("2500.0")

    def test_key_manager_not_called_for_paper(self):
        from broker.broker_factory import BrokerFactory

        km = make_key_manager()
        BrokerFactory.create(
            settings=make_settings("paper"),
            key_manager=km,
            emitter=make_emitter(),
            trade_repo=MagicMock(),
            bot_id="test_bot",
        )
        km.get.assert_not_called()


# ---------------------------------------------------------------------------
# BROKER_TYPE=bybit
# ---------------------------------------------------------------------------

class TestCreateBybit:
    def test_creates_bybit_broker(self):
        bundle, bybit_call, _, _ = make_bybit_bundle(testnet_type="bybit")
        assert bybit_call is not None  # конструктор был вызван

    def test_bybit_testnet_false(self):
        _, bybit_call, _, _ = make_bybit_bundle(testnet_type="bybit")
        _, kwargs = bybit_call
        assert kwargs.get("testnet") is False

    def test_bybit_uses_live_keys(self):
        km = make_key_manager({
            "BYBIT_API_KEY":    "live_key",
            "BYBIT_API_SECRET": "live_secret",
        })
        make_bybit_bundle(testnet_type="bybit", km=km)
        requested = [c.args[0] for c in km.get.call_args_list]
        assert "BYBIT_API_KEY"    in requested
        assert "BYBIT_API_SECRET" in requested

    def test_bybit_does_not_use_testnet_keys(self):
        km = make_key_manager()
        make_bybit_bundle(testnet_type="bybit", km=km)
        requested = [c.args[0] for c in km.get.call_args_list]
        assert "BYBIT_TESTNET_API_KEY"    not in requested
        assert "BYBIT_TESTNET_API_SECRET" not in requested

    def test_bybit_passes_timeout_settings(self):
        _, bybit_call, _, _ = make_bybit_bundle(
            settings_kw={"request_timeout_sec": 7.5, "max_retries": 5},
            testnet_type="bybit",
        )
        _, kwargs = bybit_call
        assert kwargs.get("request_timeout_sec") == 7.5
        assert kwargs.get("max_retries") == 5

    def test_bybit_returns_tracker(self):
        bundle, _, _, fake_tracker = make_bybit_bundle(testnet_type="bybit")
        assert bundle.tracker is fake_tracker


# ---------------------------------------------------------------------------
# BROKER_TYPE=bybit_testnet
# ---------------------------------------------------------------------------

class TestCreateBybitTestnet:
    def test_creates_bybit_broker(self):
        _, bybit_call, _, _ = make_bybit_bundle(testnet_type="bybit_testnet")
        assert bybit_call is not None

    def test_bybit_testnet_flag_is_true(self):
        _, bybit_call, _, _ = make_bybit_bundle(testnet_type="bybit_testnet")
        _, kwargs = bybit_call
        assert kwargs.get("testnet") is True

    def test_bybit_testnet_uses_testnet_keys(self):
        km = make_key_manager({
            "BYBIT_TESTNET_API_KEY":    "testnet_key",
            "BYBIT_TESTNET_API_SECRET": "testnet_secret",
        })
        make_bybit_bundle(testnet_type="bybit_testnet", km=km)
        requested = [c.args[0] for c in km.get.call_args_list]
        assert "BYBIT_TESTNET_API_KEY"    in requested
        assert "BYBIT_TESTNET_API_SECRET" in requested

    def test_bybit_testnet_does_not_use_live_keys(self):
        km = make_key_manager()
        make_bybit_bundle(testnet_type="bybit_testnet", km=km)
        requested = [c.args[0] for c in km.get.call_args_list]
        assert "BYBIT_API_KEY"    not in requested
        assert "BYBIT_API_SECRET" not in requested

    def test_bybit_testnet_tracker_testnet_flag(self):
        _, _, tracker_call, _ = make_bybit_bundle(testnet_type="bybit_testnet")
        _, kwargs = tracker_call
        assert kwargs.get("testnet") is True

    def test_bybit_testnet_returns_tracker(self):
        bundle, _, _, fake_tracker = make_bybit_bundle(testnet_type="bybit_testnet")
        assert bundle.tracker is fake_tracker


# ---------------------------------------------------------------------------
# Неизвестный BROKER_TYPE
# ---------------------------------------------------------------------------

class TestCreateUnknown:
    def _make_unknown_settings(self, type_str: str):
        """
        Мок settings с произвольной строкой в broker_type.value.
        Не используем реальный BrokerType — его .value нельзя мутировать.
        """
        s = MagicMock()
        s.broker.broker_type.value = type_str
        return s

    def test_unknown_type_raises_value_error(self):
        from broker.broker_factory import BrokerFactory

        with pytest.raises(ValueError) as exc_info:
            BrokerFactory.create(
                settings=self._make_unknown_settings("unknown_broker"),
                key_manager=make_key_manager(),
                emitter=make_emitter(),
                trade_repo=MagicMock(),
                bot_id="test_bot",
            )
        msg = str(exc_info.value)
        assert "unknown_broker" in msg
        assert "bybit_testnet"  in msg

    def test_error_message_lists_all_valid_types(self):
        from broker.broker_factory import BrokerFactory

        with pytest.raises(ValueError) as exc_info:
            BrokerFactory.create(
                settings=self._make_unknown_settings("oops"),
                key_manager=make_key_manager(),
                emitter=make_emitter(),
                trade_repo=MagicMock(),
                bot_id="test_bot",
            )
        msg = str(exc_info.value)
        assert "paper"         in msg
        assert "bybit"         in msg
        assert "bybit_testnet" in msg


# ---------------------------------------------------------------------------
# BrokerType enum
# ---------------------------------------------------------------------------

class TestBrokerTypeEnum:
    def test_bybit_testnet_value(self):
        from config.settings import BrokerType
        assert BrokerType.BYBIT_TESTNET.value == "bybit_testnet"

    def test_all_three_types_exist(self):
        from config.settings import BrokerType
        types = {t.value for t in BrokerType}
        assert types == {"paper", "bybit", "bybit_testnet"}
