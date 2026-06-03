"""
broker/paper_broker.py вЂ” PaperBroker: Р±СѓРјР°Р¶РЅР°СЏ С‚РѕСЂРіРѕРІР»СЏ.

РЎРёРјСѓР»РёСЂСѓРµС‚ СЂРµР°Р»СЊРЅСѓСЋ Р±РёСЂР¶Сѓ Р±РµР· СЂРµР°Р»СЊРЅС‹С… РґРµРЅРµРі. РЎРїРµС†РёР°Р»СЊРЅРѕ РЅР°СЃС‚СЂРѕРµРЅ
РїРµСЃСЃРёРјРёСЃС‚РёС‡РЅРµРµ СЂРµР°Р»СЊРЅРѕСЃС‚Рё вЂ” С‡С‚РѕР±С‹ СЂРµР°Р»СЊРЅС‹Рµ СЂРµР·СѓР»СЊС‚Р°С‚С‹ Р±С‹Р»Рё РЅРµ С…СѓР¶Рµ Paper.

Р¦РµРЅС‹ РёСЃРїРѕР»РЅРµРЅРёСЏ:
    BUY  MARKET в†’ ask + slippage   (РїР»Р°С‚РёРј Р±РѕР»СЊС€Рµ СЂС‹РЅРѕС‡РЅРѕРіРѕ вЂ” РїРµСЃСЃРёРјРёСЃС‚РёС‡РЅРѕ)
    SELL MARKET в†’ bid - slippage   (РїРѕР»СѓС‡Р°РµРј РјРµРЅСЊС€Рµ вЂ” РїРµСЃСЃРёРјРёСЃС‚РёС‡РЅРѕ)
    BUY  LIMIT  в†’ limit_price      (pending РґРѕ РґРѕСЃС‚РёР¶РµРЅРёСЏ С†РµРЅС‹)
    SELL LIMIT  в†’ limit_price      (pending РґРѕ РґРѕСЃС‚РёР¶РµРЅРёСЏ С†РµРЅС‹)

Р–РёР·РЅРµРЅРЅС‹Р№ С†РёРєР» РѕСЂРґРµСЂР° РІ РґРІСѓС… С‚РёРєР°С…:

    РўРёРє N:
        1. process_market_tick(bid, ask) в†’ РѕР±РЅРѕРІРёС‚СЊ С†РµРЅСѓ, Р·Р°РїРѕР»РЅРёС‚СЊ LIMIT
        2. DecisionEngine СЂРµС€Р°РµС‚ BUY в†’ create_order(MARKET BUY)
           в†’ PaperBroker: РёСЃРїРѕР»РЅРёС‚СЊ РЅРµРјРµРґР»РµРЅРЅРѕ, СЃРѕС…СЂР°РЅРёС‚СЊ fill РІРѕ РІРЅСѓС‚СЂРµРЅРЅРµР№ РѕС‡РµСЂРµРґРё
           в†’ РІРµСЂРЅСѓС‚СЊ OrderCreated(PENDING)  # per IBroker РєРѕРЅС‚СЂР°РєС‚
        3. commit + emit (Р±РµР· Р·РЅР°РЅРёСЏ Рѕ fill вЂ” РєР°Рє РЅР° СЂРµР°Р»СЊРЅРѕР№ Р±РёСЂР¶Рµ)

    РўРёРє N+1:
        1. get_pending_fills() в†’ РІРµСЂРЅСѓС‚СЊ РЅР°РєРѕРїР»РµРЅРЅС‹Рµ fills (РІРєР»СЋС‡Р°СЏ fill РёР· С‚РёРєР° N)
        2. TickContext РІРёРґРёС‚ fill в†’ FSM РїРµСЂРµС…РѕРґ ENTERING в†’ IN_POSITION

    Р­С‚Рѕ РїРѕРІРµРґРµРЅРёРµ РёРґРµРЅС‚РёС‡РЅРѕ СЂРµР°Р»СЊРЅРѕР№ Р±РёСЂР¶Рµ РіРґРµ WS-РїРѕРґС‚РІРµСЂР¶РґРµРЅРёРµ РїСЂРёС…РѕРґРёС‚
    Р°СЃРёРЅС…СЂРѕРЅРЅРѕ РїРѕСЃР»Рµ РІС‹СЃС‚Р°РІР»РµРЅРёСЏ РѕСЂРґРµСЂР°. BotLoop РЅРµ С‚СЂРµР±СѓРµС‚ РёР·РјРµРЅРµРЅРёР№.

Р РµРіРёСЃС‚СЂР°С†РёСЏ СЂРѕР»РµР№ РѕСЂРґРµСЂРѕРІ:
    OrderManager РІС‹Р·С‹РІР°РµС‚ register_order_role() РїРѕСЃР»Рµ create_order() С‡С‚РѕР±С‹
    PaperBroker Р·РЅР°Р» С‚РёРї РѕСЂРґРµСЂР° (ENTRY/TP/DCA) РїСЂРё С„РѕСЂРјРёСЂРѕРІР°РЅРёРё FillEvent.
    Р­С‚Рѕ РЅРµРѕР±С…РѕРґРёРјРѕ РґР»СЏ РєРѕСЂСЂРµРєС‚РЅРѕР№ С„РёР»СЊС‚СЂР°С†РёРё fills РІ TickContext.fills_for_entry/tp/dca.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from broker.broker import (
    BrokerError,
    BrokerRejected,
    IBroker,
    InsufficientFundsError,
    OrderNotFoundError,
)
from broker.models import (
    Balance,
    BrokerMode,
    HistoricalFill,
    MarketInfo,
    OpenOrder,
    OrderCreated,
    OrderFill,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)

# Runtime import: business_logic.types РЅРµ РёРјРїРѕСЂС‚РёСЂСѓРµС‚ broker РІ СЂР°РЅС‚Р°Р№РјРµ
# (С‚РѕР»СЊРєРѕ С‡РµСЂРµР· TYPE_CHECKING), РїРѕСЌС‚РѕРјСѓ С†РёРєР»РёС‡РµСЃРєРѕРіРѕ РёРјРїРѕСЂС‚Р° РЅРµС‚.
from business_logic.types import FillEvent
from business_logic.types import OrderType as FillOrderType
from business_logic.types import OrderStatus as FillOrderStatus

logger = logging.getLogger(__name__)

# (OrderRequest, locked_usdt) вЂ” locked_usdt > 0 С‚РѕР»СЊРєРѕ РґР»СЏ BUY LIMIT
_PendingEntry = Tuple[OrderRequest, Decimal]

# Р РѕР»СЊ РѕСЂРґРµСЂР° РІ С‚РѕСЂРіРѕРІРѕРј С†РёРєР»Рµ вЂ” РјР°РїРїРёРЅРі РЅР° FillOrderType
_ROLE_MAP: Dict[str, FillOrderType] = {
    "ENTRY": FillOrderType.ENTRY,
    "TP":    FillOrderType.TP,
    "DCA":   FillOrderType.DCA,
}


class PaperBroker(IBroker):
    """
    Р‘СѓРјР°Р¶РЅС‹Р№ Р±СЂРѕРєРµСЂ. Р”РµС„РѕР»С‚РЅС‹Р№ СЂРµР¶РёРј РїСЂРё СЃС‚Р°СЂС‚Рµ Р±РѕС‚Р° (BROKER_TYPE=paper).

    РЎРѕР·РґР°С‘С‚СЃСЏ С‡РµСЂРµР· BrokerFactory. РџРѕСЃР»Рµ 2-4 РЅРµРґРµР»СЊ СѓСЃРїРµС€РЅРѕР№ Р±СѓРјР°Р¶РЅРѕР№
    С‚РѕСЂРіРѕРІР»Рё РїРµСЂРµРєР»СЋС‡РёС‚СЊ РЅР° BybitBroker (BROKER_TYPE=bybit).
    """

    def __init__(
        self,
        initial_balance: Decimal,
        commission_pct: Decimal,
        slippage_pct: Decimal,
        emitter,       # observability.EventEmitter
        trade_repo,    # observability.TradeRepository
        bot_id: str,
    ) -> None:
        self._commission_pct = commission_pct
        self._slippage_pct = slippage_pct
        self._emitter = emitter
        self._trade_repo = trade_repo
        self._bot_id = bot_id

        # Р’РёСЂС‚СѓР°Р»СЊРЅС‹Р№ Р±Р°Р»Р°РЅСЃ РІ USDT
        self._free_usdt: Decimal = initial_balance
        self._locked_usdt: Decimal = Decimal("0")

        # Pending LIMIT РѕСЂРґРµСЂР°: exchange_order_id в†’ (request, locked_usdt)
        self._pending: Dict[str, _PendingEntry] = {}

        # РћС‡РµСЂРµРґСЊ fills РѕР¶РёРґР°СЋС‰РёС… Р·Р°Р±РѕСЂР° С‡РµСЂРµР· get_pending_fills()
        self._fill_queue: List[OrderFill] = []

        # Р РµРµСЃС‚СЂ СЂРѕР»РµР№: exchange_order_id в†’ "ENTRY" | "TP" | "DCA"
        # Р—Р°РїРѕР»РЅСЏРµС‚СЃСЏ С‡РµСЂРµР· register_order_role() РѕС‚ OrderManager.
        # РќРµРѕР±С…РѕРґРёРј РґР»СЏ РєРѕСЂСЂРµРєС‚РЅРѕРіРѕ FillEvent.order_type РїСЂРё get_pending_fills().
        self._order_roles: Dict[str, str] = {}

        # РўРµРєСѓС‰Р°СЏ С†РµРЅР° (РѕР±РЅРѕРІР»СЏРµС‚СЃСЏ С‡РµСЂРµР· process_market_tick РёР»Рё РїСЂРё MARKET orders)
        self._last_bid: Optional[Decimal] = None
        self._last_ask: Optional[Decimal] = None

        # РљСЌС€ market info (Р·Р°РґР°С‘С‚СЃСЏ С‡РµСЂРµР· set_market_info)
        self._market_info_cache: Dict[str, MarketInfo] = {}

        logger.info(
            "PaperBroker Р·Р°РїСѓС‰РµРЅ | Р±Р°Р»Р°РЅСЃ=%.2f USDT | РєРѕРјРёСЃСЃРёСЏ=%.4f%% | slippage=%.4f%%",
            initial_balance,
            float(commission_pct * 100),
            float(slippage_pct * 100),
        )

    # ------------------------------------------------------------------
    # IBroker вЂ” РїСѓР±Р»РёС‡РЅС‹Р№ РёРЅС‚РµСЂС„РµР№СЃ
    # ------------------------------------------------------------------

    def create_order(self, order: OrderRequest) -> OrderCreated:
        """
        РЎРѕР·РґР°С‚СЊ РѕСЂРґРµСЂ.

        MARKET в†’ РёСЃРїРѕР»РЅСЏРµС‚СЃСЏ РЅРµРјРµРґР»РµРЅРЅРѕ. Fill РїРѕРјРµС‰Р°РµС‚СЃСЏ РІ РѕС‡РµСЂРµРґСЊ
                 Рё Р±СѓРґРµС‚ РІРѕР·РІСЂР°С‰С‘РЅ РЅР° СЃР»РµРґСѓСЋС‰РµРј get_pending_fills().
        LIMIT  в†’ СЃРѕС…СЂР°РЅСЏРµС‚СЃСЏ РєР°Рє pending. РСЃРїРѕР»РЅРёС‚СЃСЏ РІ process_market_tick()
                 РёР»Рё get_pending_fills() РєРѕРіРґР° С†РµРЅР° РґРѕСЃС‚РёРіРЅРµС‚ СѓСЂРѕРІРЅСЏ.
        """
        if order.order_type == OrderType.MARKET:
            return self._fill_market_order(order)
        return self._place_limit_order(order)

    def cancel_order(self, order_id: str) -> bool:
        """
        РћС‚РјРµРЅРёС‚СЊ РѕСЂРґРµСЂ. РРґРµРјРїРѕС‚РµРЅС‚РµРЅ вЂ” True РµСЃР»Рё РѕСЂРґРµСЂР° РЅРµС‚.
        """
        if order_id not in self._pending:
            return True

        request, locked = self._pending.pop(order_id)
        self._order_roles.pop(order_id, None)
        if locked > Decimal("0"):
            self._unlock_usdt(locked)

        logger.info(
            "PaperBroker: РѕС‚РјРµРЅС‘РЅ %s %s (order_id=%s, СЂР°Р·Р±Р»РѕРєРёСЂРѕРІР°РЅРѕ=%.4f USDT)",
            request.side.value, request.ticker, order_id, float(locked),
        )
        return True

    def get_order_status(self, order_id: str) -> OrderStatus:
        """РЎС‚Р°С‚СѓСЃ РѕСЂРґРµСЂР°. РўРѕР»СЊРєРѕ РґР»СЏ reconciliation РЅР° СЃС‚Р°СЂС‚Рµ."""
        if order_id in self._pending:
            return OrderStatus.PENDING
        raise OrderNotFoundError(
            f"PaperBroker: РѕСЂРґРµСЂ {order_id} РЅРµ РЅР°Р№РґРµРЅ. "
            f"Р’РѕР·РјРѕР¶РЅРѕ СѓР¶Рµ РёСЃРїРѕР»РЅРµРЅ РёР»Рё РѕС‚РјРµРЅС‘РЅ."
        )

    def get_balance(self) -> Balance:
        """
        РўРµРєСѓС‰РёР№ РІРёСЂС‚СѓР°Р»СЊРЅС‹Р№ Р±Р°Р»Р°РЅСЃ Р±РѕС‚Р° РІ USDT.

        Р’РѕР·РІСЂР°С‰Р°РµС‚ Balance СЃ dict-РїРѕР»СЏРјРё {asset: amount},
        СЃРѕРІРјРµСЃС‚РёРјС‹Р№ СЃ BalanceReconciler.
        """
        return Balance(
            free={"USDT": self._free_usdt},
            locked={"USDT": self._locked_usdt},
        )

    def get_market_info(self, ticker: str) -> MarketInfo:
        """РўРѕСЂРіРѕРІС‹Рµ РѕРіСЂР°РЅРёС‡РµРЅРёСЏ РёРЅСЃС‚СЂСѓРјРµРЅС‚Р°."""
        if ticker in self._market_info_cache:
            return self._market_info_cache[ticker]

        logger.warning(
            "PaperBroker: MarketInfo РґР»СЏ %s РЅРµ Р·Р°РґР°РЅ вЂ” РёСЃРїРѕР»СЊР·СѓРµРј РґРµС„РѕР»С‚. "
            "Р’С‹Р·РѕРІРёС‚Рµ set_market_info() РїСЂРё СЃС‚Р°СЂС‚Рµ РґР»СЏ С‚РѕС‡РЅРѕР№ СЃРёРјСѓР»СЏС†РёРё.",
            ticker,
        )
        return MarketInfo(
            ticker=ticker,
            min_qty=Decimal("0.001"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("5"),
            price_precision=2,
            tick_size=Decimal("0.01"),
        )

    def get_open_orders(self, ticker: Optional[str] = None) -> List[OpenOrder]:
        """РЎРїРёСЃРѕРє Р°РєС‚РёРІРЅС‹С… (pending) РѕСЂРґРµСЂРѕРІ. Р”Р»СЏ reconciliation РЅР° СЃС‚Р°СЂС‚Рµ."""
        result = []
        for order_id, (request, _locked) in self._pending.items():
            if ticker is not None and request.ticker != ticker:
                continue
            result.append(OpenOrder(
                exchange_order_id=order_id,
                client_order_id=request.client_order_id,
                ticker=request.ticker,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                filled_qty=Decimal("0"),
                price=request.price,
                status=OrderStatus.PENDING,
                mode=BrokerMode.PAPER,
            ))
        return result

    def get_pending_fills(self) -> List[FillEvent]:
        """
        Р”СЂРµРЅРёСЂРѕРІР°С‚СЊ РІРЅСѓС‚СЂРµРЅРЅСЋСЋ РѕС‡РµСЂРµРґСЊ СЃРѕР±С‹С‚РёР№ РёСЃРїРѕР»РЅРµРЅРёСЏ.

        Р’С‹Р·С‹РІР°РµС‚СЃСЏ РІ РЅР°С‡Р°Р»Рµ РєР°Р¶РґРѕРіРѕ С‚РёРєР° РёР· TickContext.collect().

        Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕ: РїСЂРѕРІРµСЂСЏРµС‚ LIMIT РѕСЂРґРµСЂР° РїСЂРѕС‚РёРІ РїРѕСЃР»РµРґРЅРµР№ РёР·РІРµСЃС‚РЅРѕР№ С†РµРЅС‹
        (_last_bid/_last_ask). Р­С‚Рѕ РїРѕР·РІРѕР»СЏРµС‚ TP Рё DCA РѕСЂРґРµСЂР°Рј СЃСЂР°Р±Р°С‚С‹РІР°С‚СЊ
        РґР°Р¶Рµ Р±РµР· СЏРІРЅРѕРіРѕ РІС‹Р·РѕРІР° process_market_tick(), С…РѕС‚СЏ Рё СЃ Р·Р°РґРµСЂР¶РєРѕР№
        РІ РѕРґРёРЅ С‚РёРє (С†РµРЅР° РѕР±РЅРѕРІР»СЏРµС‚СЃСЏ РїСЂРё РёСЃРїРѕР»РЅРµРЅРёРё MARKET РѕСЂРґРµСЂРѕРІ).

        Р”Р»СЏ С‚РѕС‡РЅРѕР№ СЃРёРјСѓР»СЏС†РёРё: РІС‹Р·С‹РІР°С‚СЊ process_market_tick() СЏРІРЅРѕ
        РґРѕ get_pending_fills() СЃ Р°РєС‚СѓР°Р»СЊРЅС‹РјРё С†РµРЅР°РјРё.

        FillEvent.order_type Р·Р°РїРѕР»РЅСЏРµС‚СЃСЏ РёР· СЂРµРµСЃС‚СЂР° СЂРѕР»РµР№ (_order_roles).
        Р•СЃР»Рё СЂРѕР»СЊ РЅРµ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅР° вЂ” РґРµС„РѕР»С‚ ENTRY (СЃ WARNING).
        """
        # РџСЂРѕРІРµСЂРёС‚СЊ LIMIT РѕСЂРґРµСЂР° РµСЃР»Рё РµСЃС‚СЊ Р°РєС‚СѓР°Р»СЊРЅС‹Рµ С†РµРЅС‹
        if self._last_bid is not None and self._last_ask is not None:
            self._check_and_execute_limits(self._last_bid, self._last_ask)

        # РљРѕРЅРІРµСЂС‚РёСЂРѕРІР°С‚СЊ OrderFill в†’ FillEvent Рё РІРµСЂРЅСѓС‚СЊ
        result = []
        for fill in self._fill_queue:
            role_str = self._order_roles.get(fill.exchange_order_id)
            if role_str is None:
                logger.warning(
                    "PaperBroker.get_pending_fills(): СЂРѕР»СЊ РѕСЂРґРµСЂР° %s РЅРµ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅР° "
                    "вЂ” РёСЃРїРѕР»СЊР·СѓРµРј ENTRY. Р’С‹Р·РѕРІРёС‚Рµ register_order_role() РїРѕСЃР»Рµ create_order().",
                    fill.exchange_order_id,
                )
                role_str = "ENTRY"

            order_type = _ROLE_MAP.get(role_str, FillOrderType.ENTRY)
            status = (
                FillOrderStatus.PARTIALLY_FILLED
                if fill.is_partial
                else FillOrderStatus.FILLED
            )

            result.append(FillEvent(
                exchange_order_id=fill.exchange_order_id,
                client_order_id=fill.client_order_id,
                status=status,
                order_type=order_type,
                filled_qty=fill.filled_qty,
                remaining_qty=Decimal("0"),   # PaperBroker РІСЃРµРіРґР° РїРѕР»РЅРѕРµ РёСЃРїРѕР»РЅРµРЅРёРµ
                avg_fill_price=fill.avg_price,
                commission=fill.commission,
                timestamp_ms=int(fill.timestamp * 1000),
            ))

        self._fill_queue.clear()
        return result

    def get_fills(
        self,
        ticker: str,
        since_trade_id: Optional[str] = None,
    ) -> List[HistoricalFill]:
        """
        РСЃС‚РѕСЂРёС‡РµСЃРєР°СЏ Р»РµРЅС‚Р° СЃРґРµР»РѕРє вЂ” С‚РѕР»СЊРєРѕ РґР»СЏ reconciliation.

        PaperBroker РЅРµ С…СЂР°РЅРёС‚ РёСЃС‚РѕСЂРёСЋ fills вЂ” РІРѕР·РІСЂР°С‰Р°РµС‚ РїСѓСЃС‚РѕР№ СЃРїРёСЃРѕРє.
        РџРѕР»РЅР°СЏ СЂРµР°Р»РёР·Р°С†РёСЏ С‚СЂРµР±СѓРµС‚ С…СЂР°РЅРµРЅРёСЏ fills РІ TradeRepository
        СЃ trade_id РґР»СЏ РёРЅРєСЂРµРјРµРЅС‚Р°Р»СЊРЅРѕРіРѕ РґРѕСЃС‚СѓРїР°.
        """
        return []

    def get_mode(self) -> BrokerMode:
        return BrokerMode.PAPER

    # ------------------------------------------------------------------
    # Paper-specific API
    # ------------------------------------------------------------------

    def process_market_tick(self, bid: Decimal, ask: Decimal) -> List[OrderFill]:
        """
        РћР±РЅРѕРІРёС‚СЊ С†РµРЅСѓ Рё РїСЂРѕРІРµСЂРёС‚СЊ LIMIT РѕСЂРґРµСЂР°. Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃС‹СЂС‹Рµ OrderFill.

        РћСЃС‚Р°РІР»РµРЅ РґР»СЏ РѕР±СЂР°С‚РЅРѕР№ СЃРѕРІРјРµСЃС‚РёРјРѕСЃС‚Рё Рё СЏРІРЅРѕРіРѕ РІС‹Р·РѕРІР° РёР· С‚РµСЃС‚РѕРІ.
        Р’ РїСЂРѕРґР°РєС€РЅ tick-loop РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ get_pending_fills() вЂ” РѕРЅ РІС‹Р·С‹РІР°РµС‚
        _check_and_execute_limits() РІРЅСѓС‚СЂРё, РёСЃРїРѕР»СЊР·СѓСЏ СЃРѕС…СЂР°РЅС‘РЅРЅС‹Рµ С†РµРЅС‹.
        """
        self._last_bid = bid
        self._last_ask = ask
        self._check_and_execute_limits(bid, ask)
        # Fills РЅР°РєР°РїР»РёРІР°СЋС‚СЃСЏ РІ _fill_queue РґР»СЏ get_pending_fills().
        # process_market_tick() РЅРµ РґСЂРµРЅРёСЂСѓРµС‚ РѕС‡РµСЂРµРґСЊ вЂ” РґСЂРµРЅР°Р¶ С‚РѕР»СЊРєРѕ РІ get_pending_fills().
        return []

    def register_order_role(self, exchange_order_id: str, role: str) -> None:
        """
        Р—Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°С‚СЊ Р±РёР·РЅРµСЃ-СЂРѕР»СЊ РѕСЂРґРµСЂР° (ENTRY / TP / DCA).

        РћР±СЏР·Р°С‚РµР»СЊРЅРѕ РІС‹Р·С‹РІР°С‚СЊ РёР· OrderManager РїРѕСЃР»Рµ create_order():
            created = broker.create_order(request)
            broker.register_order_role(created.exchange_order_id, "TP")

        Р‘РµР· СЌС‚РѕРіРѕ get_pending_fills() РЅРµ СЃРјРѕР¶РµС‚ РїСЂР°РІРёР»СЊРЅРѕ Р·Р°РїРѕР»РЅРёС‚СЊ
        FillEvent.order_type, Рё TickContext.fills_for_tp/entry/dca РІРµСЂРЅСѓС‚
        РїСѓСЃС‚С‹Рµ РєРѕСЂС‚РµР¶Рё вЂ” FSM РЅРµ Р±СѓРґРµС‚ РґРІРёРіР°С‚СЊСЃСЏ.

        role: "ENTRY" | "TP" | "DCA"
        """
        if role not in _ROLE_MAP:
            logger.warning(
                "PaperBroker.register_order_role: РЅРµРёР·РІРµСЃС‚РЅР°СЏ СЂРѕР»СЊ %r "
                "(РѕР¶РёРґР°РµС‚СЃСЏ ENTRY/TP/DCA). order_id=%s",
                role, exchange_order_id,
            )
        self._order_roles[exchange_order_id] = role

    def set_market_info(self, info: MarketInfo) -> None:
        """Р—Р°РґР°С‚СЊ С‚РѕСЂРіРѕРІС‹Рµ РѕРіСЂР°РЅРёС‡РµРЅРёСЏ РёРЅСЃС‚СЂСѓРјРµРЅС‚Р°."""
        self._market_info_cache[info.ticker] = info
        logger.debug(
            "PaperBroker: MarketInfo Р·Р°РґР°РЅ РґР»СЏ %s "
            "(min_qty=%s, step_size=%s, min_notional=%s)",
            info.ticker, info.min_qty, info.step_size, info.min_notional,
        )

    # ------------------------------------------------------------------
    # Р’РЅСѓС‚СЂРµРЅРЅРёРµ РјРµС‚РѕРґС‹
    # ------------------------------------------------------------------

    def _check_and_execute_limits(self, bid: Decimal, ask: Decimal) -> None:
        """РќР°Р№С‚Рё Рё РёСЃРїРѕР»РЅРёС‚СЊ СЃСЂР°Р±РѕС‚Р°РІС€РёРµ LIMIT РѕСЂРґРµСЂР°."""
        triggered: List[Tuple[str, OrderRequest, Decimal, Decimal]] = []
        for order_id, (request, locked) in self._pending.items():
            fill_price = self._check_limit_trigger(request, bid, ask)
            if fill_price is not None:
                triggered.append((order_id, request, locked, fill_price))

        for order_id, request, locked, fill_price in triggered:
            del self._pending[order_id]
            self._execute_fill(
                order_id=order_id,
                request=request,
                execution_price=fill_price,
                locked_to_release=locked,
            )

    # ------------------------------------------------------------------
    # OHLCV playback support
    # ------------------------------------------------------------------

    def apply_downtime_tp_fill(
        self,
        order_id: str,
        tp_price: Decimal,
        ticker: str,
        qty: Decimal,
        bot_id: str,
        cycle_id: str,
    ) -> None:
        """
        Simulate a TP fill that happened while PaperBroker was offline.

        Called by StateRecovery when OHLCV klines confirm the TP price
        was reached during the downtime period.

        The simulated OrderFill is appended to _fill_queue. On the next
        call to get_pending_fills() the fill is returned, and normal
        BotLoop flow applies it: position_qty → 0, cycle → CLOSING → IDLE.

        Args:
            order_id: the lost TP order ID (from bot_state.active_tp_order_id).
            tp_price: limit price of the TP order (from bot_state.active_tp_price).
            ticker:   instrument symbol.
            qty:      position quantity being sold.
            bot_id:   bot identifier (for the fill event payload).
            cycle_id: cycle identifier (for the fill event payload).
        """
        request = OrderRequest(
            ticker=ticker,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=tp_price,
            client_order_id="",
            bot_id=bot_id,
            cycle_id=cycle_id,
        )
        # Register as TP so FillEvent gets correct order_type
        self._order_roles[order_id] = "TP"
        # Give _execute_fill a reference price
        self._last_bid = tp_price
        # Apply fill: updates _free_usdt and appends to _fill_queue
        self._execute_fill(
            order_id=order_id,
            request=request,
            execution_price=tp_price,
            locked_to_release=Decimal("0"),
        )
        logger.info(
            "PaperBroker.apply_downtime_tp_fill: simulated TP fill "
            "order=%s qty=%s @ %s (OHLCV playback)",
            order_id[:12], qty, tp_price,
        )

    def _fill_market_order(self, order: OrderRequest) -> OrderCreated:
        """РСЃРїРѕР»РЅРёС‚СЊ MARKET РѕСЂРґРµСЂ РЅРµРјРµРґР»РµРЅРЅРѕ РїРѕ ask/bid В± slippage."""
        if self._last_ask is None or self._last_bid is None:
            raise BrokerError(
                "PaperBroker: РЅРµС‚ С‚РµРєСѓС‰РµР№ С†РµРЅС‹ РґР»СЏ MARKET РѕСЂРґРµСЂР°. "
                "process_market_tick() РёР»Рё register_order_role() РґРѕР»Р¶РЅС‹ Р±С‹С‚СЊ РІС‹Р·РІР°РЅС‹ СЃРЅР°С‡Р°Р»Р°."
            )

        execution_price = self._market_execution_price(order.side)
        order_id = f"paper_{uuid4().hex[:12]}"

        if order.side == OrderSide.BUY:
            total_cost = order.quantity * execution_price * (1 + self._commission_pct)
            if total_cost > self._free_usdt:
                raise InsufficientFundsError(
                    f"PaperBroker: РЅРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ USDT РґР»СЏ BUY MARKET. "
                    f"РќСѓР¶РЅРѕ {float(total_cost):.4f}, РґРѕСЃС‚СѓРїРЅРѕ {float(self._free_usdt):.4f}"
                )

        self._execute_fill(
            order_id=order_id,
            request=order,
            execution_price=execution_price,
            locked_to_release=Decimal("0"),
        )

        return OrderCreated(
            exchange_order_id=order_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.PENDING,
            mode=BrokerMode.PAPER,
        )

    def _place_limit_order(self, order: OrderRequest) -> OrderCreated:
        """РџРѕСЃС‚Р°РІРёС‚СЊ LIMIT РѕСЂРґРµСЂ РІ pending."""
        if order.price is None:
            raise BrokerRejected(
                f"PaperBroker: LIMIT РѕСЂРґРµСЂ {order.client_order_id} Р±РµР· С†РµРЅС‹"
            )

        order_id = f"paper_{uuid4().hex[:12]}"
        lock_amount = Decimal("0")

        if order.side == OrderSide.BUY:
            lock_amount = order.quantity * order.price * (1 + self._commission_pct)
            if lock_amount > self._free_usdt:
                raise InsufficientFundsError(
                    f"PaperBroker: РЅРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ USDT РґР»СЏ BUY LIMIT. "
                    f"РќСѓР¶РЅРѕ {float(lock_amount):.4f}, РґРѕСЃС‚СѓРїРЅРѕ {float(self._free_usdt):.4f}"
                )
            self._lock_usdt(lock_amount)

        self._pending[order_id] = (order, lock_amount)

        logger.debug(
            "PaperBroker LIMIT pending: %s %s qty=%s @ %s "
            "(order_id=%s, locked=%.4f USDT)",
            (order.side if isinstance(order.side, str) else order.side.value), order.ticker,
            order.quantity, order.price,
            order_id, float(lock_amount),
        )

        return OrderCreated(
            exchange_order_id=order_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.PENDING,
            mode=BrokerMode.PAPER,
        )

    def _check_limit_trigger(
        self,
        request: OrderRequest,
        bid: Decimal,
        ask: Decimal,
    ) -> Optional[Decimal]:
        """
        РџСЂРѕРІРµСЂРёС‚СЊ РґРѕСЃС‚РёРіРЅСѓС‚ Р»Рё СѓСЂРѕРІРµРЅСЊ LIMIT РѕСЂРґРµСЂР°.

        BUY  LIMIT: РёСЃРїРѕР»РЅСЏРµС‚СЃСЏ РµСЃР»Рё ask <= limit_price
        SELL LIMIT: РёСЃРїРѕР»РЅСЏРµС‚СЃСЏ РµСЃР»Рё bid >= limit_price
        """
        if request.price is None:
            return None

        if request.side == OrderSide.BUY and ask <= request.price:
            return request.price
        if request.side == OrderSide.SELL and bid >= request.price:
            return request.price

        return None

    def _execute_fill(
        self,
        order_id: str,
        request: OrderRequest,
        execution_price: Decimal,
        locked_to_release: Decimal,
    ) -> None:
        """
        РџСЂРёРјРµРЅРёС‚СЊ РёСЃРїРѕР»РЅРµРЅРёРµ РѕСЂРґРµСЂР°: РѕР±РЅРѕРІРёС‚СЊ Р±Р°Р»Р°РЅСЃ, Р·Р°РїРёСЃР°С‚СЊ РІ Р‘Р”,
        СЌРјРёС‚РёСЂРѕРІР°С‚СЊ ORDER_FILLED, РґРѕР±Р°РІРёС‚СЊ РІ РѕС‡РµСЂРµРґСЊ.
        """
        commission = request.quantity * execution_price * self._commission_pct
        ts = time.time()
        side_str = request.side if isinstance(request.side, str) else request.side.value

        if locked_to_release > Decimal("0"):
            self._unlock_usdt(locked_to_release)

        if side_str == "BUY":
            total_cost = request.quantity * execution_price + commission
            self._free_usdt -= total_cost
            # РћР±РЅРѕРІРёС‚СЊ РєСЌС€ С†РµРЅ РёР· С„Р°РєС‚РёС‡РµСЃРєРѕР№ С†РµРЅС‹ РёСЃРїРѕР»РЅРµРЅРёСЏ
            self._last_ask = execution_price
        else:
            proceeds = request.quantity * execution_price - commission
            self._free_usdt += proceeds
            self._last_bid = execution_price

        fill = OrderFill(
            exchange_order_id=order_id,
            client_order_id=request.client_order_id,
            ticker=request.ticker,
            side=request.side,
            filled_qty=request.quantity,
            avg_price=execution_price,
            commission=commission,
            mode=BrokerMode.PAPER,
            timestamp=ts,
            is_partial=False,
        )

        if self._trade_repo is not None:
            try:
                self._trade_repo.save_fill(fill, bot_id=self._bot_id)
            except Exception as exc:
                logger.error(
                    "PaperBroker: РѕС€РёР±РєР° СЃРѕС…СЂР°РЅРµРЅРёСЏ fill РІ Р‘Р” (order_id=%s): %s",
                    order_id, exc,
                )

        self._emitter.emit(
            event_type="ORDER_FILLED",
            level="INFO",
            message=(
                f"[PAPER] {side_str} {request.quantity} {request.ticker} "
                f"@ {execution_price:.4f} | РєРѕРјРёСЃСЃРёСЏ={commission:.4f}"
            ),
            payload={
                "exchange_order_id": order_id,
                "client_order_id": request.client_order_id,
                "side": side_str,
                "filled_qty": str(request.quantity),
                "avg_price": str(execution_price),
                "commission": str(commission),
                "mode": "PAPER",
                "cycle_id": request.cycle_id,
                "bot_id": request.bot_id,
            },
        )

        self._fill_queue.append(fill)

        logger.info(
            "PaperBroker fill: %s %s qty=%s @ %s | РєРѕРјРёСЃСЃРёСЏ=%s | "
            "Р±Р°Р»Р°РЅСЃ=%.4f free / %.4f locked",
            side_str, request.ticker,
            request.quantity, execution_price, commission,
            float(self._free_usdt), float(self._locked_usdt),
        )

    def _market_execution_price(self, side: OrderSide) -> Decimal:
        """Р¦РµРЅР° РёСЃРїРѕР»РЅРµРЅРёСЏ MARKET СЃ РїРµСЃСЃРёРјРёСЃС‚РёС‡РЅС‹Рј slippage."""
        if side == OrderSide.BUY:
            return self._last_ask * (1 + self._slippage_pct)
        return self._last_bid * (1 - self._slippage_pct)

    def _lock_usdt(self, amount: Decimal) -> None:
        self._free_usdt -= amount
        self._locked_usdt += amount

    def _unlock_usdt(self, amount: Decimal) -> None:
        self._locked_usdt -= amount
        self._free_usdt += amount