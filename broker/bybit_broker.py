"""
broker/bybit_broker.py — BybitBroker: реальная торговля через Bybit REST API.

Включается после 2-4 недель успешной работы на PaperBroker:
    BROKER_TYPE=bybit в .env или системных переменных.

Разделение ответственности:
    BybitBroker     → REST: create/cancel ордера, балансы, статусы, market info
    BybitOrderTracker → WS: fill-уведомления в реальном времени
    BotLoop           → управляет обоими (broker + tracker раздельно)

Политика таймаутов и retry (из ТЗ 7):
    create_order:               таймаут → BrokerTimeout → STOP-CRANE, NO retry
    cancel_order / get_status:  сетевые ошибки → exponential retry с jitter
    rate limit (HTTP 429):      Retry-After header → WARNING, retry до 5 раз
"""
from __future__ import annotations

import logging
import random
import time
from decimal import Decimal
from typing import Dict, List, Optional

from broker.broker import (
    BrokerError,
    BrokerNetworkError,
    BrokerRejected,
    BrokerTimeout,
    IBroker,
    InsufficientFundsError,
    OrderNotFoundError,
)
from broker.exchange_adapter import BybitExchangeAdapter
from broker.models import (
    Balance,
    BrokerMode,
    MarketInfo,
    OpenOrder,
    OrderCreated,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from broker.order_tracker import map_bybit_status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bybit retCode → семантика ошибок
# ---------------------------------------------------------------------------
_INSUFFICIENT_BALANCE_CODES = {110007, 110012}
_ORDER_NOT_FOUND_CODES = {20001, 110001, 170213}
_RATE_LIMIT_HTTP_STATUS = 429

# Максимум retry при rate limit
_RATE_LIMIT_MAX_RETRIES = 5


class BybitBroker(IBroker):
    """
    Брокер для Bybit Unified Trading Account.

    Создаётся через BrokerFactory при BROKER_TYPE=bybit.
    Использует pybit (официальная Python-библиотека Bybit).

    Fills приходят ОТДЕЛЬНО через BybitOrderTracker (приватный WS).
    BybitBroker не знает о трекере — BotLoop управляет ими независимо.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        emitter,                            # observability.EventEmitter
        testnet: bool = False,
        request_timeout_sec: float = 5.0,   # Таймаут → BrokerTimeout
        retry_delay_sec: float = 1.0,       # Начальная задержка retry
        max_retries: int = 3,               # BROKER_MAX_RETRIES
        category: str = "spot",
    ) -> None:
        try:
            from pybit.unified_trading import HTTP
        except ImportError as exc:
            raise ImportError(
                "pybit не установлен. Выполните: pip install pybit"
            ) from exc

        # pybit HTTP клиент. Timeout задаётся через recv_window (мс).
        # Дополнительно оборачиваем вызовы в try/except на requests.Timeout.
        self._http = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=int(request_timeout_sec * 1000),
        )

        self._emitter = emitter
        self._request_timeout_sec = request_timeout_sec
        self._retry_delay_sec = retry_delay_sec
        self._max_retries = max_retries
        self._category = category
        self._adapter = BybitExchangeAdapter()

        # Кэш market info — ограничения меняются редко
        self._market_info_cache: Dict[str, MarketInfo] = {}

        logger.info(
            "BybitBroker запущен | testnet=%s | category=%s | "
            "timeout=%.1fs | max_retries=%d",
            testnet, category, request_timeout_sec, max_retries,
        )

    # ------------------------------------------------------------------
    # IBroker — публичный интерфейс
    # ------------------------------------------------------------------

    def create_order(self, order: OrderRequest) -> OrderCreated:
        """
        Выставить ордер на Bybit через REST.

        Таймаут → BrokerTimeout → STOP-CRANE немедленно, без retry.
        Исход при таймауте неизвестен — биржа могла принять ордер до обрыва.

        Нехватка средств → InsufficientFundsError → FSM: WAITING_FOR_LIQUIDITY.
        """
        params = {
            "category": self._category,
            "symbol": order.ticker,
            "side": "Buy" if order.side == OrderSide.BUY else "Sell",
            "orderType": order.order_type.value.capitalize(),  # "Market" / "Limit"
            "qty": str(order.quantity),
        }

        if order.price is not None:
            params["price"] = str(order.price)

        # ExchangeAdapter: orderLinkId для идемпотентности
        params = self._adapter.inject(params, order.client_order_id)

        # create_order: только один вызов, без retry
        # Любой таймаут = исход неизвестен = STOP-CRANE
        try:
            response = self._http.place_order(**params)

        except Exception as exc:
            exc_type = type(exc).__name__
            exc_str = str(exc).lower()

            # Timeout в любом виде — исход неизвестен
            if "timeout" in exc_str or "timed out" in exc_str:
                raise BrokerTimeout(
                    f"Bybit create_order: таймаут ({self._request_timeout_sec}s). "
                    f"Исход неизвестен → STOP-CRANE. "
                    f"client_order_id={order.client_order_id}"
                ) from exc

            # Явная сетевая ошибка — запрос не дошёл до биржи
            if "connection" in exc_str or "network" in exc_str:
                raise BrokerNetworkError(
                    f"Bybit create_order: сетевая ошибка (запрос не дошёл). "
                    f"client_order_id={order.client_order_id}: {exc}"
                ) from exc

            raise BrokerError(
                f"Bybit create_order: неожиданная ошибка ({exc_type}): {exc}"
            ) from exc

        self._raise_on_error(response, context=f"create_order/{order.client_order_id}")

        result = response.get("result", {})
        exchange_order_id = result.get("orderId", "")

        if not exchange_order_id:
            raise BrokerError(
                f"Bybit create_order: пустой orderId в ответе. "
                f"client_order_id={order.client_order_id} | response={response}"
            )

        logger.info(
            "BybitBroker: ордер создан | %s %s qty=%s price=%s | "
            "exchange_order_id=%s | client_order_id=%s",
            order.side.value, order.ticker,
            order.quantity, order.price,
            exchange_order_id, order.client_order_id,
        )

        return OrderCreated(
            exchange_order_id=exchange_order_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.PENDING,
            mode=BrokerMode.LIVE,
        )

    def cancel_order(self, order_id: str) -> bool:
        """
        Отменить ордер. Exponential retry на сетевые ошибки.

        Идемпотентен: если ордер уже не активен — возвращает True.
        Close Protocol делает CANCEL_MAX_RETRIES попыток через RetryManager,
        этот метод — одна попытка с внутренним retry на уровне сети.
        """
        def _call():
            return self._http.cancel_order(
                category=self._category,
                orderId=order_id,
            )

        response = self._with_network_retry(_call, f"cancel_order({order_id})")
        ret_code = response.get("retCode", -1)

        if ret_code == 0:
            logger.info("BybitBroker: ордер отменён (order_id=%s)", order_id)
            return True

        # Ордер не найден — уже исполнен или отменён ранее
        if ret_code in _ORDER_NOT_FOUND_CODES:
            logger.info(
                "BybitBroker: cancel_order(%s) — ордер не найден (уже закрыт), OK",
                order_id,
            )
            return True

        logger.warning(
            "BybitBroker: cancel_order(%s) вернул retCode=%s retMsg='%s'",
            order_id, ret_code, response.get("retMsg", ""),
        )
        return False

    def get_order_status(self, order_id: str) -> OrderStatus:
        """
        Разовая проверка статуса ордера через REST.
        Используется ТОЛЬКО при reconciliation на старте.
        В рабочем режиме статусы приходят через BybitOrderTracker (WS).
        """
        def _call():
            return self._http.get_order_realtime(
                category=self._category,
                orderId=order_id,
            )

        response = self._with_network_retry(_call, f"get_order_status({order_id})")
        ret_code = response.get("retCode", -1)

        if ret_code in _ORDER_NOT_FOUND_CODES:
            raise OrderNotFoundError(
                f"Bybit: ордер {order_id} не найден (retCode={ret_code})"
            )

        self._raise_on_error(response, context=f"get_order_status/{order_id}")

        result = response.get("result", {})
        orders = result.get("list", [])

        if not orders:
            raise OrderNotFoundError(
                f"Bybit: пустой список ордеров при запросе {order_id}"
            )

        bybit_status = orders[0].get("orderStatus", "")
        status = map_bybit_status(bybit_status)

        logger.debug(
            "BybitBroker: get_order_status(%s) = %s (bybit_status='%s')",
            order_id, status, bybit_status,
        )
        return status

    def get_balance(self) -> Balance:
        """
        Получить USDT баланс Unified Account.

        free  = availableToWithdraw (доступно для новых ордеров)
        locked = locked (зарезервировано под открытые ордеры)
        """
        def _call():
            return self._http.get_wallet_balance(accountType="UNIFIED")

        response = self._with_network_retry(_call, "get_balance")
        self._raise_on_error(response, context="get_balance")

        result = response.get("result", {})
        accounts = result.get("list", [])

        for account in accounts:
            for coin in account.get("coin", []):
                if coin.get("coin") == "USDT":
                    free = Decimal(coin.get("availableToWithdraw", "0") or "0")
                    total = Decimal(coin.get("walletBalance", "0") or "0")
                    locked = total - free
                    return Balance(
                        free=free,
                        locked=max(locked, Decimal("0")),  # guard against rounding
                    )

        logger.warning(
            "BybitBroker: USDT не найден в wallet balance — возвращаем нули. "
            "Проверьте тип аккаунта (должен быть UNIFIED)."
        )
        return Balance(free=Decimal("0"), locked=Decimal("0"))

    def get_market_info(self, ticker: str) -> MarketInfo:
        """
        Торговые ограничения инструмента: min_qty, step_size, min_notional и др.
        Результат кэшируется — ограничения меняются редко.
        """
        if ticker in self._market_info_cache:
            return self._market_info_cache[ticker]

        def _call():
            return self._http.get_instruments_info(
                category=self._category,
                symbol=ticker,
            )

        response = self._with_network_retry(_call, f"get_market_info({ticker})")
        self._raise_on_error(response, context=f"get_market_info/{ticker}")

        result = response.get("result", {})
        instruments = result.get("list", [])

        if not instruments:
            raise BrokerError(
                f"Bybit: инструмент {ticker} не найден в категории {self._category}"
            )

        inst = instruments[0]
        lot = inst.get("lotSizeFilter", {})
        price_f = inst.get("priceFilter", {})

        # Bybit spot: basePrecision = шаг количества, quotePrecision = шаг цены
        # Для некоторых пар используется qtyStep вместо basePrecision
        step_size_str = lot.get("basePrecision") or lot.get("qtyStep") or "0.001"
        tick_size_str = price_f.get("tickSize", "0.01")

        info = MarketInfo(
            ticker=ticker,
            min_qty=Decimal(lot.get("minOrderQty", "0")),
            step_size=Decimal(step_size_str),
            min_notional=Decimal(lot.get("minOrderAmt", "0")),
            price_precision=_count_decimals(tick_size_str),
            tick_size=Decimal(tick_size_str),
        )

        self._market_info_cache[ticker] = info
        logger.info(
            "BybitBroker: MarketInfo для %s | min_qty=%s | step=%s | "
            "min_notional=%s | tick=%s",
            ticker, info.min_qty, info.step_size, info.min_notional, info.tick_size,
        )
        return info

    def get_open_orders(self, ticker: Optional[str] = None) -> List[OpenOrder]:
        """
        Список активных ордеров. Используется при reconciliation на старте.
        StateRecovery запрашивает ВСЕ ордера по тикеру и сопоставляет с bot_state.
        """
        kwargs: dict = {"category": self._category}
        if ticker:
            kwargs["symbol"] = ticker

        def _call():
            return self._http.get_open_orders(**kwargs)

        response = self._with_network_retry(_call, "get_open_orders")
        self._raise_on_error(response, context="get_open_orders")

        result = response.get("result", {})
        orders_raw = result.get("list", [])
        orders: List[OpenOrder] = []

        for raw in orders_raw:
            try:
                orders.append(_parse_open_order(raw))
            except Exception as exc:
                logger.warning(
                    "BybitBroker: не удалось распарсить ордер (пропускаем): "
                    "%s | data=%s",
                    exc, raw,
                )

        return orders

    def get_mode(self) -> BrokerMode:
        return BrokerMode.LIVE

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _with_network_retry(self, fn, operation: str):
        """
        Выполнить fn с exponential retry на сетевые ошибки.

        Применяется для: cancel_order, get_order_status, get_balance,
        get_market_info, get_open_orders.

        НЕ применяется для create_order — там таймаут = STOP-CRANE.

        Retry-расписание: delay = min(base * 2^attempt + jitter, 30s)
        где jitter = random.uniform(0, 1.0) для предотвращения thundering herd.
        """
        last_exc: Optional[Exception] = None
        rate_limit_retries = 0

        for attempt in range(self._max_retries + 1):
            try:
                result = fn()

                # HTTP 429 rate limit — уважаем Retry-After если есть
                if isinstance(result, dict):
                    http_status = result.get("retCode")
                    if http_status == _RATE_LIMIT_HTTP_STATUS:
                        if rate_limit_retries >= _RATE_LIMIT_MAX_RETRIES:
                            raise BrokerError(
                                f"Bybit {operation}: rate limit, исчерпаны retry"
                            )
                        retry_after = float(result.get("retExtInfo", {}).get(
                            "retryAfter", 5.0
                        ))
                        logger.warning(
                            "BybitBroker %s: rate limit (429), ждём %.1fs (попытка %d)",
                            operation, retry_after, rate_limit_retries + 1,
                        )
                        self._emitter.emit(
                            event_type="TICK_LATENCY",
                            level="WARNING",
                            message=f"Bybit rate limit на {operation}, пауза {retry_after}s",
                            payload={"operation": operation, "retry_after": retry_after},
                        )
                        time.sleep(retry_after)
                        rate_limit_retries += 1
                        continue

                return result

            except Exception as exc:
                exc_str = str(exc).lower()
                is_network = (
                    "timeout" in exc_str
                    or "timed out" in exc_str
                    or "connection" in exc_str
                    or "network" in exc_str
                    or "remotedisconnected" in exc_str
                    or "connectionreset" in exc_str
                )

                if not is_network:
                    # Не сетевая ошибка — пробрасываем сразу
                    raise BrokerError(
                        f"Bybit {operation}: неожиданная ошибка: {exc}"
                    ) from exc

                last_exc = exc
                logger.warning(
                    "BybitBroker %s: сетевая ошибка (попытка %d/%d): %s",
                    operation, attempt + 1, self._max_retries + 1, exc,
                )

            if attempt < self._max_retries:
                delay = min(
                    self._retry_delay_sec * (2 ** attempt) + random.uniform(0, 1.0),
                    30.0,
                )
                logger.debug(
                    "BybitBroker %s: retry через %.2fs", operation, delay
                )
                time.sleep(delay)

        raise BrokerNetworkError(
            f"Bybit {operation}: исчерпаны все попытки ({self._max_retries + 1})"
        ) from last_exc

    def _raise_on_error(self, response: dict, context: str = "") -> None:
        """
        Проверить retCode ответа Bybit и бросить соответствующее исключение.
        retCode == 0 → всё хорошо, return.
        """
        ret_code = response.get("retCode", -1)

        if ret_code == 0:
            return

        ret_msg = response.get("retMsg", "no message")

        if ret_code in _INSUFFICIENT_BALANCE_CODES:
            raise InsufficientFundsError(
                f"Bybit: недостаточно средств (retCode={ret_code}). "
                f"context={context}"
            )

        if ret_code in _ORDER_NOT_FOUND_CODES:
            raise OrderNotFoundError(
                f"Bybit: ордер не найден (retCode={ret_code}). "
                f"context={context}"
            )

        raise BrokerRejected(
            f"Bybit API error: retCode={ret_code}, retMsg='{ret_msg}'. "
            f"context={context}"
        )


# ---------------------------------------------------------------------------
# Утилиты (module-level, не методы класса)
# ---------------------------------------------------------------------------

def _parse_open_order(raw: dict) -> OpenOrder:
    """Распарсить ответ Bybit в OpenOrder. Бросает ValueError при плохих данных."""
    side_raw = raw.get("side", "").upper()
    order_type_raw = raw.get("orderType", "").upper()
    price_str = raw.get("price", "")

    side = OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL
    order_type = OrderType.MARKET if order_type_raw == "MARKET" else OrderType.LIMIT
    bybit_status = raw.get("orderStatus", "")

    return OpenOrder(
        exchange_order_id=raw.get("orderId", ""),
        client_order_id=raw.get("orderLinkId", ""),
        ticker=raw.get("symbol", ""),
        side=side,
        order_type=order_type,
        quantity=Decimal(raw.get("qty", "0")),
        filled_qty=Decimal(raw.get("cumExecQty", "0")),
        price=Decimal(price_str) if price_str else None,
        status=map_bybit_status(bybit_status),
        mode=BrokerMode.LIVE,
    )


def _count_decimals(value_str: str) -> int:
    """
    Количество знаков после запятой в строке типа '0.01' → 2, '1' → 0.
    Trailing zeros учитываются: '0.010' → 2 (значащие знаки).
    """
    if "." not in value_str:
        return 0
    decimals = value_str.rstrip("0").split(".")[1]
    return len(decimals) if decimals else 0
