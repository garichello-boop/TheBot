"""
Test helpers for bot_state tests.
No project imports at module level — safe to use before sys.path setup.
"""
from decimal import Decimal


def make_state_row(**overrides) -> dict:
    """
    Return a dict that mimics a psycopg2 RealDictCursor row from bot_state.
    All fields present, all overridable.
    """
    row = {
        "user_id": "test_user",
        "bot_id": "test_bot",
        "version": 0,
        "cycle_id": None,
        "cycle_status": "IDLE",
        "virtual_balance_free": Decimal("1000.00"),
        "virtual_balance_locked": Decimal("0.00"),
        "position_qty": Decimal("0.00"),
        "position_avg_price": None,
        "dca_count": 0,
        "quote_spent": Decimal("0.00"),
        "quote_received": Decimal("0.00"),
        "last_applied_trade_id": None,
        "active_entry_order_id": None,
        "active_tp_order_id": None,
        "active_dca_order_ids": [],
        "pending_client_order_id": None,
        "entered_at": None,
        "last_order_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def make_registry_row(**overrides) -> dict:
    """
    Return a dict that mimics a psycopg2 RealDictCursor row from bot_registry.
    """
    row = {
        "user_id": "test_user",
        "bot_id": "test_bot",
        "operational_status": "STOPPED",
        "last_heartbeat": None,
        "pid": None,
        "started_at": None,
        "stopped_at": None,
        "error_message": None,
    }
    row.update(overrides)
    return row
