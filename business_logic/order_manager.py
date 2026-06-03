"""
OrderManager вЂ” Р¶РёР·РЅРµРЅРЅС‹Р№ С†РёРєР» РѕСЂРґРµСЂРѕРІ.

РџСЂРёРЅС†РёРїС‹ РёР· РўР—:
  - Р Р°Р±РѕС‚Р°РµС‚ С‚РѕР»СЊРєРѕ СЃ client_order_id (UUID) РІРЅСѓС‚СЂРё Р±РѕС‚Р°.
  - РњР°РїРїРёРЅРі РІ РїРѕР»Рµ РєРѕРЅРєСЂРµС‚РЅРѕР№ Р±РёСЂР¶Рё (orderLinkId / newClientOrderId)
    РґРµР»Р°РµС‚ ExchangeAdapter вЂ” OrderManager РѕР± СЌС‚РѕРј РЅРµ Р·РЅР°РµС‚.
  - pending_client_order_id СЃРѕС…СЂР°РЅСЏРµС‚СЃСЏ РІ bot_state Р”Рћ РѕС‚РїСЂР°РІРєРё РЅР° Р±РёСЂР¶Сѓ.
    Р­С‚Рѕ РїРѕР·РІРѕР»СЏРµС‚ reconciliation РЅР°Р№С‚Рё РѕСЂРґРµСЂ РїСЂРё СЂРµСЃС‚Р°СЂС‚Рµ РґР°Р¶Рµ РµСЃР»Рё
    Р±РѕС‚ СѓРїР°Р» РјРµР¶РґСѓ send Рё save.
  - РўР°Р№РјР°СѓС‚ create_order = BROKER_REQUEST_TIMEOUT_SEC (5 СЃРµРє).
    РџСЂРё С‚Р°Р№РјР°СѓС‚Рµ вЂ” РЅРµРјРµРґР»РµРЅРЅРѕ StopCraneError (РёСЃС…РѕРґ РЅРµРёР·РІРµСЃС‚РµРЅ, РЅРµ РїРѕРІС‚РѕСЂСЏС‚СЊ).
  - Retry РїСЂРё СЏРІРЅС‹С… СЃРµС‚РµРІС‹С… РѕС€РёР±РєР°С… (РЅРµ С‚Р°Р№РјР°СѓС‚): СЌРєСЃРїРѕРЅРµРЅС†РёР°Р»СЊРЅР°СЏ Р·Р°РґРµСЂР¶РєР°
    СЃ jitter (1в†’2в†’4СЃ, max BROKER_MAX_RETRIES).
  - Cancel РѕСЂРґРµСЂРѕРІ вЂ” РќРРљРћР“Р”Рђ РЅРµ StopCraneError. Retry РґРѕ CANCEL_MAX_RETRIES,
    РїРѕСЃР»Рµ в†’ StopCraneError (РЅРµР»СЊР·СЏ РїРµСЂРµР№С‚Рё РІ IDLE СЃ РіСЂСЏР·РЅРѕР№ РїРѕР·РёС†РёРµР№).

Р—Р°РІРёСЃРёРјРѕСЃС‚Рё:
  IBroker.create_order(request)    в†’ OrderCreated
  IBroker.cancel_order(order_id)   в†’ CancelResult
  StateManager.commit(old, new)    в†’ BotState  (two-phase commit)
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from .errors import StopCraneError, InsufficientFundsError
from .types import CancelResult, OrderType

if TYPE_CHECKING:
    from broker import IBroker, OrderRequest, OrderCreated
    from bot_state import BotState, StateManager
    from observability import EventEmitter

logger = logging.getLogger(__name__)


class OrderManager:
    """
    РЈРїСЂР°РІР»СЏРµС‚ РїРѕСЃС‚Р°РЅРѕРІРєРѕР№ Рё РѕС‚РјРµРЅРѕР№ РѕСЂРґРµСЂРѕРІ.

    РљР°Р¶РґС‹Р№ РјРµС‚РѕРґ:
      1. Р“РµРЅРµСЂРёСЂСѓРµС‚ client_order_id (UUID).
      2. РЎРѕС…СЂР°РЅСЏРµС‚ pending_client_order_id РІ bot_state С‡РµСЂРµР· StateManager.commit().
      3. Р’С‹Р·С‹РІР°РµС‚ broker.create_order() / cancel_order().
      4. РџСЂРё СѓСЃРїРµС…Рµ вЂ” СЃРѕС…СЂР°РЅСЏРµС‚ exchange_order_id, РѕС‡РёС‰Р°РµС‚ pending.
      5. РџСЂРё С‚Р°Р№РјР°СѓС‚Рµ create_order вЂ” StopCraneError (Р±РµР· retry).
      6. РџСЂРё СЃРµС‚РµРІРѕР№ РѕС€РёР±РєРµ вЂ” СЌРєСЃРїРѕРЅРµРЅС†РёР°Р»СЊРЅС‹Р№ retry СЃ jitter.
    """

    # INSUFFICIENT_FUNDS РєРѕРґС‹ РѕС‚ РёР·РІРµСЃС‚РЅС‹С… Р±РёСЂР¶ (РїРѕРїРѕР»РЅСЏРµС‚СЃСЏ РїСЂРё РёРЅС‚РµРіСЂР°С†РёРё)
    _INSUFFICIENT_FUNDS_REASONS = frozenset({
        "insufficient_balance",
        "insufficient_fund",
        "not enough balance",
        "account balance not enough",
        "insufficient_margin",
        "order cost not available",
    })

    def __init__(
        self,
        broker: "IBroker",
        state_manager: "StateManager",
        emitter: "EventEmitter",
        *,
        broker_request_timeout_sec: float = 5.0,
        broker_retry_delay_sec: float = 1.0,
        broker_max_retries: int = 3,
        cancel_max_retries: int = 5,
    ) -> None:
        self._broker                   = broker
        self._state_manager            = state_manager
        self._emitter                  = emitter
        self._request_timeout          = broker_request_timeout_sec
        self._retry_delay              = broker_retry_delay_sec
        self._max_retries              = broker_max_retries
        self._cancel_max_retries       = cancel_max_retries

    # ------------------------------------------------------------------
    # РџРѕСЃС‚Р°РЅРѕРІРєР° РѕСЂРґРµСЂРѕРІ
    # ------------------------------------------------------------------

    def place_entry_order(
        self,
        state: "BotState",
        *,
        qty: Decimal,
        price: Decimal | None,    # None в†’ MARKET
        ticker: str,
        cycle_id: str,
    ) -> tuple["OrderCreated", "BotState"]:
        """
        Р’С‹СЃС‚Р°РІРёС‚СЊ РѕСЂРґРµСЂ РЅР° РІС…РѕРґ РІ РїРѕР·РёС†РёСЋ.

        Returns:
          (OrderCreated, new_state) вЂ” new_state СЃРѕРґРµСЂР¶РёС‚ СЃРѕС…СЂР°РЅС‘РЅРЅС‹Р№
          exchange_order_id РІ active_entry_order_id.

        Raises:
          StopCraneError          вЂ” С‚Р°Р№РјР°СѓС‚ (РёСЃС…РѕРґ РЅРµРёР·РІРµСЃС‚РµРЅ).
          InsufficientFundsError  вЂ” РЅРµС…РІР°С‚РєР° СЃСЂРµРґСЃС‚РІ.
        """
        client_id = self._generate_client_id()
        order_type = "LIMIT" if price is not None else "MARKET"

        # Phase 1: СЃРѕС…СЂР°РЅСЏРµРј pending_client_order_id Р”Рћ РѕС‚РїСЂР°РІРєРё
        new_state = self._state_manager.commit(
            state,
            replace(state, pending_client_order_id=client_id),
        )

        # РЎС‚СЂРѕРёРј OrderRequest (РёРЅС‚РµСЂС„РµР№СЃ РёР· Рџ4)
        request = self._build_request(
            ticker=ticker,
            side="BUY",
            order_type=order_type,
            quantity=qty,
            price=price,
            client_order_id=client_id,
            bot_id=state.bot_id,
            cycle_id=cycle_id,
        )

        created = self._send_order(request, order_role=OrderType.ENTRY)

        # Р РµРіРёСЃС‚СЂРёСЂСѓРµРј СЂРѕР»СЊ РѕСЂРґРµСЂР° РґР»СЏ PaperBroker.get_pending_fills()
        # Duck-typing: BybitBroker РЅРµ СЂРµР°Р»РёР·СѓРµС‚ СЌС‚РѕС‚ РјРµС‚РѕРґ вЂ” РІС‹Р·РѕРІ РїСЂРѕРїСѓСЃРєР°РµС‚СЃСЏ.
        if hasattr(self._broker, "register_order_role"):
            self._broker.register_order_role(created.exchange_order_id, "ENTRY")

        # Phase 2: СЃРѕС…СЂР°РЅСЏРµРј exchange_order_id
        new_state = self._state_manager.commit(
            new_state,
            replace(
                new_state,
                active_entry_order_id=created.exchange_order_id,
                pending_client_order_id=None,
            ),
        )

        self._emitter.emit(
            event_type="ORDER_CREATED",
            level="INFO",
            message=f"Entry order СЂР°Р·РјРµС‰С‘РЅ: {qty} {ticker} @ {price or 'MARKET'}",
            payload={
                "order_id": created.exchange_order_id,
                "client_order_id": client_id,
                "side": "BUY",
                "qty": str(qty),
                "price": str(price) if price else "MARKET",
                "order_type": order_type,
                "role": "ENTRY",
            },
        )
        return created, new_state

    def place_tp_order(
        self,
        state: "BotState",
        *,
        qty: Decimal,
        price: Decimal,
        ticker: str,
        cycle_id: str,
    ) -> tuple["OrderCreated", "BotState"]:
        """
        Р’С‹СЃС‚Р°РІРёС‚СЊ TP-РѕСЂРґРµСЂ (LIMIT SELL).

        РџРµСЂРµРґ РїРѕСЃС‚Р°РЅРѕРІРєРѕР№ РїСЂРѕРІРµСЂСЏРµС‚ РЅРµС‚ Р»Рё СѓР¶Рµ Р°РєС‚РёРІРЅРѕРіРѕ TP РїРѕ С‚РёРєРµСЂСѓ
        вЂ” Р·Р°С‰РёС‚Р° РѕС‚ РґСѓР±Р»РёСЂРѕРІР°РЅРёСЏ РїСЂРё СЂРµСЃС‚Р°СЂС‚Рµ.
        """
        if state.active_tp_order_id is not None:
            logger.warning(
                "place_tp_order: active_tp_order_id=%s СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚. "
                "РџСЂРѕРїСѓСЃРєР°РµРј РїРѕСЃС‚Р°РЅРѕРІРєСѓ РґСѓР±Р»СЏ.",
                state.active_tp_order_id,
            )
            # Р’РѕР·РІСЂР°С‰Р°РµРј Р·Р°РіР»СѓС€РєСѓ вЂ” РІС‹Р·С‹РІР°СЋС‰РёР№ РєРѕРґ РґРѕР»Р¶РµРЅ РїСЂРѕРІРµСЂСЏС‚СЊ
            from broker import OrderCreated  # noqa: PLC0415
            stub = OrderCreated(
                exchange_order_id=state.active_tp_order_id,
                client_order_id="",
                status="PENDING",
                mode=self._broker.get_mode(),
            )
            return stub, state

        client_id = self._generate_client_id()

        new_state = self._state_manager.commit(
            state,
            replace(state, pending_client_order_id=client_id),
        )

        request = self._build_request(
            ticker=ticker,
            side="SELL",
            order_type="LIMIT",
            quantity=qty,
            price=price,
            client_order_id=client_id,
            bot_id=state.bot_id,
            cycle_id=cycle_id,
        )

        created = self._send_order(request, order_role=OrderType.TP)

        if hasattr(self._broker, "register_order_role"):
            self._broker.register_order_role(created.exchange_order_id, "TP")

        new_state = self._state_manager.commit(
            new_state,
            replace(
                new_state,
                active_tp_order_id=created.exchange_order_id,
                active_tp_price=price,   # persisted for OHLCV playback on restart
                pending_client_order_id=None,
            ),
        )

        self._emitter.emit(
            event_type="TP_CREATED",
            level="INFO",
            message=f"TP РІС‹СЃС‚Р°РІР»РµРЅ: {qty} {ticker} @ {price}",
            payload={
                "order_id": created.exchange_order_id,
                "client_order_id": client_id,
                "qty": str(qty),
                "price": str(price),
            },
        )
        return created, new_state

    def place_dca_order(
        self,
        state: "BotState",
        *,
        qty: Decimal,
        price: Decimal | None,
        ticker: str,
        cycle_id: str,
    ) -> tuple["OrderCreated", "BotState"]:
        """Р’С‹СЃС‚Р°РІРёС‚СЊ DCA-РѕСЂРґРµСЂ (BUY LIMIT РёР»Рё MARKET)."""
        client_id = self._generate_client_id()
        order_type = "LIMIT" if price is not None else "MARKET"

        new_state = self._state_manager.commit(
            state,
            replace(state, pending_client_order_id=client_id),
        )

        request = self._build_request(
            ticker=ticker,
            side="BUY",
            order_type=order_type,
            quantity=qty,
            price=price,
            client_order_id=client_id,
            bot_id=state.bot_id,
            cycle_id=cycle_id,
        )

        created = self._send_order(request, order_role=OrderType.DCA)

        if hasattr(self._broker, "register_order_role"):
            self._broker.register_order_role(created.exchange_order_id, "DCA")

        # Р”РѕР±Р°РІР»СЏРµРј Рє СЃРїРёСЃРєСѓ Р°РєС‚РёРІРЅС‹С… DCA-РѕСЂРґРµСЂРѕРІ (РґР»СЏ EAGER-СЂРµР¶РёРјР°)
        updated_dca_ids = (*state.active_dca_order_ids, created.exchange_order_id)

        new_state = self._state_manager.commit(
            new_state,
            replace(
                new_state,
                active_dca_order_ids=updated_dca_ids,
                pending_client_order_id=None,
            ),
        )

        self._emitter.emit(
            event_type="ORDER_CREATED",
            level="INFO",
            message=f"DCA order: {qty} {ticker} @ {price or 'MARKET'}",
            payload={
                "order_id": created.exchange_order_id,
                "client_order_id": client_id,
                "qty": str(qty),
                "price": str(price) if price else "MARKET",
                "role": "DCA",
                "dca_count_after": state.dca_count + 1,
            },
        )
        return created, new_state

    # ------------------------------------------------------------------
    # РћС‚РјРµРЅР° РѕСЂРґРµСЂРѕРІ
    # ------------------------------------------------------------------

    def cancel_order(
        self,
        order_id: str,
        *,
        order_role: str = "UNKNOWN",
    ) -> CancelResult:
        """
        РћС‚РјРµРЅРёС‚СЊ РѕСЂРґРµСЂ. Retry РґРѕ cancel_max_retries СЃ СЌРєСЃРїРѕРЅРµРЅС†РёР°Р»СЊРЅРѕР№ Р·Р°РґРµСЂР¶РєРѕР№.

        Raises:
          StopCraneError вЂ” РёСЃС‡РµСЂРїР°РЅС‹ РІСЃРµ РїРѕРїС‹С‚РєРё (РЅРµР»СЊР·СЏ РїРµСЂРµР№С‚Рё РІ IDLE).
        """
        from .errors import StopCraneError as _StopCraneError  # noqa: PLC0415

        last_error: Exception | None = None

        for attempt in range(self._cancel_max_retries):
            try:
                result: CancelResult = self._broker.cancel_order(order_id)
                if result.confirmed:
                    self._emitter.emit(
                        event_type="ORDER_CANCELLED",
                        level="WARNING",
                        message=f"РћСЂРґРµСЂ {order_id} ({order_role}) РѕС‚РјРµРЅС‘РЅ Р±РѕС‚РѕРј",
                        payload={
                            "order_id": order_id,
                            "initiated_by": "bot",
                            "role": order_role,
                            "attempt": attempt + 1,
                        },
                    )
                    return result

                # Р‘РёСЂР¶Р° РІРµСЂРЅСѓР»Р° not-confirmed вЂ” retry
                logger.warning(
                    "cancel_order РЅРµ РїРѕРґС‚РІРµСЂР¶РґС‘РЅ Р±РёСЂР¶РµР№ (РїРѕРїС‹С‚РєР° %d/%d): %s",
                    attempt + 1, self._cancel_max_retries, order_id,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "РћС€РёР±РєР° РїСЂРё cancel_order (РїРѕРїС‹С‚РєР° %d/%d): %s вЂ” %s",
                    attempt + 1, self._cancel_max_retries, order_id, exc,
                )

            if attempt < self._cancel_max_retries - 1:
                delay = self._retry_delay * (2 ** attempt) + random.uniform(0, 0.3)
                time.sleep(delay)

        raise _StopCraneError(
            f"РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґС‚РІРµСЂРґРёС‚СЊ РѕС‚РјРµРЅСѓ РѕСЂРґРµСЂР° {order_id} Р·Р° "
            f"{self._cancel_max_retries} РїРѕРїС‹С‚РѕРє",
            invariant="cancel_confirmed_before_idle",
            expected={"order_id": order_id, "status": "CANCELLED"},
            actually_found={"confirmed": False, "error": str(last_error)},
            db_state={"order_id": order_id, "role": order_role},
        )

    def cancel_all_dca(
        self,
        state: "BotState",
        cycle_id: str,
    ) -> "BotState":
        """
        Mass-cancel РІСЃРµС… Р°РєС‚РёРІРЅС‹С… DCA-РѕСЂРґРµСЂРѕРІ С‚РµРєСѓС‰РµРіРѕ С†РёРєР»Р°.

        РСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РїСЂРё РїРµСЂРµС…РѕРґРµ IN_POSITION в†’ CLOSING (TP РёСЃРїРѕР»РЅРёР»СЃСЏ).
        Р‘РµР· mass cancel DCA РѕС‚РєСЂРѕСЋС‚ РЅРѕРІСѓСЋ РїРѕР·РёС†РёСЋ.

        Р’С‹Р·С‹РІР°РµС‚ cancel_order() РґР»СЏ РєР°Р¶РґРѕРіРѕ вЂ” РїСЂРё РїРµСЂРІРѕР№ РЅРµСѓРґР°С‡Рµ С‡РµСЂРµР·
        CANCEL_MAX_RETRIES Р±СЂРѕСЃР°РµС‚ StopCraneError.
        """
        dca_ids = list(state.active_dca_order_ids)

        if not dca_ids:
            return state

        logger.info("Mass cancel %d DCA-РѕСЂРґРµСЂРѕРІ РґР»СЏ С†РёРєР»Р° %s", len(dca_ids), cycle_id)

        cancelled = []
        for order_id in dca_ids:
            self.cancel_order(order_id, order_role="DCA")  # StopCraneError РїСЂРё РЅРµСѓРґР°С‡Рµ
            cancelled.append(order_id)

        # РћС‡РёС‰Р°РµРј СЃРїРёСЃРѕРє Р°РєС‚РёРІРЅС‹С… DCA РІ СЃРѕСЃС‚РѕСЏРЅРёРё
        new_state = self._state_manager.commit(
            state,
            replace(state, active_dca_order_ids=()),
        )
        return new_state

    # ------------------------------------------------------------------
    # Р’РЅСѓС‚СЂРµРЅРЅРёРµ РјРµС‚РѕРґС‹
    # ------------------------------------------------------------------

    def _send_order(
        self,
        request: "OrderRequest",
        order_role: OrderType,
    ) -> "OrderCreated":
        """
        РћС‚РїСЂР°РІРёС‚СЊ РѕСЂРґРµСЂ РЅР° Р±РёСЂР¶Сѓ.

        РўР°Р№РјР°СѓС‚ = РЅРµРјРµРґР»РµРЅРЅРѕ StopCraneError (РёСЃС…РѕРґ РЅРµРёР·РІРµСЃС‚РµРЅ).
        РЎРµС‚РµРІР°СЏ РѕС€РёР±РєР° (РЅРµ С‚Р°Р№РјР°СѓС‚) = retry СЃ СЌРєСЃРїРѕРЅРµРЅС†РёР°Р»СЊРЅРѕР№ Р·Р°РґРµСЂР¶РєРѕР№.
        INSUFFICIENT_FUNDS = InsufficientFundsError.
        """
        from .errors import StopCraneError as _StopCraneError  # noqa: PLC0415
        import socket  # noqa: PLC0415

        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                created = self._broker.create_order(request)
                return created

            except TimeoutError as exc:
                # РўР°Р№РјР°СѓС‚ create_order = STOP_CRANE РЅРµРјРµРґР»РµРЅРЅРѕ (РўР— 4 Рё РўР— 7)
                raise _StopCraneError(
                    f"РўР°Р№РјР°СѓС‚ create_order ({self._request_timeout}СЃ) вЂ” "
                    f"РёСЃС…РѕРґ РѕСЂРґРµСЂР° РЅРµРёР·РІРµСЃС‚РµРЅ. РўСЂРµР±СѓРµС‚СЃСЏ СЂСѓС‡РЅР°СЏ РїСЂРѕРІРµСЂРєР°.",
                    invariant="create_order_outcome_known",
                    expected={
                        "client_order_id": request.client_order_id,
                        "status": "PENDING РёР»Рё FILLED",
                    },
                    actually_found=None,
                    db_state={
                        "client_order_id": request.client_order_id,
                        "role": order_role.value,
                    },
                ) from exc

            except Exception as exc:
                exc_str = str(exc).lower()

                # РџСЂРѕРІРµСЂСЏРµРј РЅРµС…РІР°С‚РєСѓ СЃСЂРµРґСЃС‚РІ
                if any(s in exc_str for s in self._INSUFFICIENT_FUNDS_REASONS):
                    raise InsufficientFundsError(
                        f"РќРµС…РІР°С‚РєР° СЃСЂРµРґСЃС‚РІ РїСЂРё РїРѕСЃС‚Р°РЅРѕРІРєРµ {order_role.value}: {exc}",
                        required=str(request.quantity * (request.price or Decimal(0))),
                        available="unknown",  # СЂРµР°Р»СЊРЅС‹Р№ Р±Р°Р»Р°РЅСЃ РІ TickContext
                    ) from exc

                # РЎРµС‚РµРІР°СЏ РѕС€РёР±РєР° вЂ” retry
                last_error = exc
                if attempt < self._max_retries:
                    delay = (
                        self._retry_delay * (2 ** attempt)
                        + random.uniform(0, self._retry_delay * 0.3)
                    )
                    logger.warning(
                        "РЎРµС‚РµРІР°СЏ РѕС€РёР±РєР° РїСЂРё create_order (РїРѕРїС‹С‚РєР° %d/%d), "
                        "retry С‡РµСЂРµР· %.1fСЃ: %s",
                        attempt + 1, self._max_retries + 1, delay, exc,
                    )
                    time.sleep(delay)
                else:
                    raise

        raise RuntimeError(f"РќРµРѕР¶РёРґР°РЅРЅС‹Р№ РІС‹С…РѕРґ РёР· retry-С†РёРєР»Р°: {last_error}")

    @staticmethod
    def _generate_client_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _build_request(
        *,
        ticker: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        client_order_id: str,
        bot_id: str,
        cycle_id: str,
    ) -> "OrderRequest":
        """РЎРѕР±СЂР°С‚СЊ OrderRequest РёР· Рџ4."""
        from broker import OrderRequest  # noqa: PLC0415
        return OrderRequest(
            ticker=ticker,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            client_order_id=client_order_id,
            bot_id=bot_id,
            cycle_id=cycle_id,
        )