"""
Общие типы для пакета bot_loop.

FillEvent   — событие исполнения или статус-апдейт ордера от брокера.
              Производит IBroker.get_pending_fills() (дренаж очереди
              OrderTracker / PaperBroker).

OrderType   — роль ордера в цикле (ENTRY / TP / DCA).
OrderStatus — статус исполнения от биржи.

DecisionAction — перечень решений DecisionEngine.
Decision       — возвращаемое значение DecisionEngine.decide().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


# ---------------------------------------------------------------------------
# Типы ордеров и статусы
# ---------------------------------------------------------------------------


class OrderType(str, Enum):
    """Роль ордера в торговом цикле."""
    ENTRY = "ENTRY"
    TP    = "TP"
    DCA   = "DCA"


class OrderStatus(str, Enum):
    """Статус ордера, возвращаемый биржей."""
    PENDING           = "PENDING"
    FILLED            = "FILLED"
    PARTIALLY_FILLED  = "PARTIALLY_FILLED"
    CANCELLED         = "CANCELLED"
    REJECTED          = "REJECTED"
    UNKNOWN           = "UNKNOWN"   # ордер не найден на бирже


# ---------------------------------------------------------------------------
# Событие исполнения / статус-апдейт от брокера
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillEvent:
    """
    Событие исполнения или статус-изменения ордера.

    Производится OrderTracker (BybitBroker, приватный WebSocket)
    или синхронно PaperBroker при каждом create_order.

    Дренируется из брокерской очереди в начале тика через
    IBroker.get_pending_fills() и включается в TickContext.

    Поле order_type заполняется брокером или OrderManager при постановке
    в очередь — чтобы DecisionEngine знал что именно исполнилось.
    """

    exchange_order_id:  str
    client_order_id:    str
    status:             OrderStatus
    order_type:         OrderType
    filled_qty:         Decimal
    remaining_qty:      Decimal
    avg_fill_price:     Decimal | None   # None при CANCELLED / REJECTED
    commission:         Decimal
    timestamp_ms:       int              # Unix ms из ответа биржи

    @property
    def fill_pct(self) -> Decimal:
        """Процент исполнения (0–100). 0 если total_qty неизвестен."""
        total = self.filled_qty + self.remaining_qty
        if total == 0:
            return Decimal(0)
        return (self.filled_qty / total * 100).quantize(Decimal("0.01"))

    @property
    def is_fully_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def is_partial(self) -> bool:
        return self.status == OrderStatus.PARTIALLY_FILLED

    @property
    def is_cancelled(self) -> bool:
        return self.status == OrderStatus.CANCELLED

    @property
    def is_rejected(self) -> bool:
        return self.status == OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# Результат отмены ордера
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CancelResult:
    """
    Результат broker.cancel_order().

    confirmed=True означает что биржа подтвердила отмену.
    При confirmed=False CloseProtocol делает retry до CANCEL_MAX_RETRIES,
    после чего срабатывает STOP_CRANE.
    """
    order_id:  str
    confirmed: bool
    reason:    str = ""


# ---------------------------------------------------------------------------
# Решение DecisionEngine
# ---------------------------------------------------------------------------


class DecisionAction(str, Enum):
    """
    Перечень решений, которые DecisionEngine возвращает в bot_loop.

    WAIT               — ничего не делать в этом тике.
    ENTER              — выставить ордер на вход в позицию.
    PLACE_TP           — выставить TP (после входа или DCA).
    REPLACE_TP         — отменить текущий TP и выставить новый (после DCA).
    PLACE_DCA          — выставить DCA-ордер (LAZY: по уровню цены).
    PLACE_EAGER_DCA    — выставить все DCA-ордера сразу (EAGER: при входе).
    CANCEL_ENTRY       — отменить ордер на вход (timeout или отмена).
    CLOSE_PROTOCOL     — запустить 13-шаговый Close Protocol.
    FORCE_CLOSE        — закрыть позицию market-ордером (FORCE_CLOSE команда).
    SET_CLOSE_ONLY     — установить bot_configs.status=CLOSE_ONLY (ручная отмена TP/DCA).
    RETRY_LIQUIDITY    — повторить ордер после WAITING_FOR_LIQUIDITY паузы.
    STOP_CRANE         — заблокировать торговлю (неизвестный исход / аномалия).
    INITIATE_SL_CLOSE  — SL сработал: инициировать закрытие позиции по рынку.
                         Обрабатывается в bot_loop._execute_sl_close().
                         Close Protocol шаг 9 форсирует MARKET при
                         closing_reason=SL независимо от CLOSE_REMAINDER_MODE.
    """
    WAIT             = "WAIT"
    ENTER            = "ENTER"
    PLACE_TP         = "PLACE_TP"
    REPLACE_TP       = "REPLACE_TP"
    PLACE_DCA        = "PLACE_DCA"
    PLACE_EAGER_DCA  = "PLACE_EAGER_DCA"
    CANCEL_ENTRY     = "CANCEL_ENTRY"
    CLOSE_PROTOCOL   = "CLOSE_PROTOCOL"
    FORCE_CLOSE      = "FORCE_CLOSE"
    SET_CLOSE_ONLY   = "SET_CLOSE_ONLY"
    RETRY_LIQUIDITY  = "RETRY_LIQUIDITY"
    STOP_CRANE       = "STOP_CRANE"
    INITIATE_SL_CLOSE = "INITIATE_SL_CLOSE"


@dataclass(frozen=True)
class Decision:
    """
    Решение DecisionEngine для текущего тика.

    bot_loop.py выполняет решение в шаге 7 tick-sequence.
    DecisionEngine принимает решение — OrderManager / CloseProtocol
    / DCAScheduler его исполняют.

    Поля:
      action           — тип решения (см. DecisionAction).
      reason           — человекочитаемая причина (для логов и payload).
      entry_qty        — объём для ENTER / RETRY_LIQUIDITY.
      entry_price      — цена лимитного ордера на вход (None → MARKET).
      dca_qty          — объём DCA-ордера.
      dca_price        — цена DCA-ордера.
      tp_price         — цена TP-ордера.
      dca_levels       — уровни DCA для PLACE_EAGER_DCA (цена → объём).
      cancel_order_id  — exchange_order_id для CANCEL_ENTRY.
      stop_crane_error — исключение для STOP_CRANE (payload для emit).
    """

    action:           DecisionAction
    reason:           str

    # Параметры для ENTER / RETRY_LIQUIDITY
    entry_qty:        Decimal | None = None
    entry_price:      Decimal | None = None   # None → MARKET

    # Параметры для DCA
    dca_qty:          Decimal | None = None
    dca_price:        Decimal | None = None

    # Параметры для TP
    tp_price:         Decimal | None = None

    # Для PLACE_EAGER_DCA: (price → qty) в порядке от текущей цены вниз
    dca_levels:       tuple[tuple[Decimal, Decimal], ...] = field(default_factory=tuple)

    # Для CANCEL_ENTRY
    cancel_order_id:  str | None = None

    # Для STOP_CRANE
    stop_crane_error: "StopCraneError | None" = None  # type: ignore[name-defined]
