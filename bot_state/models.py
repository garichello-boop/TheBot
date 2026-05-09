from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class CycleStatus(str, Enum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    IN_POSITION = "IN_POSITION"
    CLOSING = "CLOSING"
    WAITING_FOR_LIQUIDITY = "WAITING_FOR_LIQUIDITY"
    STOP_CRANE = "STOP_CRANE"


class OperationalStatus(str, Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class BotState:
    user_id: str
    bot_id: str
    version: int
    cycle_status: CycleStatus
    virtual_balance_free: Decimal
    virtual_balance_locked: Decimal
    position_qty: Decimal
    quote_spent: Decimal
    quote_received: Decimal
    active_dca_order_ids: tuple[str, ...]  # immutable, frozen=True requires hashable fields

    # Optional fields
    cycle_id: Optional[str] = None
    position_avg_price: Optional[Decimal] = None
    dca_count: int = 0
    last_applied_trade_id: Optional[str] = None
    active_entry_order_id: Optional[str] = None
    active_tp_order_id: Optional[str] = None
    pending_client_order_id: Optional[str] = None
    entered_at: Optional[datetime] = None
    last_order_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError(f"version must be >= 0, got {self.version}")
        if self.virtual_balance_free < 0:
            raise ValueError(
                f"virtual_balance_free must be >= 0, got {self.virtual_balance_free}"
            )
        if self.virtual_balance_locked < 0:
            raise ValueError(
                f"virtual_balance_locked must be >= 0, got {self.virtual_balance_locked}"
            )
        if self.position_qty < 0:
            raise ValueError(f"position_qty must be >= 0, got {self.position_qty}")

    @property
    def is_idle(self) -> bool:
        return self.cycle_status == CycleStatus.IDLE

    @property
    def has_position(self) -> bool:
        return self.position_qty > Decimal("0")

    @property
    def virtual_balance_total(self) -> Decimal:
        return self.virtual_balance_free + self.virtual_balance_locked

    def with_updates(self, **kwargs) -> "BotState":
        """Return new BotState with selected fields replaced. version auto-incremented."""
        current = {f: getattr(self, f) for f in self.__dataclass_fields__}
        current.update(kwargs)
        # Always bump version on any state change
        if "version" not in kwargs:
            current["version"] = self.version + 1
        # active_dca_order_ids: accept list, convert to tuple
        if isinstance(current.get("active_dca_order_ids"), list):
            current["active_dca_order_ids"] = tuple(current["active_dca_order_ids"])
        return BotState(**current)

    @classmethod
    def initial(cls, user_id: str, bot_id: str, virtual_balance: Decimal) -> "BotState":
        """Factory: fresh state for a new bot."""
        return cls(
            user_id=user_id,
            bot_id=bot_id,
            version=0,
            cycle_status=CycleStatus.IDLE,
            virtual_balance_free=virtual_balance,
            virtual_balance_locked=Decimal("0"),
            position_qty=Decimal("0"),
            quote_spent=Decimal("0"),
            quote_received=Decimal("0"),
            active_dca_order_ids=(),
        )

    @classmethod
    def from_row(cls, row: dict) -> "BotState":
        """Deserialize from psycopg2 RealDictCursor row."""
        dca_ids = row.get("active_dca_order_ids") or []
        return cls(
            user_id=row["user_id"],
            bot_id=row["bot_id"],
            version=row["version"],
            cycle_status=CycleStatus(row["cycle_status"]),
            virtual_balance_free=Decimal(str(row["virtual_balance_free"])),
            virtual_balance_locked=Decimal(str(row["virtual_balance_locked"])),
            position_qty=Decimal(str(row["position_qty"])),
            position_avg_price=(
                Decimal(str(row["position_avg_price"]))
                if row.get("position_avg_price") is not None
                else None
            ),
            dca_count=row.get("dca_count") or 0,
            quote_spent=Decimal(str(row["quote_spent"])),
            quote_received=Decimal(str(row["quote_received"])),
            last_applied_trade_id=row.get("last_applied_trade_id"),
            active_entry_order_id=row.get("active_entry_order_id"),
            active_tp_order_id=row.get("active_tp_order_id"),
            active_dca_order_ids=tuple(dca_ids),
            pending_client_order_id=row.get("pending_client_order_id"),
            cycle_id=row.get("cycle_id"),
            entered_at=row.get("entered_at"),
            last_order_at=row.get("last_order_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass(frozen=True)
class BotRegistry:
    user_id: str
    bot_id: str
    operational_status: OperationalStatus

    pid: Optional[int] = None
    last_heartbeat: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    error_message: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "BotRegistry":
        return cls(
            user_id=row["user_id"],
            bot_id=row["bot_id"],
            operational_status=OperationalStatus(row["operational_status"]),
            pid=row.get("pid"),
            last_heartbeat=row.get("last_heartbeat"),
            started_at=row.get("started_at"),
            stopped_at=row.get("stopped_at"),
            error_message=row.get("error_message"),
        )
