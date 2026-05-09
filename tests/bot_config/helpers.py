"""
tests/bot_config/helpers.py

Plain helper functions for bot_config tests.
No pytest fixtures, no bot_config imports — only stdlib.
Imported directly by test modules: `from helpers import make_row`

This file exists separately from conftest.py so that test modules can
import make_row without triggering any bot_config package initialization
at collection time.
"""

from datetime import datetime, timezone
from decimal import Decimal


def make_row(**overrides) -> dict:
    """Build a valid psycopg2 RealDictCursor-style row dict."""
    row = {
        "user_id":         "alex",
        "bot_id":          "btc_test",
        "ticker":          "BTCUSDT",
        "exchange":        "bybit",
        "strategy_name":   "MeanReversion",
        "strategy_params": {"ma_period": 180, "tp_pct": 0.035},
        "virtual_balance": Decimal("1000.00"),
        "status":          "ACTIVE",
        "config_version":  1,
        "created_at":      datetime.now(timezone.utc),
        "updated_at":      datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row
