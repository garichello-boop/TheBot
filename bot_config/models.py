"""
bot_config/models.py

Data models for bot configuration (Point 5).

BotConfig          — immutable snapshot of a bot_configs row.
CycleSnapshot      — freezes strategy_params at the start of each trading cycle.
                     The bot works exclusively with this object during the cycle;
                     WFO updates to bot_configs do not affect the open position.
BotStatus          — valid values for the status column.
ConfigHistoryRow   — one row from bot_configs_history (audit trail).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BotStatus(str, Enum):
    """
    Lifecycle status stored in bot_configs.status.

    ACTIVE      — normal operation, new cycles open freely.
    CLOSE_ONLY  — finish current position, open no new cycles after it closes.
    STOPPED     — finish current cycle, then shut down cleanly.
    FORCE_CLOSE — close the open position with a market order immediately.
                  Set by the operator via UPDATE bot_configs.
                  Reset to ACTIVE or STOPPED after execution (Point 7).
    """
    ACTIVE      = "ACTIVE"
    CLOSE_ONLY  = "CLOSE_ONLY"
    STOPPED     = "STOPPED"
    FORCE_CLOSE = "FORCE_CLOSE"


# ---------------------------------------------------------------------------
# BotConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BotConfig:
    """
    Immutable representation of one bot_configs row.

    Loaded at startup and before each new trading cycle by ConfigWatcher.
    Never mutated in place — replaced entirely when config_version changes.

    strategy_params is a plain dict (deserialized from JSONB).
    virtual_balance is Decimal to match NUMERIC(20,8) in the DB.
    """
    user_id:         str
    bot_id:          str
    ticker:          str
    exchange:        str
    strategy_name:   str
    strategy_params: dict[str, Any]
    virtual_balance: Decimal
    status:          BotStatus
    config_version:  int
    created_at:      datetime
    updated_at:      datetime

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> BotConfig:
        """
        Build a BotConfig from a psycopg2 RealDictCursor row.

        RealDictCursor already deserializes JSONB to dict and TIMESTAMPTZ
        to timezone-aware datetime, so no extra parsing is needed for those.
        NUMERIC comes back as Decimal from psycopg2.
        """
        return cls(
            user_id         = row["user_id"],
            bot_id          = row["bot_id"],
            ticker          = row["ticker"],
            exchange        = row["exchange"],
            strategy_name   = row["strategy_name"],
            strategy_params = dict(row["strategy_params"] or {}),
            virtual_balance = (
                row["virtual_balance"]
                if isinstance(row["virtual_balance"], Decimal)
                else Decimal(str(row["virtual_balance"]))
            ),
            status         = BotStatus(row["status"]),
            config_version = int(row["config_version"]),
            created_at     = row["created_at"],
            updated_at     = row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        return self.status == BotStatus.ACTIVE

    def allows_new_cycles(self) -> bool:
        """True only when new trading cycles may be opened."""
        return self.status == BotStatus.ACTIVE

    def __repr__(self) -> str:
        return (
            f"BotConfig(bot_id={self.bot_id!r}, ticker={self.ticker!r}, "
            f"strategy={self.strategy_name!r}, status={self.status.value!r}, "
            f"version={self.config_version})"
        )


# ---------------------------------------------------------------------------
# CycleSnapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CycleSnapshot:
    """
    Immutable snapshot of strategy_params captured at the start of a cycle.

    The bot works exclusively with this object for the entire duration of
    the cycle. WFO may update bot_configs.strategy_params at any time —
    those changes are invisible to the running cycle and take effect only
    when the next cycle starts.

    Usage:
        snapshot = CycleSnapshot.from_config(config)
        value = snapshot.get("ma_period", default=180)
    """
    strategy_params: dict[str, Any]
    config_version:  int
    started_at:      datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: BotConfig) -> CycleSnapshot:
        """
        Capture a snapshot from the current BotConfig.
        Makes a shallow copy of strategy_params so later WFO updates
        to the dict do not leak into the snapshot.
        """
        return cls(
            strategy_params = dict(config.strategy_params),
            config_version  = config.config_version,
            started_at      = datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Param access
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a strategy parameter by name."""
        return self.strategy_params.get(key, default)

    def require(self, key: str) -> Any:
        """
        Retrieve a strategy parameter, raising KeyError if absent.
        Use for params that are mandatory for the strategy to function.
        """
        if key not in self.strategy_params:
            raise KeyError(
                f"CycleSnapshot: required param {key!r} not found "
                f"in strategy_params (version={self.config_version}). "
                f"Available: {list(self.strategy_params)}"
            )
        return self.strategy_params[key]

    def __repr__(self) -> str:
        return (
            f"CycleSnapshot(version={self.config_version}, "
            f"started_at={self.started_at.isoformat()}, "
            f"params={list(self.strategy_params)})"
        )


# ---------------------------------------------------------------------------
# ConfigHistoryRow
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigHistoryRow:
    """
    Immutable representation of one bot_configs_history row.

    Each row is a full snapshot of the bot_configs state captured by the
    audit trigger after an INSERT or UPDATE. The history is append-only and
    never modified after creation.

    Fields:
        id             — surrogate primary key (BIGSERIAL), useful for ordering.
        config_version — mirrors bot_configs.config_version at the moment of
                         the change. Use this value with rollback().
        changed_by     — who triggered the change:
                           'operator'               direct SQL / pgAdmin edit
                           'wfo'                    WFO script with SET LOCAL GUC
                           'bot'                    bot internal set_status()
                           'rollback_to_vN:actor'   rollback() call
                           'unknown'                no GUC set before the DML
        changed_at     — wall-clock time (UTC) when the trigger fired.

    Usage:
        history = repo.get_history("igor", "btc_paper_01", limit=10)
        for h in history:
            print(h.config_version, h.changed_by, h.strategy_params)
    """
    id:              int
    user_id:         str
    bot_id:          str
    config_version:  int
    ticker:          str
    strategy_name:   str
    strategy_params: dict[str, Any]
    virtual_balance: Decimal
    status:          BotStatus
    changed_by:      str
    changed_at:      datetime

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ConfigHistoryRow:
        """Build from a psycopg2 RealDictCursor row."""
        return cls(
            id              = int(row["id"]),
            user_id         = row["user_id"],
            bot_id          = row["bot_id"],
            config_version  = int(row["config_version"]),
            ticker          = row["ticker"],
            strategy_name   = row["strategy_name"],
            strategy_params = dict(row["strategy_params"] or {}),
            virtual_balance = (
                row["virtual_balance"]
                if isinstance(row["virtual_balance"], Decimal)
                else Decimal(str(row["virtual_balance"]))
            ),
            status     = BotStatus(row["status"]),
            changed_by = row["changed_by"],
            changed_at = row["changed_at"],
        )

    def __repr__(self) -> str:
        return (
            f"ConfigHistoryRow(id={self.id}, bot_id={self.bot_id!r}, "
            f"version={self.config_version}, changed_by={self.changed_by!r}, "
            f"changed_at={self.changed_at.isoformat()})"
        )
