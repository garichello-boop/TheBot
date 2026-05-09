"""
broker/order_tracker.py — отслеживание статусов ордеров через WS.

BybitOrderTracker слушает приватный WS Bybit в фоновом потоке и накапливает
fills. BotLoop забирает их в начале каждого тика через pop_recent_fills():

    # В BotLoop (LIVE режим):
    recent_fills = order_tracker.pop_recent_fills()

    # В BotLoop (PAPER режим):
    recent_fills = paper_broker.process_market_tick(bid, ask)

PaperBroker не использует OrderTracker — роль трекера там выполняет
process_market_tick() синхронно.

Разделение ответственности:
    BybitBroker     → REST: create_order, cancel_order, get_balance, etc.
    BybitOrderTracker → WS: статусы ордеров, fills, уведомления
    BotLoop           → управляет обоими, собирает fills на каждом тике
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, List, Optional

from broker.models import BrokerMode, OrderFill, OrderSide, OrderStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Маппинг статусов Bybit → внутренние OrderStatus
# Используется и в BybitOrderTracker, и в BybitBroker.get_order_status()
# ---------------------------------------------------------------------------

BYBIT_STATUS_MAP: Dict[str, OrderStatus] = {
    "New": OrderStatus.PENDING,
    "Created": OrderStatus.PENDING,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "PartiallyFilledCanceled": OrderStatus.PARTIALLY_FILLED,  # Частично + отменён
    "Rejected": OrderStatus.REJECTED,
    "Triggered": OrderStatus.PENDING,   # Conditional order triggered
    "Deactivated": OrderStatus.CANCELLED,
}


def map_bybit_status(bybit_status: str) -> OrderStatus:
    """Конвертировать строку статуса Bybit в OrderStatus."""
    return BYBIT_STATUS_MAP.get(bybit_status, OrderStatus.UNKNOWN)


# ---------------------------------------------------------------------------
# Абстрактный трекер
# ---------------------------------------------------------------------------

class OrderTracker(ABC):
    """
    Абстрактный трекер статусов ордеров.

    Реализации:
    - BybitOrderTracker: приватный WS Bybit (LIVE режим)
    - Не нужен для PaperBroker: роль выполняет process_market_tick()
    """

    @abstractmethod
    def start(self) -> None:
        """Подключиться к бирже и начать получать апдейты."""

    @abstractmethod
    def stop(self) -> None:
        """Отключиться и остановить трекер."""

    @abstractmethod
    def pop_recent_fills(self) -> List[OrderFill]:
        """
        Вернуть и очистить все накопленные fills с момента последнего вызова.

        Thread-safe. Вызывается из основного потока BotLoop в начале каждого тика
        ПЕРЕД сборкой TickContext. Быстрая операция — только чтение очереди.
        """


# ---------------------------------------------------------------------------
# Bybit реализация
# ---------------------------------------------------------------------------

class BybitOrderTracker(OrderTracker):
    """
    Отслеживание ордеров Bybit через приватный WebSocket.

    WS-callbacks выполняются в фоновом потоке pybit.
    Fills накапливаются в thread-safe очереди.
    BotLoop читает их через pop_recent_fills() в начале каждого тика.

    Reconnect при обрыве WS: pybit обрабатывает самостоятельно через
    встроенный механизм переподключения.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        emitter,           # observability.EventEmitter
        testnet: bool = False,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._emitter = emitter
        self._testnet = testnet

        self._lock = threading.Lock()
        self._fill_queue: List[OrderFill] = []
        self._ws = None
        self._running = False

    def start(self) -> None:
        """
        Подключиться к приватному WS Bybit и подписаться на ордера.
        Вызывается один раз при старте бота, до начала tick-loop.
        """
        try:
            from pybit.unified_trading import WebSocket
            self._ws = WebSocket(
                testnet=self._testnet,
                channel_type="private",
                api_key=self._api_key,
                api_secret=self._api_secret,
            )
            self._ws.order_stream(callback=self._on_order_update)
            self._running = True
            logger.info(
                "BybitOrderTracker: подключён к приватному WS (testnet=%s)",
                self._testnet,
            )
        except Exception as exc:
            logger.error("BybitOrderTracker: ошибка подключения к WS: %s", exc)
            raise

    def stop(self) -> None:
        """Закрыть WS-соединение. Вызывается при остановке бота."""
        self._running = False
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception as exc:
                logger.warning(
                    "BybitOrderTracker: ошибка при закрытии WS: %s", exc
                )
            self._ws = None
        logger.info("BybitOrderTracker: отключён")

    def pop_recent_fills(self) -> List[OrderFill]:
        """Thread-safe: вернуть и очистить очередь fills."""
        with self._lock:
            fills = list(self._fill_queue)
            self._fill_queue.clear()
        return fills

    # ------------------------------------------------------------------
    # WS callbacks — выполняются в фоновом потоке pybit
    # ------------------------------------------------------------------

    def _on_order_update(self, message: dict) -> None:
        """
        Точка входа для WS-сообщений от Bybit.
        Вызывается в фоновом потоке — все операции thread-safe.
        """
        try:
            data_list = message.get("data", [])
            if not isinstance(data_list, list):
                return
            for order_data in data_list:
                self._process_order_update(order_data)
        except Exception as exc:
            # Ошибка в callback не должна убивать WS-соединение
            logger.error(
                "BybitOrderTracker: ошибка обработки WS-сообщения: %s | msg=%s",
                exc, message,
            )

    def _process_order_update(self, data: dict) -> None:
        """Обработать один ордерный апдейт из WS."""
        bybit_status = data.get("orderStatus", "")
        order_status = map_bybit_status(bybit_status)

        order_id = data.get("orderId", "")
        client_order_id = data.get("orderLinkId", "")
        ticker = data.get("symbol", "")
        side_raw = data.get("side", "").upper()

        if order_status == OrderStatus.FILLED:
            fill = self._parse_fill(data, order_id, client_order_id, ticker, side_raw,
                                    is_partial=False)
            if fill:
                with self._lock:
                    self._fill_queue.append(fill)
                self._emitter.emit(
                    event_type="ORDER_FILLED",
                    level="INFO",
                    message=(
                        f"[LIVE] {side_raw} {ticker} "
                        f"qty={fill.filled_qty} @ {fill.avg_price}"
                    ),
                    payload={
                        "exchange_order_id": order_id,
                        "client_order_id": client_order_id,
                        "side": side_raw,
                        "filled_qty": str(fill.filled_qty),
                        "avg_price": str(fill.avg_price),
                        "commission": str(fill.commission),
                        "mode": "LIVE",
                    },
                )

        elif order_status == OrderStatus.PARTIALLY_FILLED:
            fill = self._parse_fill(data, order_id, client_order_id, ticker, side_raw,
                                    is_partial=True)
            if fill:
                with self._lock:
                    self._fill_queue.append(fill)
                self._emitter.emit(
                    event_type="ORDER_PARTIALLY_FILLED",
                    level="WARNING",
                    message=(
                        f"[LIVE] Частичное исполнение: {side_raw} {ticker} "
                        f"filled={fill.filled_qty}"
                    ),
                    payload={
                        "exchange_order_id": order_id,
                        "client_order_id": client_order_id,
                        "filled_qty": str(fill.filled_qty),
                        "avg_price": str(fill.avg_price),
                        "remaining_qty": data.get("leavesQty", "unknown"),
                        "mode": "LIVE",
                    },
                )

        elif order_status == OrderStatus.CANCELLED:
            self._emitter.emit(
                event_type="ORDER_CANCELLED",
                level="WARNING",
                message=f"[LIVE] Ордер отменён: {order_id} ({ticker})",
                payload={
                    "exchange_order_id": order_id,
                    "client_order_id": client_order_id,
                    "initiated_by": "exchange_or_user",
                    "ticker": ticker,
                },
            )

        elif order_status == OrderStatus.REJECTED:
            self._emitter.emit(
                event_type="ORDER_REJECTED",
                level="ERROR",
                message=f"[LIVE] Ордер отклонён биржей: {order_id} ({ticker})",
                payload={
                    "exchange_order_id": order_id,
                    "client_order_id": client_order_id,
                    "reason": data.get("rejectReason", "unknown"),
                    "ticker": ticker,
                },
            )

        elif order_status == OrderStatus.UNKNOWN:
            logger.warning(
                "BybitOrderTracker: неизвестный статус ордера '%s' (order_id=%s)",
                bybit_status, order_id,
            )

    def _parse_fill(
        self,
        data: dict,
        order_id: str,
        client_order_id: str,
        ticker: str,
        side_raw: str,
        is_partial: bool,
    ) -> Optional[OrderFill]:
        """
        Распарсить данные исполнения из WS-сообщения в OrderFill.
        Возвращает None при ошибке парсинга (логируется, торговля продолжается).
        """
        try:
            filled_qty = Decimal(data.get("cumExecQty", "0"))
            avg_price = Decimal(data.get("avgPrice", "0"))
            commission = Decimal(data.get("cumExecFee", "0"))

            if filled_qty <= Decimal("0") or avg_price <= Decimal("0"):
                logger.warning(
                    "BybitOrderTracker: fill с нулевым qty или ценой (order_id=%s): "
                    "qty=%s price=%s — пропускаем",
                    order_id, filled_qty, avg_price,
                )
                return None

            side = OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL

            return OrderFill(
                exchange_order_id=order_id,
                client_order_id=client_order_id,
                ticker=ticker,
                side=side,
                filled_qty=filled_qty,
                avg_price=avg_price,
                commission=commission,
                mode=BrokerMode.LIVE,
                timestamp=time.time(),
                is_partial=is_partial,
            )
        except Exception as exc:
            logger.error(
                "BybitOrderTracker: ошибка парсинга fill (order_id=%s): %s | data=%s",
                order_id, exc, data,
            )
            return None
