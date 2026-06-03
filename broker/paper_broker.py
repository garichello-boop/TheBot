"""
broker/paper_broker.py — PaperBroker: бумажная торговля.

Симулирует реальную биржу без реальных денег. Специально настроен
пессимистичнее реальности — чтобы реальные результаты были не хуже Paper.

Цены исполнения:
    BUY  MARKET → ask + slippage   (платим больше рыночного — пессимистично)
    SELL MARKET → bid - slippage   (получаем меньше — пессимистично)
    BUY  LIMIT  → limit_price      (pending до достижения цены)
    SELL LIMIT  → limit_price      (pending до достижения цены)

Жизненный цикл ордера в двух тиках:

    РўРёРє N:
        1. process_market_tick(bid, ask) → обновить цену, заполнить LIMIT
        2. DecisionEngine решает BUY → create_order(MARKET BUY)
           → PaperBroker: исполнить немедленно, сохранить fill во внутренней очереди
           → вернуть OrderCreated(PENDING)  # per IBroker контракт
        3. commit + emit (без знания о fill — как на реальной бирже)

    РўРёРє N+1:
        1. get_pending_fills() → вернуть накопленные fills (включая fill из тика N)
        2. TickContext видит fill → FSM переход ENTERING → IN_POSITION

    Это поведение идентично реальной бирже где WS-подтверждение приходит
    асинхронно после выставления ордера. BotLoop не требует изменений.

Регистрация ролей ордеров:
    OrderManager вызывает register_order_role() после create_order() чтобы
    PaperBroker знал тип ордера (ENTRY/TP/DCA) при формировании FillEvent.
    Это необходимо для корректной фильтрации fills в TickContext.fills_for_entry/tp/dca.
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

# Runtime import: business_logic.types не импортирует broker в рантайме
# (только через TYPE_CHECKING), поэтому циклического импорта нет.
from business_logic.types import FillEvent
from business_logic.types import OrderType as FillOrderType
from business_logic.types import OrderStatus as FillOrderStatus

logger = logging.getLogger(__name__)

# (OrderRequest, locked_usdt) — locked_usdt > 0 только для BUY LIMIT
_PendingEntry = Tuple[OrderRequest, Decimal]

# Роль ордера в торговом цикле — маппинг на FillOrderType
_ROLE_MAP: Dict[str, FillOrderType] = {
    "ENTRY": FillOrderType.ENTRY,
    "TP":    FillOrderType.TP,
    "DCA":   FillOrderType.DCA,
}


class PaperBroker(IBroker):
    """
    Бумажный брокер. Дефолтный режим при старте бота (BROKER_TYPE=paper).

    Создаётся через BrokerFactory. После 2-4 недель успешной бумажной
    торговли переключить на BybitBroker (BROKER_TYPE=bybit).
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

        # Виртуальный баланс в USDT
        self._free_usdt: Decimal = initial_balance
        self._locked_usdt: Decimal = Decimal("0")

        # Pending LIMIT ордера: exchange_order_id → (request, locked_usdt)
        self._pending: Dict[str, _PendingEntry] = {}

        # Очередь fills ожидающих забора через get_pending_fills()
        self._fill_queue: List[OrderFill] = []

        # Реестр ролей: exchange_order_id → "ENTRY" | "TP" | "DCA"
        # Заполняется через register_order_role() от OrderManager.
        # Необходим для корректного FillEvent.order_type при get_pending_fills().
        self._order_roles: Dict[str, str] = {}

        # Текущая цена (обновляется через process_market_tick или при MARKET orders)
        self._last_bid: Optional[Decimal] = None
        self._last_ask: Optional[Decimal] = None

        # Кэш market info (задаётся через set_market_info)
        self._market_info_cache: Dict[str, MarketInfo] = {}

        logger.info(
            "PaperBroker запущен | баланс=%.2f USDT | комиссия=%.4f%% | slippage=%.4f%%",
            initial_balance,
            float(commission_pct * 100),
            float(slippage_pct * 100),
        )

    # ------------------------------------------------------------------
    # IBroker — публичный интерфейс
    # ------------------------------------------------------------------

    def create_order(self, order: OrderRequest) -> OrderCreated:
        """
        Создать ордер.

        MARKET → исполняется немедленно. Fill помещается в очередь
                 и будет возвращён на следующем get_pending_fills().
        LIMIT  → сохраняется как pending. Исполнится в process_market_tick()
                 или get_pending_fills() когда цена достигнет уровня.
        """
        if order.order_type == OrderType.MARKET:
            return self._fill_market_order(order)
        return self._place_limit_order(order)

    def cancel_order(self, order_id: str) -> bool:
        """
        Отменить ордер. Идемпотентен — True если ордера нет.
        """
        if order_id not in self._pending:
            return True

        request, locked = self._pending.pop(order_id)
        self._order_roles.pop(order_id, None)
        if locked > Decimal("0"):
            self._unlock_usdt(locked)

        logger.info(
            "PaperBroker: отменён %s %s (order_id=%s, разблокировано=%.4f USDT)",
            request.side.value, request.ticker, order_id, float(locked),
        )
        return True

    def get_order_status(self, order_id: str) -> OrderStatus:
        """Статус ордера. Только для reconciliation на старте."""
        if order_id in self._pending:
            return OrderStatus.PENDING
        raise OrderNotFoundError(
            f"PaperBroker: ордер {order_id} не найден. "
            f"Возможно уже исполнен или отменён."
        )

    def get_balance(self) -> Balance:
        """
        Текущий виртуальный баланс бота в USDT.

        Возвращает Balance с dict-полями {asset: amount},
        совместимый с BalanceReconciler.
        """
        return Balance(
            free={"USDT": self._free_usdt},
            locked={"USDT": self._locked_usdt},
        )

    def get_market_info(self, ticker: str) -> MarketInfo:
        """Торговые ограничения инструмента."""
        if ticker in self._market_info_cache:
            return self._market_info_cache[ticker]

        logger.warning(
            "PaperBroker: MarketInfo для %s не задан — используем дефолт. "
            "Вызовите set_market_info() при старте для точной симуляции.",
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
        """Список активных (pending) ордеров. Для reconciliation на старте."""
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
        Дренировать внутреннюю очередь событий исполнения.

        Вызывается в начале каждого тика из TickContext.collect().

        Дополнительно: проверяет LIMIT ордера против последней известной цены
        (_last_bid/_last_ask). Это позволяет TP и DCA ордерам срабатывать
        даже без явного вызова process_market_tick(), хотя и с задержкой
        в один тик (цена обновляется при исполнении MARKET ордеров).

        Для точной симуляции: вызывать process_market_tick() явно
        до get_pending_fills() с актуальными ценами.

        FillEvent.order_type заполняется из реестра ролей (_order_roles).
        Если роль не зарегистрирована — дефолт ENTRY (с WARNING).
        """
        # Проверить LIMIT ордера если есть актуальные цены
        if self._last_bid is not None and self._last_ask is not None:
            self._check_and_execute_limits(self._last_bid, self._last_ask)

        # Конвертировать OrderFill → FillEvent и вернуть
        result = []
        for fill in self._fill_queue:
            role_str = self._order_roles.get(fill.exchange_order_id)
            if role_str is None:
                logger.warning(
                    "PaperBroker.get_pending_fills(): роль ордера %s не зарегистрирована "
                    "— используем ENTRY. Вызовите register_order_role() после create_order().",
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
                remaining_qty=Decimal("0"),   # PaperBroker всегда полное исполнение
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
        Историческая лента сделок — только для reconciliation.

        PaperBroker не хранит историю fills — возвращает пустой список.
        Полная реализация требует хранения fills в TradeRepository
        с trade_id для инкрементального доступа.
        """
        return []

    def get_mode(self) -> BrokerMode:
        return BrokerMode.PAPER

    # ------------------------------------------------------------------
    # Paper-specific API
    # ------------------------------------------------------------------

    def process_market_tick(self, bid: Decimal, ask: Decimal) -> List[OrderFill]:
        """
        Обновить цену и проверить LIMIT ордера. Возвращает сырые OrderFill.

        Оставлен для обратной совместимости и явного вызова из тестов.
        В продакшн tick-loop используется get_pending_fills() — он вызывает
        _check_and_execute_limits() внутри, используя сохранённые цены.
        """
        self._last_bid = bid
        self._last_ask = ask
        self._check_and_execute_limits(bid, ask)
        # Fills накапливаются в _fill_queue для get_pending_fills().
        # process_market_tick() не дренирует очередь — дренаж только в get_pending_fills().
        return []

    def register_order_role(self, exchange_order_id: str, role: str) -> None:
        """
        Зарегистрировать бизнес-роль ордера (ENTRY / TP / DCA).

        Обязательно вызывать из OrderManager после create_order():
            created = broker.create_order(request)
            broker.register_order_role(created.exchange_order_id, "TP")

        Без этого get_pending_fills() не сможет правильно заполнить
        FillEvent.order_type, и TickContext.fills_for_tp/entry/dca вернут
        пустые кортежи — FSM не будет двигаться.

        role: "ENTRY" | "TP" | "DCA"
        """
        if role not in _ROLE_MAP:
            logger.warning(
                "PaperBroker.register_order_role: неизвестная роль %r "
                "(ожидается ENTRY/TP/DCA). order_id=%s",
                role, exchange_order_id,
            )
        self._order_roles[exchange_order_id] = role

    def set_market_info(self, info: MarketInfo) -> None:
        """Задать торговые ограничения инструмента."""
        self._market_info_cache[info.ticker] = info
        logger.debug(
            "PaperBroker: MarketInfo задан для %s "
            "(min_qty=%s, step_size=%s, min_notional=%s)",
            info.ticker, info.min_qty, info.step_size, info.min_notional,
        )

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _check_and_execute_limits(self, bid: Decimal, ask: Decimal) -> None:
        """Найти и исполнить сработавшие LIMIT ордера."""
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
        """Исполнить MARKET ордер немедленно по ask/bid ± slippage."""
        if self._last_ask is None or self._last_bid is None:
            raise BrokerError(
                "PaperBroker: нет текущей цены для MARKET ордера. "
                "process_market_tick() или register_order_role() должны быть вызваны сначала."
            )

        execution_price = self._market_execution_price(order.side)
        order_id = f"paper_{uuid4().hex[:12]}"

        if order.side == OrderSide.BUY:
            total_cost = order.quantity * execution_price * (1 + self._commission_pct)
            if total_cost > self._free_usdt:
                raise InsufficientFundsError(
                    f"PaperBroker: недостаточно USDT для BUY MARKET. "
                    f"Нужно {float(total_cost):.4f}, доступно {float(self._free_usdt):.4f}"
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
        """Поставить LIMIT ордер в pending."""
        if order.price is None:
            raise BrokerRejected(
                f"PaperBroker: LIMIT ордер {order.client_order_id} без цены"
            )

        order_id = f"paper_{uuid4().hex[:12]}"
        lock_amount = Decimal("0")

        if order.side == OrderSide.BUY:
            lock_amount = order.quantity * order.price * (1 + self._commission_pct)
            if lock_amount > self._free_usdt:
                raise InsufficientFundsError(
                    f"PaperBroker: недостаточно USDT для BUY LIMIT. "
                    f"Нужно {float(lock_amount):.4f}, доступно {float(self._free_usdt):.4f}"
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
        Проверить достигнут ли уровень LIMIT ордера.

        BUY  LIMIT: исполняется если ask <= limit_price
        SELL LIMIT: исполняется если bid >= limit_price
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
        Применить исполнение ордера: обновить баланс, записать в БД,
        эмитировать ORDER_FILLED, добавить в очередь.
        """
        commission = request.quantity * execution_price * self._commission_pct
        ts = time.time()
        side_str = request.side if isinstance(request.side, str) else request.side.value

        if locked_to_release > Decimal("0"):
            self._unlock_usdt(locked_to_release)

        if side_str == "BUY":
            total_cost = request.quantity * execution_price + commission
            self._free_usdt -= total_cost
            # Обновить кэш цен из фактической цены исполнения
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
                    "PaperBroker: ошибка сохранения fill в БД (order_id=%s): %s",
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
            "баланс=%.4f free / %.4f locked",
            side_str, request.ticker,
            request.quantity, execution_price, commission,
            float(self._free_usdt), float(self._locked_usdt),
        )

    def _market_execution_price(self, side: OrderSide) -> Decimal:
        """Цена исполнения MARKET с пессимистичным slippage."""
        if side == OrderSide.BUY:
            return self._last_ask * (1 + self._slippage_pct)
        return self._last_bid * (1 - self._slippage_pct)

    def _lock_usdt(self, amount: Decimal) -> None:
        self._free_usdt -= amount
        self._locked_usdt += amount

    def _unlock_usdt(self, amount: Decimal) -> None:
        self._locked_usdt -= amount
        self._free_usdt += amount